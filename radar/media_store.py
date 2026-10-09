"""Idempotent persistence of aiograpi Media with historic metrics."""
import hashlib
import re
from datetime import timezone as dt_timezone
from types import SimpleNamespace
from django.db import transaction
from django.utils import timezone
from .models import (Post, PostSnapshot, PostMetricHistory, PostAsset, PostHashtag,
                     PostMention, PostCollaborator, PostLocation, PostLike)
from .relationship_store import upsert_identity

HASHTAG_RE = re.compile(r"(?<!\w)#([^\W_][\w\u200c]*)", re.UNICODE)
MENTION_RE = re.compile(r"(?<![\w.])@([A-Za-z0-9._]{1,100})")
METRIC_FIELDS = ("like_count", "view_count", "play_count", "comment_count")


def _count(raw):
    return max(0, int(raw)) if raw is not None else None


def _first_present(*values):
    """Return the first API counter that is present, preserving a real zero."""
    return next((value for value in values if value is not None), None)


def _published(raw):
    if raw and timezone.is_naive(raw):
        return timezone.make_aware(raw, dt_timezone.utc)
    return raw


def parse_media(media, target):
    """Preserve absent metrics as NULL; never turn missing views into a real zero."""
    pk = str(media.pk)
    code = str(media.code)
    if not pk.isdecimal() or not re.fullmatch(r"[\w-]{3,100}", code):
        raise ValueError("Invalid Instagram media identifier")
    caption = str(getattr(media, "caption_text", "") or "")
    user = getattr(media, "user", None)
    is_reel = str(getattr(media, "product_type", "") or "").lower() == "clips"
    result = {"media_pk": pk, "shortcode": code, "owner_pk": str(getattr(user, "pk", "") or ""),
              "owner_username": str(getattr(user, "username", target) or target),
              "owner_full_name": str(getattr(user, "full_name", "") or "")[:250],
              "owner_is_private": bool(getattr(user, "is_private", False)),
              "owner_is_verified": bool(getattr(user, "is_verified", False)),
              "owner_profile_pic_url": str(getattr(user, "profile_pic_url", "") or ""),
              "discovered_from": target, "media_type": int(media.media_type),
              "is_reel": is_reel, "caption": caption,
              "caption_hash": hashlib.sha256(caption.encode("utf-8")).hexdigest(),
              "published_at": _published(getattr(media, "taken_at", None) or
                                           getattr(media, "taken_at_utc", None))}
    # aiograpi/Instagram use slightly different names across media endpoints.
    # Keep NULL for genuinely absent counters, but persist every real alias.
    result["like_count"] = _count(getattr(media, "like_count", None))
    result["comment_count"] = _count(getattr(media, "comment_count", None))
    result["play_count"] = _count(_first_present(
        getattr(media, "play_count", None), getattr(media, "video_play_count", None)))
    result["view_count"] = _count(_first_present(
        getattr(media, "view_count", None), getattr(media, "video_view_count", None),
        result["play_count"]))
    resources = list(getattr(media, "resources", []) or []) or [media]
    result["assets"] = [{"position": index, "media_type": int(getattr(item, "media_type", media.media_type)),
                         "preview_url": str(getattr(item, "thumbnail_url", "") or ""),
                         "video_url": str(getattr(item, "video_url", "") or "")}
                        for index, item in enumerate(resources, start=1)]
    result["hashtags"] = {tag.casefold() for tag in HASHTAG_RE.findall(caption)}
    result["mentions"] = {tag.casefold() for tag in MENTION_RE.findall(caption)}
    for item in getattr(media, "usertags", []) or []:
        username = str(getattr(getattr(item, "user", None), "username", "") or "").casefold()
        if username:
            result["mentions"].add(username)
    result["collaborators"] = [(str(getattr(u, "pk", "") or ""), u.username.casefold())
                              for u in (getattr(media, "coauthor_producers", []) or []) if getattr(u, "username", None)]
    location = getattr(media, "location", None)
    result["location"] = {"instagram_pk": str(getattr(location, "pk", "") or ""),
                          "name": str(getattr(location, "name", "") or ""),
                          "city": str(getattr(location, "city", "") or "")} if location else None
    return result


@transaction.atomic
def save_media(job, media, target, *, detailed=False, stream="timeline"):
    values = parse_media(media, target)
    assets = values.pop("assets")
    hashtags = values.pop("hashtags")
    mentions = values.pop("mentions")
    collaborators = values.pop("collaborators")
    location = values.pop("location")
    owner_profile = {key: values.pop(key) for key in (
        "owner_full_name", "owner_is_private", "owner_is_verified", "owner_profile_pic_url")}
    pk = values.pop("media_pk")
    if values["owner_pk"].isdecimal() and values["owner_username"]:
        values["owner_identity"] = upsert_identity(SimpleNamespace(
            pk=values["owner_pk"], username=values["owner_username"],
            full_name=owner_profile["owner_full_name"],
            is_private=owner_profile["owner_is_private"],
            is_verified=owner_profile["owner_is_verified"],
            profile_pic_url=owner_profile["owner_profile_pic_url"],
        ))[0]
    # A reel discovered on the timeline is still a reel even if its source differs.
    values["is_reel"] = values["is_reel"] or stream == "reels"
    values["detail_status"] = "full" if detailed else "listing"
    post, created = Post.objects.select_for_update().get_or_create(media_pk=pk, defaults=values)
    previous = {field: getattr(post, field) for field in METRIC_FIELDS}
    previous_caption = post.caption_hash
    if not created:
        for field, value in values.items():
            if not detailed and post.detail_status == "full" and field in (
                    "detail_status", "caption", "caption_hash", "published_at", "owner_username", "owner_pk"):
                continue
            if field in METRIC_FIELDS and value is None:
                continue
            setattr(post, field, value)
        post.save()
    snapshot_defaults = {field: getattr(post, field) for field in METRIC_FIELDS}
    snapshot_defaults["caption_hash"] = post.caption_hash
    snapshot_defaults["collected_at"] = timezone.now()
    PostSnapshot.objects.update_or_create(job=job, post=post, defaults=snapshot_defaults)
    if created or any(previous[field] != getattr(post, field) for field in METRIC_FIELDS):
        PostMetricHistory.objects.update_or_create(job=job, post=post,
            defaults={field: getattr(post, field) for field in METRIC_FIELDS})
    if created or detailed or (post.detail_status != "full" and previous_caption != post.caption_hash):
        PostHashtag.objects.filter(post=post).delete()
        PostMention.objects.filter(post=post).delete()
        PostCollaborator.objects.filter(post=post).delete()
        PostHashtag.objects.bulk_create([PostHashtag(post=post, tag=tag) for tag in sorted(hashtags)])
        PostMention.objects.bulk_create([PostMention(post=post, username=name) for name in sorted(mentions)])
        PostCollaborator.objects.bulk_create([PostCollaborator(post=post, instagram_pk=identifier, username=name)
                                              for identifier, name in collaborators])
        if location:
            PostLocation.objects.update_or_create(post=post, defaults=location)
        else:
            PostLocation.objects.filter(post=post).delete()
        PostAsset.objects.filter(post=post).delete()
        PostAsset.objects.bulk_create([PostAsset(post=post, **asset) for asset in assets])
    return post, created


@transaction.atomic
def save_likers(job, post, users):
    """Upsert accessible liker identities and their relation to the post."""
    saved = 0
    for user in users:
        raw_pk = str(getattr(user, "pk", "") or getattr(user, "pk_id", "") or "")
        username = str(getattr(user, "username", "") or "").strip().lower()
        if not raw_pk.isdecimal() or not username:
            continue
        identity = upsert_identity(user)[0]
        PostLike.objects.update_or_create(
            post=post, identity=identity,
            defaults={"source_job": job, "last_seen_at": timezone.now()})
        saved += 1
    return saved
