import asyncio
import random
import time
from datetime import timedelta
from celery import shared_task
from celery.signals import heartbeat_sent
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from .account_pool import (
    acquire_operation_account, instagram_safety_signal, record_account_failure, record_account_success,
    release_operation_lease,
)
from .crypto import seal
from .models import CrawlJob, CrawlMetric, InstagramAccount, ProfileSnapshot
from .session import InstagramSessionError, aiograpi_client
from .parsers import parse_profile


async def _fetch_profile(account, target):
    client = await aiograpi_client(account)
    await asyncio.sleep(random.randint(settings.INSTRADAR_MIN_DELAY_MS,
                                     settings.INSTRADAR_MAX_DELAY_MS) / 1000)
    profile = parse_profile(await client.user_info_by_username(target))
    return profile, client.get_settings()


def enqueue(job, countdown=0):
    crawl_profile.apply_async(args=[str(job.pk)], soft_time_limit=settings.INSTRADAR_JOB_TIMEOUT,
                        time_limit=settings.INSTRADAR_JOB_TIMEOUT + 30, countdown=countdown)


def _record_worker_heartbeat():
    from redis import Redis
    Redis.from_url(settings.CELERY_BROKER_URL).set("instradar:worker:heartbeat", "1", ex=90)


@heartbeat_sent.connect
def worker_process_heartbeat(**kwargs):
    _record_worker_heartbeat()


@shared_task(name="radar.tasks.worker_heartbeat")
def worker_heartbeat():
    _record_worker_heartbeat()


@shared_task(name="radar.tasks.recover_jobs")
def recover_jobs():
    now = timezone.now()
    # Recovery is idempotent: duplicate messages cannot claim an already running row.
    for job in CrawlJob.objects.filter(status=CrawlJob.Status.RUNNING, lease_until__lt=now)[:100]:
        with transaction.atomic():
            claimed = CrawlJob.objects.select_for_update().get(pk=job.pk)
            if claimed.status != CrawlJob.Status.RUNNING or claimed.lease_until >= now:
                continue
            claimed.status = CrawlJob.Status.QUEUED if claimed.attempts < settings.INSTRADAR_MAX_RETRIES else CrawlJob.Status.FAILED
            claimed.error = "Lease expired; resuming from saved cursor" if claimed.status == CrawlJob.Status.QUEUED else "Retry limit reached"
            claimed.save(update_fields=["status", "error", "updated_at"])
        if claimed.status == CrawlJob.Status.QUEUED:
            enqueue(claimed)
    # Also reclaim jobs left queued when broker lost a message during restart.
    for job in CrawlJob.objects.filter(status=CrawlJob.Status.QUEUED, updated_at__lt=now - timedelta(minutes=2))[:100]:
        enqueue(job)
        CrawlJob.objects.filter(pk=job.pk, status=CrawlJob.Status.QUEUED).update(updated_at=now)


@shared_task(name="radar.tasks.crawl_profile")
def crawl_profile(job_id):
    start = time.monotonic()
    operation_lease = None
    with transaction.atomic():
        job = CrawlJob.objects.select_for_update().select_related("account").get(pk=job_id)
        if job.status != CrawlJob.Status.QUEUED or (job.lease_until and job.lease_until > timezone.now()):
            return
        operation_lease, unavailable = acquire_operation_account(
            job.account_id, None, "profile", settings.INSTRADAR_JOB_TIMEOUT + 60,
            cost=1)
        if unavailable:
            job.lease_until = timezone.now() + timedelta(
                seconds=unavailable.wait_seconds)
            job.save(update_fields=["lease_until", "updated_at"])
            transaction.on_commit(lambda: enqueue(
                job, countdown=unavailable.wait_seconds))
            return
        job.status = CrawlJob.Status.RUNNING
        job.attempts += 1
        job.heartbeat_at = timezone.now()
        job.lease_until = timezone.now() + timedelta(seconds=settings.INSTRADAR_JOB_TIMEOUT + 30)
        job.save(update_fields=["status", "attempts", "heartbeat_at", "lease_until", "updated_at"])
    try:
        account = job.account
        profile, client_settings = asyncio.run(_fetch_profile(account, job.target))
        record_account_success(job.account_id)
        if job.stage == "profile":
            with transaction.atomic():
                current = CrawlJob.objects.select_for_update().get(pk=job.pk)
                if current.status != CrawlJob.Status.RUNNING:
                    return
                ProfileSnapshot.objects.update_or_create(job=current, instagram_pk=profile["instagram_pk"], defaults={
                    "username": profile["username"], "full_name": profile["full_name"],
                    "is_private": profile["is_private"], "is_verified": profile["is_verified"],
                    "followers_count": profile["followers_count"], "following_count": profile["following_count"],
                    "media_count": profile["posts_count"], "posts_count": profile["posts_count"],
                    "reels_count": profile["reels_count"]})
                account.encrypted_aiograpi_settings = seal(client_settings)
                account.status = "active"
                account.last_error = ""
                account.verified_at = timezone.now()
                account.save(update_fields=["encrypted_aiograpi_settings", "status", "last_error", "verified_at"])
                current.cursor = {"profile_pk": profile["instagram_pk"], "profile_done": True}
                current.stage = "done"
                current.status = CrawlJob.Status.DONE
                current.heartbeat_at = timezone.now()
                current.save(update_fields=["cursor", "stage", "status", "heartbeat_at", "updated_at"])
            CrawlMetric.objects.create(job=job, stage="profile", status="done", records=1,
                                       duration_ms=int((time.monotonic()-start)*1000))
    except Exception as exc:
        # Do not log exception contents: upstream errors may include credentials/cookies.
        reason = (str(exc) if isinstance(exc, InstagramSessionError)
                  else f"{type(exc).__name__}: اتصال نشست یا دریافت پروفایل ناموفق بود")
        record_account_failure(job.account_id, None, exc, reason)
        safety_signal = instagram_safety_signal(exc)
        permanent = safety_signal == "login" or (
            safety_signal not in ("feedback", "rate") and (
                getattr(exc, "permanent", False) or
                isinstance(exc, (ValueError, AssertionError)) or
                (isinstance(exc, RuntimeError) and str(exc) in (
                    "SessionRejected", "LoggedIntoDifferentAccount"))))
        retry = not permanent and job.attempts < settings.INSTRADAR_MAX_RETRIES
        delay = min(settings.INSTRADAR_BACKOFF_MAX, settings.INSTRADAR_BACKOFF_BASE * (2 ** (job.attempts - 1)))
        CrawlJob.objects.filter(pk=job.pk, status=CrawlJob.Status.RUNNING).update(
            status=CrawlJob.Status.QUEUED if retry else CrawlJob.Status.FAILED, error=reason,
            lease_until=timezone.now() + timedelta(seconds=delay) if retry else None)
        CrawlMetric.objects.create(job=job, stage=job.stage, status="failed", message=reason,
                                   duration_ms=int((time.monotonic()-start)*1000))
        if retry:
            enqueue(job, countdown=delay)
        elif permanent:
            InstagramAccount.objects.filter(pk=job.account_id).update(status="needs_login", last_error=reason)
    finally:
        release_operation_lease(operation_lease)


from . import media_tasks  # noqa: E402,F401 — register Stage 2 tasks during Celery autodiscovery
from . import comment_tasks  # noqa: E402,F401 — register Stage 3 tasks during Celery autodiscovery
from . import monitor_tasks  # noqa: E402,F401 — register monitoring tasks during Celery autodiscovery
from . import relationship_tasks  # noqa: E402,F401 — register Stage 4 tasks during Celery autodiscovery
from . import publish_tasks  # noqa: E402,F401 — register Stage 7 tasks during Celery autodiscovery
from . import observation_tasks  # noqa: E402,F401 — register Stage 8 orchestration tasks
