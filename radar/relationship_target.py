"""Owner-scoped reuse of stable Instagram IDs discovered by earlier crawls."""
from types import SimpleNamespace

from django.db.models import Q

from .models import InstagramIdentity, MediaSyncJob, Post, ProfileSnapshot
from .relationship_store import upsert_identity


def known_relationship_target(owner_id, target):
    profile = ProfileSnapshot.objects.filter(
        job__owner_id=owner_id, username__iexact=target).order_by(
            "-collected_at").first()
    identity = InstagramIdentity.objects.filter(
        Q(source_snapshots__job__owner_id=owner_id) |
        Q(target_snapshots__job__owner_id=owner_id) |
        Q(posts__postsnapshot__job__owner_id=owner_id) |
        Q(comments__post__postsnapshot__job__owner_id=owner_id)
    ).filter(
        Q(current_username__iexact=target) | Q(aliases__username__iexact=target)
    ).distinct().first()
    target_pk = str(identity.instagram_pk) if identity else ""
    if profile and str(profile.instagram_pk).isdecimal():
        target_pk = str(profile.instagram_pk)
    if not target_pk:
        target_pk = (MediaSyncJob.objects.filter(
            owner_id=owner_id, target__iexact=target).exclude(target_pk="").order_by(
                "-updated_at").values_list("target_pk", flat=True).first() or "")
    if not target_pk:
        target_pk = (Post.objects.filter(
            postsnapshot__job__owner_id=owner_id, owner_username__iexact=target,
            owner_pk__regex=r"^[0-9]+$").order_by("-updated_at").values_list(
                "owner_pk", flat=True).first() or "")
    if target_pk and target_pk.isdecimal() and identity is None:
        identity = upsert_identity(SimpleNamespace(
            pk=target_pk, username=target, full_name="", is_private=False,
            is_verified=False, profile_pic_url=""))[0]
    return (identity, target_pk if target_pk.isdecimal() else "",
            profile.followers_count if profile else None,
            profile.following_count if profile else None)
