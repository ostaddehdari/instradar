"""Normalize aiograpi Comment objects and commit each successfully fetched page."""
import re
from types import SimpleNamespace
from django.db import transaction
from django.utils import timezone
from .models import Comment, CommentMention, Contact, ReplySyncCursor
from .relationship_store import upsert_identity

MENTION = re.compile(r"(?<![\w@])@([A-Za-z0-9._]{1,100})")


def normalize(raw, parent_id=""):
    author = getattr(raw, "user", None)
    pk = str(getattr(raw, "pk", "") or "")
    if not pk:
        raise ValueError("MissingCommentId")
    return {
        "instagram_pk": pk,
        "parent_instagram_pk": parent_id or str(getattr(raw, "replied_to_comment_id", "") or ""),
        "author_pk": str(getattr(author, "pk", "") or ""),
        "author_username": str(getattr(author, "username", "") or "")[:100],
        "author_full_name": str(getattr(author, "full_name", "") or "")[:250],
        "author_is_private": bool(getattr(author, "is_private", False)),
        "author_is_verified": bool(getattr(author, "is_verified", False)),
        "author_profile_pic_url": str(getattr(author, "profile_pic_url", "") or ""),
        "text": str(getattr(raw, "text", "") or ""),
        "like_count": getattr(raw, "like_count", None),
        "published_at": getattr(raw, "created_at_utc", None),
        "reply_count": getattr(raw, "child_comment_count", None),
    }


@transaction.atomic
def save_page(job, rows, *, parent=None):
    """Idempotent upsert; only create reply cursors for top-level comments."""
    if parent and parent.post_id != job.post_id:
        raise ValueError("ReplyParentPostMismatch")
    count = 0
    for data in rows:
        comment_id = data["instagram_pk"]
        existing = Comment.objects.filter(instagram_pk=comment_id).first()
        if existing and existing.post_id != job.post_id:
            raise ValueError("CommentPostMismatch")
        parent_id = data["parent_instagram_pk"]
        parent_obj = parent or (Comment.objects.filter(post=job.post, instagram_pk=parent_id).first() if parent_id else None)
        defaults = {key: data[key] for key in ("author_pk", "author_username", "text", "like_count", "published_at", "parent_instagram_pk")}
        commenter_identity = None
        if data["author_pk"].isdecimal() and data["author_username"]:
            commenter_identity = upsert_identity(SimpleNamespace(
                pk=data["author_pk"], username=data["author_username"],
                full_name=data["author_full_name"], is_private=data["author_is_private"],
                is_verified=data["author_is_verified"],
                profile_pic_url=data["author_profile_pic_url"],
            ))[0]
            defaults["author_identity"] = commenter_identity
            Contact.objects.update_or_create(
                owner=job.owner, identity=commenter_identity,
                defaults={"username": commenter_identity.current_username,
                          "last_seen_at": timezone.now()},
            )
        defaults.update(parent=parent_obj, last_seen_at=timezone.now(), last_seen_job=job, unavailable=False)
        obj, _ = Comment.objects.update_or_create(instagram_pk=comment_id, defaults={"post": job.post, **defaults})
        tags = set(MENTION.findall(data["text"]))
        CommentMention.objects.filter(comment=obj).exclude(username__in=tags).delete()
        CommentMention.objects.bulk_create([CommentMention(comment=obj, username=tag) for tag in tags], ignore_conflicts=True)
        if not parent and data.get("reply_count"):
            ReplySyncCursor.objects.update_or_create(job=job, parent=obj,
                defaults={"expected_count": data["reply_count"]}, create_defaults={"expected_count": data["reply_count"]})
        count += 1
    return count
