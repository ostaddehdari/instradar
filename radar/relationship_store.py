"""Canonical identity and directed follow-edge persistence for Stage 4."""
from __future__ import annotations

import re

from django.db import transaction
from django.utils import timezone

from .models import (AccountAlias, AccountRelationship, InstagramIdentity,
                     RelationshipChange, RelationshipSnapshot,
                     RelationshipSyncState, Contact, FollowEdge, FollowerSnapshot)

USERNAME_RE = re.compile(r"^[A-Za-z0-9._]{1,100}$")


def normalize_identity(raw):
    instagram_pk = str(getattr(raw, "pk", "") or "")
    username = str(getattr(raw, "username", "") or "").strip().lower()
    if not instagram_pk.isdecimal() or not USERNAME_RE.fullmatch(username):
        raise ValueError("Invalid Instagram identity")
    return {
        "instagram_pk": instagram_pk,
        "current_username": username,
        "full_name": str(getattr(raw, "full_name", "") or "")[:250],
        "is_private": bool(getattr(raw, "is_private", False)),
        "is_verified": bool(getattr(raw, "is_verified", False)),
        "profile_pic_url": str(getattr(raw, "profile_pic_url", "") or ""),
        "followers_count": getattr(raw, "follower_count", getattr(raw, "followers_count", None)),
        "following_count": getattr(raw, "following_count", getattr(raw, "followings_count", None)),
        "media_count": getattr(raw, "media_count", getattr(raw, "posts_count", None)),
    }


@transaction.atomic
def upsert_identity(raw, *, seen_at=None):
    values = normalize_identity(raw)
    seen_at = seen_at or timezone.now()
    instagram_pk = values.pop("instagram_pk")
    identity, created = InstagramIdentity.objects.select_for_update().get_or_create(
        instagram_pk=instagram_pk,
        defaults={**values, "first_seen_at": seen_at, "last_seen_at": seen_at},
    )
    previous_username = identity.current_username
    if not created:
        for field, value in values.items():
            if value not in (None, "") or field in ("is_private", "is_verified"):
                if field.endswith("_count") and value is not None:
                    value = max(0, int(value))
                setattr(identity, field, value)
        identity.last_seen_at = seen_at
        identity.save()
    for username in {previous_username, identity.current_username} - {""}:
        alias, _ = AccountAlias.objects.get_or_create(
            identity=identity, username=username,
            defaults={"first_seen_at": seen_at, "last_seen_at": seen_at},
        )
        if alias.last_seen_at != seen_at:
            alias.last_seen_at = seen_at
            alias.save(update_fields=["last_seen_at"])
    return identity, created, previous_username != identity.current_username


@transaction.atomic
def save_relationship_page(job, stream, users):
    """Upsert one API page. A follower edge is user -> seed; following is seed -> user."""
    if not job.seed_identity_id:
        raise ValueError("Relationship job has no seed identity")
    now = timezone.now()
    written = 0
    renamed = 0
    for raw in users:
        identity, _, was_renamed = upsert_identity(raw, seen_at=now)
        renamed += int(was_renamed)
        if identity.pk == job.seed_identity_id:
            continue
        if stream == RelationshipSyncState.Stream.FOLLOWERS:
            source, target = identity, job.seed_identity
        elif stream == RelationshipSyncState.Stream.FOLLOWING:
            source, target = job.seed_identity, identity
        else:
            raise ValueError("Unknown relationship stream")
        relationship, created = AccountRelationship.objects.select_for_update().get_or_create(
            source=source, target=target, relation_type=AccountRelationship.Type.FOLLOWS,
            defaults={"first_seen_at": now, "last_seen_at": now, "depth": job.depth,
                      "discovered_from": job.seed_identity, "last_seen_job": job},
        )
        change_type = None
        if created:
            change_type = RelationshipChange.Type.FOLLOWED
        else:
            if not relationship.is_active:
                change_type = RelationshipChange.Type.REACTIVATED
            relationship.last_seen_at = now
            relationship.ended_at = None
            relationship.is_active = True
            relationship.depth = min(relationship.depth, job.depth)
            relationship.discovered_from = job.seed_identity
            relationship.last_seen_job = job
            relationship.save()
        Contact.objects.update_or_create(
            owner=job.owner, identity=identity,
            defaults={"username": identity.current_username, "last_seen_at": now},
        )
        edge, _ = FollowEdge.objects.get_or_create(
            source=source, target=target,
            defaults={"first_seen_at": now, "last_seen_at": now, "last_session": None},
        )
        edge.last_seen_at = now
        edge.is_active = True
        edge.save(update_fields=["last_seen_at", "is_active", "last_session"])
        if job.observation_id:
            from .crawl_state import ensure_session
            session = ensure_session(job.observation)
            edge.last_session_id = session.pk
            edge.save(update_fields=["last_session"])
            FollowerSnapshot.objects.update_or_create(
                session=session, stream=stream, edge=edge,
                defaults={"observed_at": now},
            )
        RelationshipSnapshot.objects.update_or_create(
            job=job, relationship=relationship,
            defaults={"source": source, "target": target,
                      "relation_type": AccountRelationship.Type.FOLLOWS, "observed_at": now},
        )
        if change_type:
            RelationshipChange.objects.get_or_create(
                job=job, relationship=relationship, change_type=change_type,
                defaults={"happened_at": now},
            )
        written += 1
    return written, renamed


@transaction.atomic
def reconcile_relationship_stream(job, state):
    """End missing edges only after a demonstrably complete scan."""
    if state.reconciled:
        return 0
    if not state.finished or state.issue:
        return 0
    expected = job.expected_followers if state.stream == state.Stream.FOLLOWERS else job.expected_following
    observed = RelationshipSnapshot.objects.filter(job=job)
    if state.stream == state.Stream.FOLLOWERS:
        observed = observed.filter(relationship__target=job.seed_identity)
    else:
        observed = observed.filter(relationship__source=job.seed_identity)
    observed_count = observed.count()
    if expected is None or observed_count < expected:
        state.issue = (f"فقط {observed_count} رابطه از شمارندهٔ "
                       f"{expected if expected is not None else 'نامشخص'} دیده شد")
        state.save(update_fields=["issue"])
        return 0
    if state.stream == state.Stream.FOLLOWERS:
        candidates = AccountRelationship.objects.filter(
            target=job.seed_identity, relation_type=AccountRelationship.Type.FOLLOWS, is_active=True)
    else:
        candidates = AccountRelationship.objects.filter(
            source=job.seed_identity, relation_type=AccountRelationship.Type.FOLLOWS, is_active=True)
    seen_ids = RelationshipSnapshot.objects.filter(job=job).values_list("relationship_id", flat=True)
    missing = list(candidates.exclude(pk__in=seen_ids))
    now = timezone.now()
    for relationship in missing:
        relationship.is_active = False
        relationship.ended_at = now
        relationship.save(update_fields=["is_active", "ended_at"])
        RelationshipChange.objects.get_or_create(
            job=job, relationship=relationship,
            change_type=RelationshipChange.Type.UNFOLLOWED,
            defaults={"happened_at": now},
        )
    state.reconciled = True
    state.save(update_fields=["reconciled"])
    return len(missing)
