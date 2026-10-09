"""Stage 8 unified observation orchestration and scheduled profile enrichment."""
from __future__ import annotations

import asyncio
from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from zoneinfo import ZoneInfo

from .account_pool import (
    acquire_operation_account, dispatch_comment_jobs, initial_comment_accounts,
    record_account_failure, record_account_success, release_operation_lease,
)
from .comment_tasks import queue_comments
from .media_tasks import queue_media
from .models import (
    CommentSyncJob, InstagramAccount, InstagramIdentity, MediaSyncJob,
    ObservationJob, Post, PostCommentSyncState, PostSnapshot, PostSyncCursor, PublishItem, PublishJob,
    PublishJobDestination, RelationshipScanJob, RelationshipSnapshot,
    RelationshipSyncState, SiteSettings,
)
from .observation_progress import enabled_task_keys, normalized_task_order
from .publish_tasks import queue_publish
from .relationship_target import known_relationship_target
from .relationship_tasks import queue_relationships
from .session import aiograpi_client

TERMINAL = {"done", "partial", "failed"}


def queue_observation(job, countdown=0):
    orchestrate_observation.apply_async(
        args=[str(job.pk)], countdown=countdown, priority=min(9, max(0, job.priority)))


def _concurrency_available(job):
    config, _ = SiteSettings.objects.get_or_create(owner=job.owner)
    active = ObservationJob.objects.filter(
        owner=job.owner, status=ObservationJob.Status.RUNNING).exclude(pk=job.pk).count()
    return active < max(1, min(10, config.max_concurrent_observations))


def _create_media(job):
    media = MediaSyncJob.objects.create(
        observation=job, owner=job.owner, account=job.account, target=job.target,
        mode=job.mode, max_pages=job.max_pages, date_from=job.date_from,
        date_to=job.date_to)
    PostSyncCursor.objects.bulk_create([
        PostSyncCursor(job=media, stream="timeline", finished=not job.include_posts),
        PostSyncCursor(job=media, stream="reels", finished=not job.include_reels),
    ])
    return media


def _create_relationship(job):
    if not (job.include_followers or job.include_following):
        return None
    seed, target_pk, followers, following = known_relationship_target(job.owner_id, job.target)
    mode = (RelationshipScanJob.Mode.BOTH if job.include_followers and job.include_following
            else RelationshipScanJob.Mode.FOLLOWERS if job.include_followers
            else RelationshipScanJob.Mode.FOLLOWING)
    relation = RelationshipScanJob.objects.create(
        observation=job, owner=job.owner, account=job.account, target=job.target,
        target_pk=target_pk, seed_identity=seed, mode=mode, max_pages=job.max_pages,
        expected_followers=followers, expected_following=following)
    states = []
    if job.include_followers:
        states.append(RelationshipSyncState(
            job=relation, stream=RelationshipSyncState.Stream.FOLLOWERS,
            api="private_gql"))
    if job.include_following:
        states.append(RelationshipSyncState(
            job=relation, stream=RelationshipSyncState.Stream.FOLLOWING,
            api="private_gql"))
    RelationshipSyncState.objects.bulk_create(states)
    return relation


def _create_comments(job, media):
    if media is None and not (job.include_posts or job.include_reels):
        # Comments-only observations intentionally reuse the owner's existing
        # archive. They must not start a hidden media crawl or borrow posts
        # collected by another Django user.
        post_ids = list(PostSnapshot.objects.filter(
            job__owner=job.owner,
            post__owner_username__iexact=job.target,
        ).values_list("post_id", flat=True).distinct())
    else:
        post_ids = list(PostSnapshot.objects.filter(
            job=media).values_list("post_id", flat=True))
    existing = set(CommentSyncJob.objects.filter(
        observation=job, post_id__in=post_ids).values_list("post_id", flat=True))
    posts = list(Post.objects.filter(pk__in=set(post_ids) - existing).order_by("published_at", "id"))
    skipped = 0
    # Archived comment-only jobs do not have a freshly refreshed comment_count;
    # never infer "unchanged" from that stale archive value.
    if job.mode == "incremental" and media is not None and posts:
        cutoff = timezone.now() - timedelta(
            hours=settings.INSTRADAR_COMMENT_INCREMENTAL_REFRESH_HOURS)
        states = {item.post_id: item for item in PostCommentSyncState.objects.filter(
            owner=job.owner, post_id__in=[post.pk for post in posts])}
        pending = []
        for post in posts:
            state = states.get(post.pk)
            unchanged = bool(
                state and state.last_complete_at and state.last_complete_at >= cutoff and
                post.comment_count is not None and
                state.last_observed_count == post.comment_count)
            if unchanged:
                skipped += 1
            else:
                pending.append(post)
        posts = pending
    accounts = initial_comment_accounts(job)
    rows = [CommentSyncJob(
        observation=job, owner=job.owner,
        account=accounts[index % len(accounts)], post=post,
        expected_count=post.comment_count,
    ) for index, post in enumerate(posts)]
    CommentSyncJob.objects.bulk_create(rows, batch_size=250)
    job.comments_scheduled_count = len(rows)
    job.comments_skipped_count = skipped
    job.save(update_fields=[
        "comments_scheduled_count", "comments_skipped_count", "updated_at"])
    return rows, len(post_ids), skipped


def _create_publish(job, media):
    if not job.publish_to_telegram or not job.telegram_account_id:
        return None
    publish = PublishJob.objects.create(
        observation=job, owner=job.owner, telegram_account=job.telegram_account,
        instagram_account=job.account, media_job=media,
        mode=PublishJob.Mode.OBSERVATION, target_username=job.target,
        caption_template=job.caption_template, status=PublishJob.Status.QUEUED)
    destinations = list(job.telegram_destinations.filter(
        account=job.telegram_account, is_active=True, can_post=True))
    PublishJobDestination.objects.bulk_create([
        PublishJobDestination(job=publish, destination=item) for item in destinations])
    posts = list(Post.objects.filter(postsnapshot__job=media).distinct().order_by(
        "published_at", "id"))
    PublishItem.objects.bulk_create([
        PublishItem(job=publish, post=post, sequence=index)
        for index, post in enumerate(posts, start=1)
    ], batch_size=500)
    publish.total_items = len(posts)
    if not posts or not destinations:
        publish.status = PublishJob.Status.FAILED
        publish.error = "محتوا یا مقصد تلگرام برای انتقال آماده نیست"
    publish.save(update_fields=["total_items", "status", "error", "updated_at"])
    return publish


def _finalize(job):
    child_statuses = []
    if hasattr(job, "media_job"):
        child_statuses.append(job.media_job.status)
    if hasattr(job, "relationship_job"):
        child_statuses.append(job.relationship_job.status)
    child_statuses.extend(job.comment_jobs.values_list("status", flat=True))
    if hasattr(job, "publish_job"):
        child_statuses.append(job.publish_job.status)
    job.status = (ObservationJob.Status.FAILED if child_statuses and
                  all(value == "failed" for value in child_statuses)
                  else ObservationJob.Status.PARTIAL if any(
                      value in ("failed", "partial") for value in child_statuses)
                  else ObservationJob.Status.DONE)
    job.current_stage = "complete"
    job.completed_at = timezone.now()
    job.error = "برخی زیرجاب‌ها ناقص یا ناموفق بودند" if job.status == "partial" else ""
    job.save(update_fields=["status", "current_stage", "completed_at", "error", "updated_at"])


def _selected_tasks(job):
    """Return the persisted, stream-level queue order.

    Legacy stage names are expanded by ``normalized_task_order`` so existing
    3.11 rows continue without a data migration.
    """
    return normalized_task_order(job)


def _media(job):
    try:
        return job.media_job
    except MediaSyncJob.DoesNotExist:
        return None


def _relationship(job):
    try:
        return job.relationship_job
    except RelationshipScanJob.DoesNotExist:
        return None


def _publish(job):
    try:
        return job.publish_job
    except PublishJob.DoesNotExist:
        return None


def _selected_media_finished(job, media):
    if not (job.include_posts or job.include_reels):
        return True
    if media is None:
        return False
    names = []
    if job.include_posts:
        names.append("timeline")
    if job.include_reels:
        names.append("reels")
    states = {row.stream: row.finished for row in media.streams.filter(stream__in=names)}
    return bool(names) and all(states.get(name, False) for name in names)


def _queue_selected_media(media, stream_name):
    first = media.streams.filter(finished=False).order_by("id").values_list(
        "stream", flat=True).first()
    if first == stream_name:
        queue_media(media)
    else:
        queue_media(media, stream=stream_name)


def _queue_selected_relationship(relation, stream_name):
    first = relation.streams.filter(finished=False).order_by("id").values_list(
        "stream", flat=True).first()
    if first == stream_name:
        queue_relationships(relation)
    else:
        queue_relationships(relation, stream=stream_name)


@shared_task(name="radar.observation_tasks.orchestrate_observation")
def orchestrate_observation(job_id):
    with transaction.atomic():
        # telegram_account is nullable. Locking an OUTER JOIN fails on
        # PostgreSQL (FOR UPDATE cannot be applied to the nullable side).
        job = ObservationJob.objects.select_for_update(of=("self",)).get(pk=job_id)
        if job.status in (ObservationJob.Status.PAUSED, ObservationJob.Status.ARCHIVED,
                          ObservationJob.Status.DONE, ObservationJob.Status.FAILED):
            return
        if job.status == ObservationJob.Status.QUEUED and not _concurrency_available(job):
            return
        job.status = ObservationJob.Status.RUNNING
        job.started_at = job.started_at or timezone.now()
        job.save(update_fields=["status", "started_at", "updated_at"])

        normalized = _selected_tasks(job)
        if job.stage_order != normalized:
            job.stage_order = normalized
            job.save(update_fields=["stage_order", "updated_at"])

        media = _media(job)
        relation = _relationship(job)
        for task_key in normalized:
            if task_key in ("posts", "reels"):
                if media is None:
                    media = _create_media(job)
                stream_name = "timeline" if task_key == "posts" else "reels"
                stream = media.streams.filter(stream=stream_name).first()
                if stream is None or stream.finished:
                    continue
                if media.status == MediaSyncJob.Status.RUNNING:
                    return
                if media.status in (MediaSyncJob.Status.FAILED,
                                    MediaSyncJob.Status.PARTIAL):
                    continue
                if media.status == MediaSyncJob.Status.PAUSED:
                    return
                job.current_stage = task_key
                job.save(update_fields=["current_stage", "updated_at"])
                transaction.on_commit(
                    lambda item=media, name=stream_name: _queue_selected_media(item, name))
                return

            if task_key in ("followers", "following"):
                if relation is None:
                    relation = _create_relationship(job)
                if relation is None:
                    continue
                state = relation.streams.filter(stream=task_key).first()
                if state is None or state.finished:
                    continue
                if relation.status == RelationshipScanJob.Status.RUNNING:
                    return
                if relation.status in (RelationshipScanJob.Status.FAILED,
                                       RelationshipScanJob.Status.PARTIAL):
                    continue
                if relation.status == RelationshipScanJob.Status.PAUSED:
                    return
                job.current_stage = task_key
                job.save(update_fields=["current_stage", "updated_at"])
                transaction.on_commit(
                    lambda item=relation, name=task_key:
                    _queue_selected_relationship(item, name))
                return

            if task_key == "comments":
                # Comments consume the final set of media.  If the user drags
                # comments above media, the scheduler runs the unmet media
                # dependency first and returns to the requested order later.
                if not _selected_media_finished(job, media):
                    continue
                comments_initialized = bool(
                    job.comments_scheduled_count or job.comments_skipped_count or
                    job.comment_jobs.exists())
                if not comments_initialized:
                    created, source_count, _ = _create_comments(job, media)
                    if not created and not source_count:
                        job.status = ObservationJob.Status.PARTIAL
                        job.current_stage = "complete"
                        job.completed_at = timezone.now()
                        job.error = (
                            "برای این پیج هیچ پست استخراج‌شده‌ای در آرشیو موجود نیست؛ "
                            "ابتدا رصد پست اجرا کنید"
                        )
                        job.save(update_fields=[
                            "status", "current_stage", "completed_at", "error", "updated_at"])
                        return
                if job.comment_jobs.filter(status__in=("queued", "running")).exists():
                    job.current_stage = "comments"
                    job.save(update_fields=["current_stage", "updated_at"])
                    transaction.on_commit(
                        lambda item=job: dispatch_comment_jobs(item, enqueue=queue_comments))
                    return
                if job.comment_jobs.filter(status="paused").exists():
                    return

            if task_key == "telegram":
                if not _selected_media_finished(job, media):
                    continue
                publish = _publish(job)
                if publish is None:
                    publish = _create_publish(job, media)
                if publish and publish.status == PublishJob.Status.QUEUED:
                    job.current_stage = "telegram"
                    job.save(update_fields=["current_stage", "updated_at"])
                    transaction.on_commit(lambda item=publish: queue_publish(item))
                    return
                if publish and publish.status in (PublishJob.Status.RUNNING,
                                                  PublishJob.Status.WAITING,
                                                  PublishJob.Status.PAUSED):
                    return
        _finalize(job)


@shared_task(name="radar.observation_tasks.orchestrate_due_observations")
def orchestrate_due_observations():
    jobs = ObservationJob.objects.filter(status__in=(
        ObservationJob.Status.QUEUED, ObservationJob.Status.RUNNING)).order_by(
            "-priority", "created_at")[:100]
    for job in jobs:
        queue_observation(job)


def _visible_identity_ids(owner_id):
    ids = set(Post.objects.filter(postsnapshot__job__owner_id=owner_id,
                                  owner_identity__isnull=False).values_list(
                                      "owner_identity_id", flat=True)[:5000])
    ids.update(RelationshipSnapshot.objects.filter(job__owner_id=owner_id).values_list(
        "source_id", flat=True)[:5000])
    ids.update(RelationshipSnapshot.objects.filter(job__owner_id=owner_id).values_list(
        "target_id", flat=True)[:5000])
    return ids


@shared_task(name="radar.observation_tasks.enrich_identity")
def enrich_identity(owner_id, identity_id, safety_retries=0):
    identity = InstagramIdentity.objects.get(pk=identity_id)
    account = InstagramAccount.objects.filter(
        owner_id=owner_id, is_active=True, encrypted_cookies__isnull=False,
    ).exclude(health_status=InstagramAccount.Health.QUARANTINED).filter(
        Q(cooldown_until__isnull=True) | Q(cooldown_until__lte=timezone.now())
    ).order_by("last_request_at", "created_at").first()
    if account is None:
        return

    lease, unavailable = acquire_operation_account(
        account.pk, None, "enrichment", settings.INSTRADAR_JOB_TIMEOUT + 60,
        cost=1)
    if unavailable:
        if safety_retries < 3:
            enrich_identity.apply_async(
                args=[owner_id, identity_id, safety_retries + 1],
                countdown=unavailable.wait_seconds, priority=1)
        return

    async def fetch():
        client = await aiograpi_client(account)
        return await client.user_info(identity.instagram_pk)

    try:
        with asyncio.Runner() as runner:
            profile = runner.run(fetch())
        record_account_success(account.pk)
    except Exception as exc:
        record_account_failure(
            account.pk, None, exc, f"{type(exc).__name__}: enrichment ناموفق بود")
        return
    finally:
        release_operation_lease(lease)
    InstagramIdentity.objects.filter(pk=identity.pk).update(
        current_username=getattr(profile, "username", identity.current_username),
        full_name=getattr(profile, "full_name", "") or "",
        biography=getattr(profile, "biography", "") or "",
        profile_pic_url=str(getattr(profile, "profile_pic_url_hd", "") or
                            getattr(profile, "profile_pic_url", "") or ""),
        followers_count=getattr(profile, "follower_count", None),
        following_count=getattr(profile, "following_count", None),
        media_count=getattr(profile, "media_count", None), enriched_at=timezone.now(),
        last_seen_at=timezone.now())


@shared_task(name="radar.observation_tasks.enrichment_tick")
def enrichment_tick():
    local = timezone.now().astimezone(ZoneInfo("Asia/Tehran"))
    for config in SiteSettings.objects.select_related("owner").filter(enrichment_enabled=True):
        if config.last_enrichment_date == local.date():
            continue
        if (local.hour, local.minute) < (config.enrichment_hour, config.enrichment_minute):
            continue
        ids = _visible_identity_ids(config.owner_id)
        missing = InstagramIdentity.objects.filter(pk__in=ids).filter(
            Q(biography="") | Q(followers_count__isnull=True) |
            Q(following_count__isnull=True) | Q(media_count__isnull=True)
        ).order_by("enriched_at")[:settings.INSTRADAR_ENRICHMENT_BATCH_SIZE]
        for index, identity_id in enumerate(missing.values_list("pk", flat=True)):
            enrich_identity.apply_async(
                args=[config.owner_id, identity_id], priority=1,
                countdown=index * settings.INSTRADAR_ENRICHMENT_SPACING_SECONDS)
        config.last_enrichment_date = local.date()
        config.save(update_fields=["last_enrichment_date", "updated_at"])
