"""Stage 2: bounded, recoverable media pages; cursor commits after each page."""
import asyncio
import random
import time
import logging
from datetime import timedelta
from celery import shared_task
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from .account_pool import (
    acquire_operation_account, instagram_safety_signal, record_account_failure, record_account_success,
    release_operation_lease,
)
from .crypto import seal
from .media_store import save_likers, save_media
from .models import (InstagramAccount, MediaSyncJob, MediaSyncEvent, PostCrawlState,
                     PostSnapshot, PostSyncCursor, PublishJob)
from .session import InstagramSessionError, aiograpi_client
from .crawl_state import ensure_session, checkpoint, save_profile_discovery, save_profile_to_session, ensure_progress

STREAMS = ("timeline", "reels")
logger = logging.getLogger(__name__)


def queue_media(job, countdown=0, stream=None):
    media_page.apply_async(args=[str(job.pk), stream or ""], countdown=countdown,
        priority=9,
        soft_time_limit=settings.INSTRADAR_MEDIA_TIMEOUT,
        time_limit=settings.INSTRADAR_MEDIA_TIMEOUT + 30)


async def _connect(account, target, known_pk):
    client = await aiograpi_client(account)
    profile = await client.user_info_by_username(target) if not known_pk else None
    return client, profile


async def _fetch_page(client, stream, target_pk, expected, include_likers=False):
    size = settings.INSTRADAR_MEDIA_PAGE_SIZE
    cursor, api = stream.cursor, stream.api
    if stream.stream == "reels":
        items, next_cursor = await client.user_clips_paginated_v1(target_pk, amount=size, end_cursor=cursor)
    elif api == "gql":
        items, next_cursor = await client.user_medias_paginated_gql(target_pk, amount=size, end_cursor=cursor)
    else:
        items, next_cursor = await client.user_medias_paginated_v1(target_pk, amount=size, end_cursor=cursor)
        if not cursor and not items and expected:
            items, next_cursor = await client.user_medias_paginated_gql(target_pk, amount=size, end_cursor="")
            api = "gql"
    results = []
    incomplete = False
    for item in items:
        await asyncio.sleep(random.randint(settings.INSTRADAR_MIN_DELAY_MS,
                                          settings.INSTRADAR_MAX_DELAY_MS) / 1000)
        try:
            detailed = await client.media_info(str(item.pk), use_cache=False)
            likers = []
            if include_likers and getattr(detailed, "like_count", 0):
                try:
                    likers = await asyncio.wait_for(
                        client.media_likers(str(detailed.pk)),
                        timeout=settings.INSTRADAR_COMMENT_REQUEST_TIMEOUT)
                except Exception as exc:
                    if instagram_safety_signal(exc):
                        raise
                    logger.info("Instagram did not expose likers for media=%s", detailed.pk)
            results.append((detailed, True, likers))
        except Exception as exc:
            if instagram_safety_signal(exc):
                raise
            incomplete = True
            results.append((item, False, []))
    return results, str(next_cursor or ""), api, incomplete


def _finish(job):
    streams = list(PostSyncCursor.objects.filter(job=job))
    if len(streams) != 2 or not all(stream.finished for stream in streams):
        return False
    distinct_posts = PostSnapshot.objects.filter(job=job).count()
    issues = [stream.issue for stream in streams if stream.issue]
    if (job.mode == "full" and not job.date_from and not job.date_to
            and job.expected_media_count is not None and distinct_posts < job.expected_media_count):
        issues.append(f"{distinct_posts} از {job.expected_media_count} محتوای اعلام‌شده در دسترس بود")
    job.status = MediaSyncJob.Status.PARTIAL if issues else MediaSyncJob.Status.DONE
    job.error = "; ".join(issues)[:300]
    job.lease_until = None
    job.save(update_fields=["status", "error", "lease_until", "updated_at"])
    if not issues and not job.date_from and not job.date_to:
        for stream in streams:
            if stream.top_media_pk:
                PostCrawlState.objects.update_or_create(account=job.account, target_pk=job.target_pk,
                    stream=stream.stream, defaults={"last_top_pk": stream.top_media_pk,
                                                      "last_complete_at": timezone.now()})
    # Stage 7 publication is deliberately started only after both timeline and
    # reels have closed, so Telegram receives a stable oldest-to-newest order.
    if (job.observation_id is None and
            PublishJob.objects.filter(media_job=job, status=PublishJob.Status.WAITING).exists()):
        from .publish_tasks import prepare_observation_publish
        transaction.on_commit(lambda: prepare_observation_publish.delay(str(job.pk)))
    if job.observation_id:
        from .observation_tasks import queue_observation
        transaction.on_commit(lambda: queue_observation(job.observation))
    return True


def _persist_page(job, stream, results, next_cursor, api, incomplete, elapsed_ms=0):
    previous_cursor = stream.cursor
    top, last_code = stream.top_media_pk, stream.last_shortcode
    written, reached_anchor, liker_records = 0, False, 0
    for result in results:
        item, detailed = result[:2]
        likers = result[2] if len(result) > 2 else []
        taken_at = getattr(item, "taken_at", None)
        if job.date_from and taken_at and taken_at.date() < job.date_from:
            continue
        if job.date_to and taken_at and taken_at.date() > job.date_to:
            continue
        post, _ = save_media(job, item, job.target, detailed=detailed, stream=stream.stream)
        if likers:
            liker_records += save_likers(job, post, likers)
        written += 1
        last_code = post.shortcode
        if not top:
            top = post.media_pk
        if job.mode == "incremental" and stream.previous_top_pk == post.media_pk:
            reached_anchor = True
    stream.api, stream.cursor = api, next_cursor
    stream.top_media_pk, stream.last_shortcode = top, last_code
    stream.pages += 1
    stream.seen += len(results)
    if incomplete:
        stream.issue = "برخی جزئیات دریافت نشد؛ فهرست اولیه ذخیره شد"
    if not results and stream.stream == "timeline" and stream.pages == 1 and job.expected_media_count:
        stream.issue = "Timeline با وجود شمارندهٔ غیرصفر خالی بود"
    if next_cursor and next_cursor == previous_cursor:
        stream.issue = "Cursor تکراری؛ پیمایش متوقف شد"
    if job.max_pages and stream.pages >= job.max_pages and next_cursor and not reached_anchor:
        stream.issue = "سقف صفحهٔ آزمایشی رسید؛ Resume برای ادامه"
    stream.finished = bool(not next_cursor or reached_anchor or (stream.issue and (
        "Cursor تکراری" in stream.issue or "سقف صفحه" in stream.issue)))
    stream.save()
    if job.observation_id:
        session = ensure_session(job.observation)
        checkpoint(session, stream.stream, cursor=next_cursor, last_media_id=stream.top_media_pk,
                   last_item_id=last_code, pages=stream.pages, extracted=stream.seen,
                   expected=job.expected_media_count, finished=stream.finished, issue=stream.issue,
                   status="partial" if stream.issue else "running")
    MediaSyncEvent.objects.create(job=job, stream=stream.stream, records=written,
        duration_ms=elapsed_ms,
        message=(f"صفحه {stream.pages}: {len(results)} مورد دیده شد؛ {written} ذخیره شد؛ "
                 f"{liker_records} لایک‌کنندهٔ قابل دسترس ثبت شد؛ API={api}"))


@shared_task(name="radar.media_tasks.media_page")
def media_page(job_id, stream_name=""):
    operation_lease = None
    with transaction.atomic():
        job = MediaSyncJob.objects.select_for_update().select_related(
            "account", "observation", "observation__account_pool").get(pk=job_id)
        if (job.observation_id and job.observation.status not in (
                job.observation.Status.QUEUED, job.observation.Status.RUNNING)):
            return
        if job.status != MediaSyncJob.Status.QUEUED or (job.lease_until and job.lease_until > timezone.now()):
            return
        include_likers = bool(job.observation_id and job.observation.include_likes)
        request_cost = 1 + settings.INSTRADAR_MEDIA_PAGE_SIZE * (
            2 if include_likers else 1)
        operation_lease, unavailable = acquire_operation_account(
            job.account_id, job.observation_id, "media",
            settings.INSTRADAR_MEDIA_TIMEOUT + 60, cost=request_cost)
        if unavailable:
            job.lease_until = timezone.now() + timedelta(
                seconds=unavailable.wait_seconds)
            job.save(update_fields=["lease_until", "updated_at"])
            MediaSyncEvent.objects.create(
                job=job, stream="scheduler", message=unavailable.reason[:300])
            if job.observation_id:
                from .observation_tasks import queue_observation
                transaction.on_commit(lambda: queue_observation(
                    job.observation, countdown=unavailable.wait_seconds))
            else:
                transaction.on_commit(lambda: queue_media(
                    job, countdown=unavailable.wait_seconds))
            return
        if operation_lease.account_id != job.account_id:
            # A pooled observation may rotate to a healthier member between
            # pages. Persist the selected account so this page and all
            # failure/success bookkeeping use that session.
            job.account_id = operation_lease.account_id
            job.account = InstagramAccount.objects.get(pk=operation_lease.account_id)
            job.save(update_fields=["account", "updated_at"])
        job.status = MediaSyncJob.Status.RUNNING
        job.attempts += 1
        job.lease_until = timezone.now() + timedelta(seconds=settings.INSTRADAR_MEDIA_TIMEOUT + 30)
        job.save(update_fields=["status", "attempts", "lease_until", "updated_at"])
    try:
        with asyncio.Runner() as runner:
            client, profile = runner.run(_connect(job.account, job.target, job.target_pk))
            InstagramAccount.objects.filter(pk=job.account_id).update(
                encrypted_aiograpi_settings=seal(client.get_settings()), status="active",
                last_error="", verified_at=timezone.now())
            if profile:
                if job.observation_id:
                    session = ensure_session(job.observation)
                    save_profile_to_session(session, profile)
                    save_profile_discovery(job.owner_id, profile)
                    checkpoint(session, "profile", extracted=1, expected=1, pages=1, finished=True, status="done")
                    for _stream, _expected, _enabled in (("timeline", getattr(profile, "media_count", None), job.observation.include_posts if job.observation_id else True),
                                                          ("reels", getattr(profile, "clips_count", None), job.observation.include_reels if job.observation_id else True)):
                        if _enabled: ensure_progress(session, _stream, expected=_expected)
                job.target_pk = str(profile.pk)
                job.expected_media_count = getattr(profile, "media_count", None)
                job.save(update_fields=["target_pk", "expected_media_count", "updated_at"])
                for name in STREAMS:
                    previous = PostCrawlState.objects.filter(account=job.account, target_pk=job.target_pk, stream=name).first()
                    PostSyncCursor.objects.filter(job=job, stream=name).update(
                        previous_top_pk=previous.last_top_pk if previous else "")
            pages_this_task = 1 if job.observation_id else 3
            for _ in range(pages_this_task):
                job.refresh_from_db()
                if job.status != MediaSyncJob.Status.RUNNING:
                    return
                streams = PostSyncCursor.objects.filter(job=job, finished=False)
                if stream_name:
                    streams = streams.filter(stream=stream_name)
                stream = streams.order_by("id").first()
                if stream is None:
                    if not _finish(job) and job.observation_id:
                        from .observation_tasks import queue_observation
                        MediaSyncJob.objects.filter(pk=job.pk).update(
                            status=MediaSyncJob.Status.QUEUED, lease_until=None)
                        queue_observation(job.observation)
                    return
                started = time.monotonic()
                include_likers = bool(job.observation_id and job.observation.include_likes)
                results, next_cursor, api, incomplete = runner.run(
                    _fetch_page(client, stream, job.target_pk, job.expected_media_count,
                                include_likers=include_likers))
                _persist_page(job, stream, results, next_cursor, api, incomplete,
                              elapsed_ms=int((time.monotonic() - started) * 1000))
                record_account_success(job.account_id)
                job.attempts = 0
                job.lease_until = timezone.now() + timedelta(seconds=settings.INSTRADAR_MEDIA_TIMEOUT + 30)
                job.save(update_fields=["attempts", "lease_until", "updated_at"])
                if _finish(job):
                    return
        if MediaSyncJob.objects.filter(pk=job.pk, status=MediaSyncJob.Status.RUNNING).update(
                status=MediaSyncJob.Status.QUEUED, lease_until=None):
            if job.observation_id:
                from .observation_tasks import queue_observation
                queue_observation(job.observation)
            else:
                queue_media(job)
    except Exception as exc:
        logger.exception("Media observation failed job=%s target=%s", job.pk, job.target)
        reason = (str(exc) if isinstance(exc, InstagramSessionError)
                  else f"{type(exc).__name__}: {str(exc) or 'دریافت یا ذخیرهٔ صفحه ناموفق بود'}")
        reason = reason[:300]
        record_account_failure(job.account_id, job.observation_id, exc, reason)
        safety_signal = instagram_safety_signal(exc)
        permanent = safety_signal == "login" or (
            safety_signal not in ("feedback", "rate") and (
                getattr(exc, "permanent", False) or
                any(word in type(exc).__name__.lower() for word in
                    ("challenge", "loginrequired")) or
                isinstance(exc, (ValueError, AssertionError)) or
                (isinstance(exc, RuntimeError) and str(exc) in (
                    "SessionRejected", "LoggedIntoDifferentAccount"))))
        retry = not permanent and job.attempts < settings.INSTRADAR_MAX_RETRIES
        delay = min(settings.INSTRADAR_BACKOFF_MAX, settings.INSTRADAR_BACKOFF_BASE *
                    (2 ** max(0, job.attempts - 1)))
        changed = MediaSyncJob.objects.filter(pk=job.pk, status=MediaSyncJob.Status.RUNNING).update(
            status=MediaSyncJob.Status.QUEUED if retry else MediaSyncJob.Status.FAILED,
            error=reason, lease_until=timezone.now() + timedelta(seconds=delay) if retry else None)
        if changed:
            MediaSyncEvent.objects.create(job=job, stream="error", message=reason)
            if retry:
                if job.observation_id:
                    from .observation_tasks import queue_observation
                    queue_observation(job.observation, countdown=delay)
                else:
                    queue_media(job, countdown=delay)
    finally:
        release_operation_lease(operation_lease)


@shared_task(name="radar.media_tasks.recover_media_sync")
def recover_media_sync():
    now = timezone.now()
    for pending in MediaSyncJob.objects.filter(status=MediaSyncJob.Status.RUNNING, lease_until__lt=now)[:100]:
        with transaction.atomic():
            job = MediaSyncJob.objects.select_for_update().get(pk=pending.pk)
            if job.status != MediaSyncJob.Status.RUNNING or job.lease_until >= now:
                continue
            job.status = MediaSyncJob.Status.QUEUED if job.attempts < settings.INSTRADAR_MAX_RETRIES else MediaSyncJob.Status.FAILED
            job.error = "Worker قطع شد؛ ادامه از Cursor ذخیره‌شده" if job.status == MediaSyncJob.Status.QUEUED else "تلاش مجاز تمام شد"
            job.lease_until = None
            job.save(update_fields=["status", "error", "lease_until", "updated_at"])
        if job.status == MediaSyncJob.Status.QUEUED:
            if job.observation_id:
                from .observation_tasks import queue_observation
                queue_observation(job.observation)
            else:
                queue_media(job)
    for job in MediaSyncJob.objects.filter(status=MediaSyncJob.Status.QUEUED,
                                          updated_at__lt=now - timedelta(minutes=2))[:100]:
        if job.lease_until and job.lease_until > now:
            continue
        if job.observation_id:
            from .observation_tasks import queue_observation
            queue_observation(job.observation)
        else:
            queue_media(job)
        MediaSyncJob.objects.filter(pk=job.pk, status=MediaSyncJob.Status.QUEUED).update(updated_at=now)
