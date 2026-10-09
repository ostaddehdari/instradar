"""Stage 7 PostgreSQL-checkpointed Telegram publication queue."""
from __future__ import annotations

import time
from datetime import timedelta
from pathlib import Path

from celery import shared_task
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from redis import Redis

from .download_store import download_post, storage_root
from .models import (DownloadItem, DownloadJob, MediaAsset, MediaSyncJob, PublishAttempt,
                     PublishItem, PublishJob, StorageVolume, TelegramMessage)
from .telegram_service import TelegramServiceError, send_media


def queue_publish(job, countdown=0):
    publish_next.apply_async(args=[str(job.pk)], countdown=countdown, priority=1)


def _volume():
    volume, _ = StorageVolume.objects.get_or_create(
        root_path=str(settings.INSTRADAR_STORAGE_ROOT),
        defaults={"name": "فضای اصلی تلگرام", "reserved_bytes": 0})
    return volume


def _local_media(job, item, *, download=True):
    assets = list(MediaAsset.objects.filter(owner=job.owner, post=item.post)
                  .select_related("item__job__volume").order_by("position"))
    valid = []
    for asset in assets:
        root = storage_root(asset.item.job) if asset.item_id else settings.INSTRADAR_STORAGE_ROOT
        path = (Path(root) / asset.local_path).resolve()
        if path.is_file():
            valid.append((asset, path))
    if valid:
        return [path for _, path in valid]
    if not download:
        return []

    download_job = DownloadJob.objects.create(
        owner=job.owner, account=job.instagram_account, volume=_volume(), mode="single",
        target_username=item.post.owner_username, status=DownloadJob.Status.RUNNING,
        total_items=1, started_at=timezone.now())
    download_item = DownloadItem.objects.create(
        job=download_job, post=item.post, status=DownloadItem.Status.RUNNING,
        attempts=1, started_at=timezone.now())
    try:
        downloaded, byte_count = download_post(download_item)
    except Exception as exc:
        download_item.status = DownloadItem.Status.FAILED
        download_item.error = f"{type(exc).__name__}: {exc}"[:500]
        download_item.completed_at = timezone.now()
        download_item.save()
        download_job.status = DownloadJob.Status.FAILED
        download_job.failed_items = 1
        download_job.error = download_item.error
        download_job.completed_at = timezone.now()
        download_job.save()
        raise
    download_item.status = DownloadItem.Status.DONE
    download_item.bytes_downloaded = byte_count
    download_item.completed_at = timezone.now()
    download_item.save()
    download_job.status = DownloadJob.Status.DONE
    download_job.completed_items = 1
    download_job.bytes_downloaded = byte_count
    download_job.completed_at = timezone.now()
    download_job.save()
    root = storage_root(download_job)
    return [(Path(root) / asset.local_path).resolve() for asset in downloaded]


def _totals(job):
    completed = job.items.filter(status__in=(PublishItem.Status.DONE,
                                              PublishItem.Status.SKIPPED)).count()
    failed = job.items.filter(status=PublishItem.Status.FAILED).count()
    return completed, failed


@shared_task(name="radar.publish_tasks.prepare_observation_publish", acks_late=True)
def prepare_observation_publish(media_job_id):
    try:
        media_job = MediaSyncJob.objects.get(pk=media_job_id)
        with transaction.atomic():
            job = PublishJob.objects.select_for_update().get(media_job=media_job)
            if job.status != PublishJob.Status.WAITING:
                return
    except (MediaSyncJob.DoesNotExist, PublishJob.DoesNotExist):
        return
    post_ids = list(media_job.postsnapshot_set.order_by(
        "post__published_at", "post_id").values_list("post_id", flat=True))
    PublishItem.objects.bulk_create(
        [PublishItem(job=job, post_id=post_id, sequence=index)
         for index, post_id in enumerate(post_ids, 1)], ignore_conflicts=True, batch_size=500)
    job.total_items = job.items.count()
    job.status = PublishJob.Status.QUEUED if job.total_items else PublishJob.Status.DONE
    job.completed_at = timezone.now() if not job.total_items else None
    job.error = "رصد محتوایی نداشت" if not job.total_items else ""
    job.save()
    if job.total_items:
        queue_publish(job)


@shared_task(name="radar.publish_tasks.publish_next", acks_late=True)
def publish_next(job_id):
    now = timezone.now()
    with transaction.atomic():
        job = PublishJob.objects.select_for_update().select_related(
            "telegram_account", "instagram_account").get(pk=job_id)
        if job.status in (PublishJob.Status.WAITING, PublishJob.Status.PAUSED,
                          PublishJob.Status.CANCELLED, PublishJob.Status.DONE):
            return
        if job.status == PublishJob.Status.RUNNING and job.lease_until and job.lease_until > now:
            return
        item = job.items.select_for_update().filter(status=PublishItem.Status.QUEUED).order_by(
            "sequence", "id").first()
        if item is None:
            completed, failed = _totals(job)
            job.completed_items, job.failed_items = completed, failed
            job.status = (PublishJob.Status.DONE if completed == job.total_items else
                          PublishJob.Status.PARTIAL if completed else PublishJob.Status.FAILED)
            job.error = f"{failed} محتوا ارسال نشد" if failed else ""
            job.lease_until = None
            job.completed_at = now
            job.save()
            return
        job.status = PublishJob.Status.RUNNING
        job.started_at = job.started_at or now
        job.heartbeat_at = now
        job.lease_until = now + timedelta(seconds=settings.INSTRADAR_TELEGRAM_TIMEOUT + 60)
        job.save()
        item.status = PublishItem.Status.RUNNING
        item.attempts += 1
        item.started_at = now
        item.error = ""
        item.save()
        item_id, attempt_number = item.pk, item.attempts

    started = time.monotonic()
    status, error = PublishItem.Status.DONE, ""
    successful, skipped, retry_after = 0, 0, 0
    try:
        item = PublishItem.objects.select_related(
            "job__owner", "job__telegram_account__owner", "job__instagram_account", "post"
        ).get(pk=item_id)
        job = item.job
        local_paths = _local_media(job, item, download=False)
        remote_urls = [(asset.video_url or asset.preview_url) for asset in
                       item.post.assets.order_by("position") if
                       (asset.video_url or asset.preview_url)]
        destinations = [link.destination for link in job.destination_links.select_related(
            "destination").filter(destination__is_active=True, destination__can_post=True)]
        if not destinations:
            raise RuntimeError("هیچ مقصد فعال و قابل ارسالی انتخاب نشده است")
        for destination in destinations:
            if (not job.force_resend and
                    TelegramMessage.objects.filter(destination=destination, post=item.post).exists()):
                skipped += 1
                continue
            try:
                message_ids = send_media(job.telegram_account, destination, item.post,
                                         remote_urls, local_paths, job.caption_template)
            except TelegramServiceError as exc:
                if local_paths or "fallback" not in str(exc):
                    raise
                # The direct URL attempt from the supplied reference code was
                # rejected. Only now download through gallery-dl and upload.
                local_paths = _local_media(job, item, download=True)
                message_ids = send_media(job.telegram_account, destination, item.post,
                                         [], local_paths, job.caption_template)
            TelegramMessage.objects.create(job=job, item=item, destination=destination,
                                           post=item.post, message_ids=message_ids)
            successful += 1
        if skipped == len(destinations):
            status = PublishItem.Status.SKIPPED
    except Exception as exc:
        retry_after = max(0, int(getattr(exc, "seconds", 0) or 0))
        error = f"{type(exc).__name__}: {exc}"[:500]
        status = (PublishItem.Status.QUEUED
                  if attempt_number < settings.INSTRADAR_TELEGRAM_MAX_RETRIES
                  else PublishItem.Status.FAILED)
    duration_ms = int((time.monotonic() - started) * 1000)

    with transaction.atomic():
        job = PublishJob.objects.select_for_update().get(pk=job_id)
        item = PublishItem.objects.select_for_update().get(pk=item_id)
        item.status, item.error = status, error
        item.completed_at = (timezone.now() if status in (PublishItem.Status.DONE,
                                                          PublishItem.Status.SKIPPED,
                                                          PublishItem.Status.FAILED) else None)
        item.save()
        PublishAttempt.objects.update_or_create(
            item=item, attempt_number=attempt_number,
            defaults={"status": status, "duration_ms": duration_ms,
                      "message": error or f"{successful} مقصد ارسال شد؛ {skipped} تکراری رد شد"})
        completed, failed = _totals(job)
        job.completed_items, job.failed_items = completed, failed
        job.lease_until = None
        job.heartbeat_at = timezone.now()
        queued = job.items.filter(status=PublishItem.Status.QUEUED).exists()
        held = job.status in (PublishJob.Status.PAUSED, PublishJob.Status.CANCELLED)
        if not held and not queued:
            job.status = (PublishJob.Status.DONE if completed == job.total_items else
                          PublishJob.Status.PARTIAL if completed else PublishJob.Status.FAILED)
            job.completed_at = timezone.now()
            job.error = f"{failed} محتوا ارسال نشد" if failed else ""
        elif not held:
            job.status = PublishJob.Status.QUEUED
            job.error = error if status == PublishItem.Status.QUEUED else ""
        job.save()
        if not held and not queued and job.observation_id:
            from .observation_tasks import queue_observation
            transaction.on_commit(lambda: queue_observation(job.observation))
    if queued and not held and job.status == PublishJob.Status.QUEUED:
        delay = max(retry_after,
                    settings.INSTRADAR_TELEGRAM_BACKOFF_BASE * (2 ** (attempt_number - 1))
                 if status == PublishItem.Status.QUEUED else 0)
        if job.observation_id:
            from .observation_tasks import queue_observation
            queue_observation(job.observation, countdown=min(delay, 3600))
        else:
            queue_publish(job, countdown=min(delay, 3600))


@shared_task(name="radar.publish_tasks.recover_publish_jobs")
def recover_publish_jobs():
    now = timezone.now()
    for job in PublishJob.objects.filter(status=PublishJob.Status.RUNNING,
                                         lease_until__lt=now)[:50]:
        with transaction.atomic():
            locked = PublishJob.objects.select_for_update().get(pk=job.pk)
            if locked.status != PublishJob.Status.RUNNING or not locked.lease_until or locked.lease_until >= now:
                continue
            locked.items.filter(status=PublishItem.Status.RUNNING).update(status=PublishItem.Status.QUEUED)
            locked.status, locked.lease_until = PublishJob.Status.QUEUED, None
            locked.error = "Worker تلگرام متوقف شده بود؛ از آخرین پست ادامه می‌دهد"
            locked.save()
        if locked.observation_id:
            from .observation_tasks import queue_observation
            queue_observation(locked.observation)
        else:
            queue_publish(locked)
    for job in PublishJob.objects.filter(status=PublishJob.Status.QUEUED).order_by("created_at")[:20]:
        if job.observation_id:
            from .observation_tasks import queue_observation
            queue_observation(job.observation)
        else:
            queue_publish(job)


@shared_task(name="radar.publish_tasks.telegram_worker_heartbeat")
def telegram_worker_heartbeat():
    Redis.from_url(settings.CELERY_BROKER_URL, socket_timeout=2).set(
        "instradar:telegram:heartbeat", timezone.now().isoformat(), ex=90)
