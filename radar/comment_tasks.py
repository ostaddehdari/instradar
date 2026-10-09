"""Stage 3: resumable, bounded comment/reply pages using the captured aiograpi session."""
import asyncio
import random
import time
from datetime import timedelta
from celery import shared_task
from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from .account_pool import (
    acquire_comment_account, instagram_safety_signal, record_account_failure, record_account_success,
    release_comment_account,
)
from .comment_store import normalize, save_page
from .crypto import seal
from .media_tasks import _connect
from .models import (
    Comment, CommentSyncEvent, CommentSyncJob, InstagramAccount,
    PostCommentSyncState, ReplySyncCursor,
)
from .session import InstagramSessionError


def queue_comments(job, countdown=0):
    comment_page.apply_async(args=[str(job.pk)], countdown=countdown,
        priority=3,
        soft_time_limit=settings.INSTRADAR_COMMENT_TIMEOUT,
        time_limit=settings.INSTRADAR_COMMENT_TIMEOUT + 30)


def _gql_comment(raw):
    """Normalize the raw node returned by aiograpi's Web GraphQL chunk."""
    from aiograpi.extractors import extract_comment_gql

    node = dict(raw)
    threaded = node.get("edge_threaded_comments") or {}
    if node.get("child_comment_count") is None:
        node["child_comment_count"] = threaded.get("count") or len(threaded.get("edges") or [])
    return extract_comment_gql(node)


def _gql_page(raw_items):
    """Keep embedded replies and link them to the top-level node."""
    result = []
    for raw in raw_items:
        parent = _gql_comment(raw)
        result.append(parent)
        threaded = (raw.get("edge_threaded_comments") or {}).get("edges") or []
        for edge in threaded:
            child = dict(edge.get("node") or {})
            if not child:
                continue
            child["parent_comment_id"] = str(parent.pk)
            result.append(_gql_comment(child))
    return result


async def _fetch(client, media_pk, expected_count, top_cursor="",
                 reply_parent_pk="", reply_cursor=""):
    """Fetch one page without touching Django models inside the event loop."""
    await asyncio.sleep(random.randint(settings.INSTRADAR_MIN_DELAY_MS,
                                     settings.INSTRADAR_MAX_DELAY_MS) / 1000)
    size = settings.INSTRADAR_COMMENT_PAGE_SIZE
    if reply_parent_pk:
        items, cursor = await asyncio.wait_for(client.media_comment_replies_chunk(
            media_pk, reply_parent_pk, size, min_id=reply_cursor or None),
            timeout=settings.INSTRADAR_COMMENT_REQUEST_TIMEOUT)
        return items, str(cursor or ""), "private", ""

    expected_remaining = (1 if expected_count is None
                          else max(0, int(expected_count)))
    attempts = []
    try:
        items, cursor = await asyncio.wait_for(client.media_comments_chunk(
            media_pk, size, min_id=top_cursor or None),
            timeout=settings.INSTRADAR_COMMENT_REQUEST_TIMEOUT)
        attempts.append("private")
        if items or cursor or not expected_remaining:
            return items, str(cursor or ""), "private", ""
    except Exception as exc:
        if instagram_safety_signal(exc):
            raise
        attempts.append(f"private:{type(exc).__name__}")

    # Private endpoints can return HTTP 200 with an empty list.  The Web
    # GraphQL cursor uses the exact same browser cookie bundle captured by
    # noVNC and is therefore the safe fallback for that false-empty response.
    try:
        raw_items, cursor = await asyncio.wait_for(
            client.media_comments_gql_chunk(media_pk,
                                            end_cursor=top_cursor or ""),
            timeout=settings.INSTRADAR_COMMENT_REQUEST_TIMEOUT)
        attempts.append("gql")
        items = _gql_page(raw_items)
        notice = "Private خالی بود؛ صفحه از Web GraphQL همان نشست خوانده شد"
        if not items and expected_remaining:
            notice = "هر دو مسیر Private و Web GraphQL پاسخ خالی دادند"
        return items, str(cursor or ""), "gql", notice
    except Exception as exc:
        if instagram_safety_signal(exc):
            raise
        attempts.append(f"gql:{type(exc).__name__}")
        raise RuntimeError("CommentRoutesEmpty[" + ",".join(attempts) + "]") from exc


def _complete(job):
    if not job.top_finished or job.reply_cursors.filter(finished=False).exists():
        return False
    observed = Comment.objects.filter(post=job.post, last_seen_job=job,
                                      last_seen_at__gte=job.scan_started_at).count()
    issues = list(job.reply_cursors.exclude(issue="").values_list("issue", flat=True)[:10])
    if job.error:
        issues.append(job.error)
    if job.expected_count is not None and observed < job.expected_count:
        issues.append(f"فقط {observed} کامنت از {job.expected_count} شمارندهٔ پست در دسترس بود")
    if job.expected_count is None:
        issues.append("شمارندهٔ قابل تطبیق برای پست وجود ندارد")
    job.status = CommentSyncJob.Status.PARTIAL if issues else CommentSyncJob.Status.DONE
    job.error = "; ".join(issues)[:300]
    job.extracted_count = observed
    job.lease_until = None
    job.save(update_fields=["status", "error", "extracted_count", "lease_until", "updated_at"])
    state, _ = PostCommentSyncState.objects.get_or_create(
        owner=job.owner, post=job.post)
    state.last_account = job.account
    state.last_observed_count = job.expected_count
    state.last_status = job.status
    state.last_scan_at = timezone.now()
    if job.status == CommentSyncJob.Status.DONE:
        state.last_complete_at = state.last_scan_at
    state.save(update_fields=[
        "last_account", "last_observed_count", "last_status", "last_scan_at",
        "last_complete_at"])
    # Never mark missing comments as deleted on incomplete or resumed-only scans.
    if not issues and job.top_pages and job.expected_count is not None and observed >= job.expected_count:
        Comment.objects.filter(post=job.post).exclude(last_seen_job=job, last_seen_at__gte=job.scan_started_at).update(unavailable=True)
    CommentSyncEvent.objects.create(job=job, stage="reconcile", records=observed,
        message=f"پایان پیمایش: {observed} از {job.expected_count if job.expected_count is not None else '?'}؛ {job.get_status_display()}")
    if job.observation_id:
        from .observation_tasks import queue_observation
        transaction.on_commit(lambda: queue_observation(job.observation))
    return True


@transaction.atomic
def _commit_page(job, reply, items, next_cursor, elapsed_ms, api="private", notice=""):
    rows = [normalize(item, reply.parent.instagram_pk if reply else "") for item in items]
    saved = save_page(job, rows, parent=reply.parent if reply else None)
    cursor = str(next_cursor or "")
    previous = reply.cursor if reply else job.top_cursor
    stage = "replies" if reply else "top"
    if reply:
        reply.cursor = cursor
        reply.pages += 1
        seen = Comment.objects.filter(last_seen_job=job, parent=reply.parent,
                                      last_seen_at__gte=job.scan_started_at).count()
        if not cursor and reply.expected_count is not None and seen < reply.expected_count:
            reply.issue = f"پاسخ‌های {reply.parent.instagram_pk}: {seen} از {reply.expected_count} در دسترس بود"
        if cursor == previous and cursor:
            reply.issue = "Cursor پاسخ تکراری؛ صفحه متوقف شد"
        reply.finished = not cursor or bool(reply.issue)
        reply.save()
    else:
        job.top_cursor = cursor
        job.top_pages += 1
        job.top_finished = not cursor or (cursor == previous)
        if cursor == previous and cursor:
            job.error = "Cursor کامنت تکراری؛ خروجی ممکن است ناقص باشد"
        job.save(update_fields=["top_cursor", "top_pages", "top_finished", "error", "updated_at"])
    job.extracted_count = Comment.objects.filter(last_seen_job=job).count()
    job.save(update_fields=["extracted_count", "updated_at"])
    message = (f"صفحهٔ {stage}: {saved} مورد؛ API={api}؛ "
               f"cursor={'ادامه' if cursor else 'پایان'}")
    if notice:
        message += f"؛ {notice}"
    CommentSyncEvent.objects.create(job=job, stage=stage, records=saved,
                                    duration_ms=elapsed_ms, message=message[:300])


@shared_task(name="radar.comment_tasks.comment_page")
def comment_page(job_id):
    with transaction.atomic():
        job = CommentSyncJob.objects.select_for_update().select_related(
            "account", "post", "observation", "observation__account_pool").get(pk=job_id)
        if (job.observation_id and job.observation.status not in (
                job.observation.Status.QUEUED, job.observation.Status.RUNNING)):
            job.scheduled_at = None
            job.save(update_fields=["scheduled_at", "updated_at"])
            return
        if job.status != CommentSyncJob.Status.QUEUED or (job.lease_until and job.lease_until > timezone.now()):
            return
        lease, unavailable = acquire_comment_account(job)
        if unavailable:
            job.scheduled_at = None
            job.lease_until = timezone.now() + timedelta(seconds=unavailable.wait_seconds)
            job.save(update_fields=["scheduled_at", "lease_until", "updated_at"])
            CommentSyncEvent.objects.create(
                job=job, stage="scheduler", message=unavailable.reason[:300])
            if job.observation_id:
                from .observation_tasks import queue_observation
                transaction.on_commit(lambda: queue_observation(
                    job.observation, countdown=unavailable.wait_seconds))
            else:
                transaction.on_commit(lambda: queue_comments(
                    job, countdown=unavailable.wait_seconds))
            return
        job.refresh_from_db()
        job.status = CommentSyncJob.Status.RUNNING
        job.attempts += 1
        job.lease_until = timezone.now() + timedelta(seconds=settings.INSTRADAR_COMMENT_TIMEOUT + 30)
        job.scheduled_at = None
        job.save(update_fields=[
            "status", "attempts", "lease_until", "scheduled_at", "updated_at"])
    active_account_id = lease.account_id
    try:
        with asyncio.Runner() as runner:
            client, _ = runner.run(_connect(job.account, job.post.owner_username, job.post.owner_pk))
            InstagramAccount.objects.filter(pk=job.account_id).update(
                encrypted_aiograpi_settings=seal(client.get_settings()), status="active",
                last_error="", verified_at=timezone.now())
            # Unified observations yield after each page.  This lets a newly
            # dragged higher-priority stream run before the next comment page.
            pages_this_task = 1 if job.observation_id else 2
            for _ in range(pages_this_task):
                job.refresh_from_db()
                if job.status != CommentSyncJob.Status.RUNNING:
                    return
                reply = None if not job.top_finished else (ReplySyncCursor.objects.filter(job=job, finished=False)
                    .select_related("parent").order_by("id").first())
                if job.top_finished and not reply:
                    _complete(job)
                    return
                started = time.monotonic()
                # refresh_from_db() clears select_related caches. Resolve every
                # ORM value synchronously here and pass only primitives into
                # the asyncio runner; otherwise job.post access raises
                # SynchronousOnlyOperation before either Instagram route runs.
                post = job.post
                media_pk = str(post.media_pk)
                reply_parent_pk = (str(reply.parent.instagram_pk) if reply else "")
                reply_cursor = str(reply.cursor or "") if reply else ""
                items, cursor, api, notice = runner.run(_fetch(
                    client, media_pk, job.expected_count, job.top_cursor,
                    reply_parent_pk, reply_cursor))
                _commit_page(job, reply, items, cursor,
                             int((time.monotonic() - started) * 1000), api, notice)
                record_account_success(active_account_id)
                job.attempts = 0
                if "ناموفق" in job.error:
                    job.error = ""
                job.lease_until = timezone.now() + timedelta(seconds=settings.INSTRADAR_COMMENT_TIMEOUT + 30)
                job.save(update_fields=["attempts", "error", "lease_until", "updated_at"])
                if _complete(job):
                    return
        if CommentSyncJob.objects.filter(pk=job.pk, status=CommentSyncJob.Status.RUNNING).update(
                status=CommentSyncJob.Status.QUEUED, lease_until=None, scheduled_at=None):
            release_comment_account(job, lease)
            if job.observation_id:
                from .observation_tasks import queue_observation
                queue_observation(job.observation)
            else:
                queue_comments(job)
    except Exception as exc:
        if isinstance(exc, InstagramSessionError):
            reason = str(exc)
        elif isinstance(exc, RuntimeError) and str(exc).startswith("CommentRoutesEmpty["):
            reason = f"همه مسیرهای کامنت ناموفق بودند؛ {str(exc)[:180]}"
        else:
            reason = f"{type(exc).__name__}: دریافت کامنت ناموفق بود"
        kind = type(exc).__name__.lower()
        safety_signal = instagram_safety_signal(exc)
        permanent = safety_signal == "login" or (
            safety_signal not in ("feedback", "rate") and (
                getattr(exc, "permanent", False) or
                any(word in kind for word in ("challenge", "loginrequired")) or
                (isinstance(exc, RuntimeError) and str(exc) in (
                    "SessionRejected", "LoggedIntoDifferentAccount"))))
        category = record_account_failure(
            active_account_id, job.observation_id, exc, reason)
        has_pool = bool(job.observation_id and job.observation.account_pool_id)
        retry = job.attempts < settings.INSTRADAR_MAX_RETRIES and (
            not permanent or has_pool)
        delay = min(settings.INSTRADAR_BACKOFF_MAX, settings.INSTRADAR_BACKOFF_BASE *
                    (2 ** max(0, job.attempts - 1)))
        changed = CommentSyncJob.objects.filter(pk=job.pk, status=CommentSyncJob.Status.RUNNING).update(
            status=CommentSyncJob.Status.QUEUED if retry else CommentSyncJob.Status.FAILED,
            error=reason, scheduled_at=None,
            lease_until=timezone.now() + timedelta(seconds=delay) if retry else None)
        release_comment_account(job, lease)
        if changed:
            CommentSyncEvent.objects.create(
                job=job, stage="error", message=f"{reason}؛ account={category}"[:300])
            if retry:
                if job.observation_id:
                    from .observation_tasks import queue_observation
                    queue_observation(job.observation, countdown=delay)
                else:
                    queue_comments(job, countdown=delay)
    finally:
        # Covers early completion returns as well as fetch/connect failures.
        release_comment_account(job, lease)


@shared_task(name="radar.comment_tasks.recover_comments")
def recover_comments():
    now = timezone.now()
    from .models import InstagramAccountLease
    InstagramAccountLease.objects.filter(expires_at__lt=now).delete()
    for pending in CommentSyncJob.objects.filter(status=CommentSyncJob.Status.RUNNING, lease_until__lt=now)[:100]:
        with transaction.atomic():
            job = CommentSyncJob.objects.select_for_update().get(pk=pending.pk)
            if job.status != CommentSyncJob.Status.RUNNING or job.lease_until is None or job.lease_until >= now:
                continue
            job.status = CommentSyncJob.Status.QUEUED if job.attempts < settings.INSTRADAR_MAX_RETRIES else CommentSyncJob.Status.FAILED
            job.error = "Worker قطع شد؛ ادامه از cursor ذخیره‌شده" if job.status == CommentSyncJob.Status.QUEUED else "تلاش مجاز تمام شد"
            job.lease_until = None
            job.scheduled_at = None
            job.save(update_fields=[
                "status", "error", "lease_until", "scheduled_at", "updated_at"])
        if job.status == CommentSyncJob.Status.QUEUED:
            if job.observation_id:
                from .observation_tasks import queue_observation
                queue_observation(job.observation)
            else:
                queue_comments(job)
    stale_schedule = now - timedelta(
        seconds=settings.INSTRADAR_COMMENT_DISPATCH_STALE_SECONDS)
    for job in CommentSyncJob.objects.filter(
            status=CommentSyncJob.Status.QUEUED,
            updated_at__lt=now - timedelta(minutes=2)).filter(
                Q(scheduled_at__isnull=True) | Q(scheduled_at__lt=stale_schedule))[:100]:
        if job.lease_until and job.lease_until > now:
            continue
        if job.observation_id:
            from .observation_tasks import queue_observation
            queue_observation(job.observation)
        else:
            queue_comments(job)
        CommentSyncJob.objects.filter(pk=job.pk, status=CommentSyncJob.Status.QUEUED).update(updated_at=now)
