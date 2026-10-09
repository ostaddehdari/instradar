from django.db import transaction
from django.utils import timezone
from .models import CrawlSession, CrawlCursor, ExtractionProgress, ProfileDiscoveryState


def ensure_session(observation):
    session, _ = CrawlSession.objects.get_or_create(
        observation=observation,
        defaults={"account": observation.account, "target": observation.target},
    )
    changed = []
    if session.account_id != observation.account_id:
        session.account_id = observation.account_id; changed.append("account")
    if session.target != observation.target:
        session.target = observation.target; changed.append("target")
    if changed:
        session.save(update_fields=changed + ["updated_at"])
    return session


def ensure_progress(session, stream, *, expected=None):
    row, _ = ExtractionProgress.objects.get_or_create(session=session, stream=stream)
    if expected is not None and row.expected != expected:
        row.expected = expected
        row.save(update_fields=["expected", "updated_at"])
    return row


def checkpoint(session, stream, *, cursor=None, last_item_id="", last_media_id="", pages=None, extracted=None, expected=None, finished=None, issue="", status=None):
    defaults = {}
    if cursor is not None: defaults["cursor"] = str(cursor or "")
    if last_item_id: defaults["last_item_id"] = str(last_item_id)
    if last_media_id: defaults["last_media_id"] = str(last_media_id)
    if pages is not None: defaults["pages"] = pages
    if extracted is not None: defaults["extracted"] = extracted
    if expected is not None: defaults["total_expected"] = expected
    if finished is not None: defaults["finished"] = finished
    if issue: defaults["issue"] = issue
    cursor_row, _ = CrawlCursor.objects.get_or_create(session=session, stream=stream)
    for key, value in defaults.items(): setattr(cursor_row, key, value)
    cursor_row.save()
    progress = ensure_progress(session, stream, expected=expected)
    if extracted is not None: progress.extracted = extracted
    if pages is not None: progress.pages = pages
    if finished is not None: progress.status = "done" if finished else (status or "running")
    elif status: progress.status = status
    progress.last_crawl_at = timezone.now()
    progress.save()
    session.last_crawl_at = timezone.now()
    if finished is True: session.status = "complete"
    elif status == "failed": session.status = "failed"
    else: session.status = "running"
    session.save(update_fields=["last_crawl_at", "status", "updated_at"])
    return cursor_row, progress


def save_profile_discovery(owner_id, profile):
    username = str(getattr(profile, "username", "") or "").strip().lower()
    if not username: return None
    return ProfileDiscoveryState.objects.create(
        owner_id=owner_id, username=username, instagram_pk=str(getattr(profile, "pk", "") or ""),
        full_name=str(getattr(profile, "full_name", "") or "")[:250],
        is_private=bool(getattr(profile, "is_private", False)),
        is_verified=bool(getattr(profile, "is_verified", False)),
        posts_count=int(getattr(profile, "media_count", getattr(profile, "posts_count", 0)) or 0),
        reels_count=(int(getattr(profile, "clips_count", getattr(profile, "reels_count", 0)))
                     if getattr(profile, "clips_count", getattr(profile, "reels_count", None)) is not None else None),
        followers_count=int(getattr(profile, "follower_count", 0) or 0),
        following_count=int(getattr(profile, "following_count", 0) or 0),
    )


def save_profile_to_session(session, profile):
    session.instagram_pk = str(getattr(profile, "pk", "") or "")
    session.profile_username = str(getattr(profile, "username", "") or "").strip().lstrip("@").lower()
    session.profile_full_name = str(getattr(profile, "full_name", "") or "")[:250]
    session.profile_is_private = bool(getattr(profile, "is_private", False))
    session.profile_is_verified = bool(getattr(profile, "is_verified", False))
    session.profile_followers_count = getattr(profile, "follower_count", None)
    session.profile_following_count = getattr(profile, "following_count", None)
    session.profile_posts_count = getattr(profile, "media_count", getattr(profile, "posts_count", None))
    session.profile_reels_count = getattr(profile, "clips_count", getattr(profile, "reels_count", None))
    session.status = "profile_captured"
    session.save(update_fields=[
        "instagram_pk", "profile_username", "profile_full_name", "profile_is_private",
        "profile_is_verified", "profile_followers_count", "profile_following_count",
        "profile_posts_count", "profile_reels_count", "status", "updated_at",
    ])
    return session
