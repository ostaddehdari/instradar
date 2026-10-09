"""Conservative account-pool scheduling for comment page work.

The pool is a safety boundary, not a rate-limit bypass: it enforces both
per-account and pool-wide budgets, one active lease per browser session, and
automatic cooldown/quarantine on Instagram safety responses.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from .models import (
    CommentSyncJob, InstagramAccount, InstagramAccountHealthEvent,
    InstagramAccountLease, InstagramAccountPool, InstagramAccountPoolMember,
    ObservationJob,
)


@dataclass(frozen=True)
class AccountUnavailable:
    wait_seconds: int
    reason: str


def instagram_safety_signal(exc):
    """Classify upstream stop signals without inspecting or storing response bodies."""
    raw = f"{type(exc).__name__} {exc}".lower()
    if "feedbackrequired" in raw or "feedback_required" in raw:
        return "feedback"
    if any(marker in raw for marker in (
            "ratelimit", "pleasewaitfewminutes", "too many", "http 429", "http429",
            " 429")):
        return "rate"
    if any(marker in raw for marker in (
            "challenge", "checkpoint", "loginrequired", "login_required",
            "sessionrejected", "loggedintodifferentaccount", "consentrequired")):
        return "login"
    return ""


def _seconds_until(value, now, default=30):
    if not value:
        return default
    return max(1, int((value - now).total_seconds()) + 1)


def _reset_account_windows(account, now):
    changed = []
    if (not account.request_window_started_at or
            account.request_window_started_at <= now - timedelta(hours=1)):
        account.request_window_started_at = now
        account.requests_this_hour = 0
        changed += ["request_window_started_at", "requests_this_hour"]
    today = timezone.localdate(now)
    if account.request_day != today:
        account.request_day = today
        account.requests_today = 0
        changed += ["request_day", "requests_today"]
    if (account.health_status == InstagramAccount.Health.COOLDOWN and
            account.cooldown_until and account.cooldown_until <= now):
        account.health_status = InstagramAccount.Health.READY
        account.cooldown_until = None
        changed += ["health_status", "cooldown_until"]
    return changed


def _reset_pool_windows(pool, now):
    changed = []
    if (not pool.request_window_started_at or
            pool.request_window_started_at <= now - timedelta(hours=1)):
        pool.request_window_started_at = now
        pool.requests_this_hour = 0
        changed += ["request_window_started_at", "requests_this_hour"]
    today = timezone.localdate(now)
    if pool.request_day != today:
        pool.request_day = today
        pool.requests_today = 0
        changed += ["request_day", "requests_today"]
    return changed


def _budget_wait(obj, now, cost=1):
    if isinstance(obj, InstagramAccountPool):
        hourly_limit = min(
            obj.hourly_request_limit, settings.INSTRADAR_POOL_HARD_HOURLY_LIMIT)
        daily_limit = min(
            obj.daily_request_limit, settings.INSTRADAR_POOL_HARD_DAILY_LIMIT)
    else:
        hourly_limit = min(
            obj.hourly_request_limit, settings.INSTRADAR_ACCOUNT_HARD_HOURLY_LIMIT)
        daily_limit = min(
            obj.daily_request_limit, settings.INSTRADAR_ACCOUNT_HARD_DAILY_LIMIT)
        if obj.health_score <= 80:
            # Slow-start a recently limited account until successful pages
            # gradually restore its health score.
            hourly_limit = max(1, hourly_limit // 2)
            daily_limit = max(1, daily_limit // 2)
    if hourly_limit and obj.requests_this_hour + cost > hourly_limit:
        return _seconds_until(obj.request_window_started_at + timedelta(hours=1), now)
    if daily_limit and obj.requests_today + cost > daily_limit:
        tomorrow = timezone.localtime(now).replace(
            hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        return _seconds_until(tomorrow.astimezone(now.tzinfo), now)
    return 0


def _pool_members(observation):
    if not observation or not observation.account_pool_id:
        return []
    return list(InstagramAccountPoolMember.objects.filter(
        pool_id=observation.account_pool_id,
        pool__owner_id=observation.owner_id,
        is_active=True,
        allow_comments=True,
        account__owner_id=observation.owner_id,
        account__is_active=True,
        account__encrypted_cookies__isnull=False,
        account__status__in=("active", "captured"),
    ).select_related("account").order_by("last_used_at", "pk"))


def initial_comment_accounts(observation):
    """Return a stable round-robin seed list for post-level sharding."""
    members = _pool_members(observation)
    if not members:
        return [observation.account]
    now = timezone.now()
    usable = [item.account for item in members if (
        item.account.health_status != InstagramAccount.Health.QUARANTINED and
        (not item.account.cooldown_until or item.account.cooldown_until <= now)
    )]
    return usable or [item.account for item in members]


def comment_concurrency(observation):
    hard_cap = settings.INSTRADAR_POOL_MAX_CONCURRENCY
    if not observation.account_pool_id:
        return 1
    member_count = InstagramAccountPoolMember.objects.filter(
        pool_id=observation.account_pool_id, is_active=True, allow_comments=True,
        account__is_active=True, account__encrypted_cookies__isnull=False,
    ).count()
    return max(1, min(
        hard_cap, observation.max_active_accounts,
        observation.account_pool.max_concurrency, max(1, member_count)))


@transaction.atomic
def acquire_comment_account(job):
    """Acquire one eligible account and charge exactly one request budget unit."""
    now = timezone.now()
    job = CommentSyncJob.objects.select_for_update().select_related(
        "account", "observation", "observation__account_pool").get(pk=job.pk)
    observation = job.observation
    pool = None
    waits = []
    unavailable_reasons = []
    request_cost = 2  # Private + possible Web fallback for one comment page.

    InstagramAccountLease.objects.filter(expires_at__lte=now).delete()
    if observation and observation.account_pool_id:
        pool = InstagramAccountPool.objects.select_for_update().get(
            pk=observation.account_pool_id, owner_id=job.owner_id, is_active=True)
        changed = _reset_pool_windows(pool, now)
        if changed:
            pool.save(update_fields=list(dict.fromkeys(changed + ["updated_at"])))
        wait = _budget_wait(pool, now, request_cost)
        if wait:
            return None, AccountUnavailable(wait, "سقف درخواست سراسری Pool تکمیل شده است")
        active = InstagramAccountLease.objects.filter(
            observation__account_pool_id=pool.pk, expires_at__gt=now).count()
        if active >= min(pool.max_concurrency, settings.INSTRADAR_POOL_MAX_CONCURRENCY):
            return None, AccountUnavailable(15, "ظرفیت هم‌زمان Pool تکمیل است")
        members = _pool_members(observation)
        # Preserve a post's cursor/session affinity when its assigned account is
        # healthy; otherwise use least-recently-used fair rotation.
        members.sort(key=lambda item: (
            0 if item.account_id == job.account_id else 1,
            item.last_used_at is not None,
            item.last_used_at or now - timedelta(days=36500),
            item.pk,
        ))
        candidates = [(item.account_id, item.pk) for item in members]
    else:
        candidates = [(job.account_id, None)]

    if not candidates:
        return None, AccountUnavailable(300, "هیچ حساب فعال و نشست‌داری در Pool موجود نیست")

    for account_id, membership_id in candidates:
        account = InstagramAccount.objects.select_for_update().get(pk=account_id)
        changed = _reset_account_windows(account, now)
        if changed:
            account.save(update_fields=list(dict.fromkeys(changed)))
        if (not account.is_active or not account.encrypted_cookies or
                account.status not in ("active", "captured") or
                account.health_status == InstagramAccount.Health.QUARANTINED):
            continue
        if account.cooldown_until and account.cooldown_until > now:
            waits.append(_seconds_until(account.cooldown_until, now))
            unavailable_reasons.append("حساب در Cooldown است")
            continue
        wait = _budget_wait(account, now, request_cost)
        if wait:
            waits.append(wait)
            unavailable_reasons.append("سقف بودجهٔ حساب تکمیل شده است")
            continue
        if account.last_request_at:
            earliest = account.last_request_at + timedelta(
                seconds=max(settings.INSTRADAR_ACCOUNT_MIN_INTERVAL_FLOOR,
                            account.min_interval_seconds) *
                (2 if account.health_score <= 80 else 1))
            if earliest > now:
                waits.append(_seconds_until(earliest, now))
                unavailable_reasons.append("فاصلهٔ ایمن بین دو درخواست هنوز تمام نشده است")
                continue
        try:
            with transaction.atomic():
                lease = InstagramAccountLease.objects.create(
                    account=account, observation=observation, comment_job=job,
                    expires_at=now + timedelta(
                        seconds=settings.INSTRADAR_COMMENT_TIMEOUT + 60))
        except IntegrityError:
            waits.append(15)
            unavailable_reasons.append("حساب Lease فعال دارد")
            continue

        account.requests_this_hour += request_cost
        account.requests_today += request_cost
        account.last_request_at = now
        account.save(update_fields=[
            "requests_this_hour", "requests_today", "last_request_at"])
        if pool:
            pool.requests_this_hour += request_cost
            pool.requests_today += request_cost
            pool.last_request_at = now
            pool.save(update_fields=[
                "requests_this_hour", "requests_today", "last_request_at", "updated_at"])
        if membership_id:
            InstagramAccountPoolMember.objects.filter(pk=membership_id).update(
                last_used_at=now)
        if job.account_id != account.pk:
            job.account = account
            job.account_switches += 1
            job.save(update_fields=["account", "account_switches", "updated_at"])
        return lease, None

    return None, AccountUnavailable(
        min(waits) if waits else 300,
        unavailable_reasons[0] if len(set(unavailable_reasons)) == 1 else
        "همهٔ حساب‌های Pool مشغول، در Cooldown یا خارج از بودجه‌اند")


def release_comment_account(job, lease=None):
    query = InstagramAccountLease.objects.filter(comment_job_id=job.pk)
    if lease is not None:
        query = query.filter(pk=lease.pk)
    query.delete()


@transaction.atomic
def acquire_operation_account(account_id, observation_id, purpose, lease_seconds, cost=1):
    """Acquire a non-comment operation under the same global safety boundary."""
    now = timezone.now()
    InstagramAccountLease.objects.filter(expires_at__lte=now).delete()
    observation = None
    pool = None
    waits = []
    unavailable_reasons = []
    if observation_id:
        observation = ObservationJob.objects.select_related("account_pool").get(
            pk=observation_id)
        if observation.account_pool_id:
            pool = InstagramAccountPool.objects.select_for_update().get(
                pk=observation.account_pool_id, owner_id=observation.owner_id,
                is_active=True)
            changed = _reset_pool_windows(pool, now)
            if changed:
                pool.save(update_fields=list(dict.fromkeys(changed + ["updated_at"])))
            wait = _budget_wait(pool, now, cost)
            if wait:
                return None, AccountUnavailable(
                    wait, "سقف درخواست سراسری Pool تکمیل شده است")
            active = InstagramAccountLease.objects.filter(
                observation__account_pool_id=pool.pk, expires_at__gt=now).count()
            if active >= min(pool.max_concurrency, settings.INSTRADAR_POOL_MAX_CONCURRENCY):
                return None, AccountUnavailable(15, "ظرفیت هم‌زمان Pool تکمیل است")

    if pool:
        # A pool is also useful for media and relationship pages.  A member
        # that opted out of comments remains eligible for those read-only
        # operations, while quarantined/invalid sessions are always skipped.
        members = list(InstagramAccountPoolMember.objects.filter(
            pool=pool, is_active=True, account__owner_id=observation.owner_id,
            account__is_active=True,
            account__encrypted_cookies__isnull=False,
            account__status__in=("active", "captured"),
        ).select_related("account").order_by("last_used_at", "pk"))
        members.sort(key=lambda item: (
            item.last_used_at is not None,
            item.last_used_at or now - timedelta(days=36500),
            0 if item.account_id == account_id else 1,
            item.pk,
        ))
        candidates = [(item.account_id, item.pk) for item in members]
    else:
        candidates = [(account_id, None)]

    if not candidates:
        return None, AccountUnavailable(300, "هیچ حساب فعال و نشست‌داری در Pool موجود نیست")

    for candidate_id, membership_id in candidates:
        account = InstagramAccount.objects.select_for_update().get(pk=candidate_id)
        changed = _reset_account_windows(account, now)
        if changed:
            account.save(update_fields=list(dict.fromkeys(changed)))
        if (not account.is_active or
                account.status in ("needs_login", "error", "deleted") or
                account.health_status == InstagramAccount.Health.QUARANTINED):
            unavailable_reasons.append("حساب آمادهٔ عملیات نیست")
            continue
        if pool and (not account.encrypted_cookies or
                     account.status not in ("active", "captured")):
            unavailable_reasons.append("نشست حساب Pool آماده نیست")
            continue
        if account.cooldown_until and account.cooldown_until > now:
            waits.append(_seconds_until(account.cooldown_until, now))
            unavailable_reasons.append("حساب در Cooldown است")
            continue
        wait = _budget_wait(account, now, cost)
        if wait:
            waits.append(wait)
            unavailable_reasons.append("سقف بودجهٔ حساب تکمیل شده است")
            continue
        if account.last_request_at:
            earliest = account.last_request_at + timedelta(
                seconds=max(settings.INSTRADAR_ACCOUNT_MIN_INTERVAL_FLOOR,
                            account.min_interval_seconds) *
                (2 if account.health_score <= 80 else 1))
            if earliest > now:
                waits.append(_seconds_until(earliest, now))
                unavailable_reasons.append("فاصلهٔ ایمن بین دو درخواست هنوز تمام نشده است")
                continue
        try:
            with transaction.atomic():
                lease = InstagramAccountLease.objects.create(
                    account=account, observation=observation, purpose=purpose,
                    expires_at=now + timedelta(seconds=lease_seconds))
        except IntegrityError:
            waits.append(15)
            unavailable_reasons.append("حساب Lease فعال دارد")
            continue
        account.requests_this_hour += cost
        account.requests_today += cost
        account.last_request_at = now
        account.save(update_fields=[
            "requests_this_hour", "requests_today", "last_request_at"])
        if pool:
            pool.requests_this_hour += cost
            pool.requests_today += cost
            pool.last_request_at = now
            pool.save(update_fields=[
                "requests_this_hour", "requests_today", "last_request_at", "updated_at"])
            if membership_id:
                InstagramAccountPoolMember.objects.filter(pk=membership_id).update(
                    last_used_at=now)
        return lease, None

    return None, AccountUnavailable(
        min(waits) if waits else 300,
        unavailable_reasons[0] if len(set(unavailable_reasons)) == 1 else
        "همهٔ حساب‌های Pool مشغول، در Cooldown یا خارج از بودجه‌اند")


def release_operation_lease(lease):
    if lease:
        InstagramAccountLease.objects.filter(pk=lease.pk).delete()


def record_account_success(account_id):
    with transaction.atomic():
        account = InstagramAccount.objects.select_for_update().get(pk=account_id)
        account.consecutive_failures = 0
        account.health_score = min(100, account.health_score + 1)
        if (account.health_status == InstagramAccount.Health.COOLDOWN and
                (not account.cooldown_until or account.cooldown_until <= timezone.now())):
            account.health_status = InstagramAccount.Health.READY
            account.cooldown_until = None
        account.save(update_fields=[
            "consecutive_failures", "health_score", "health_status", "cooldown_until"])


def record_account_failure(account_id, observation_id, exc, message=""):
    """Apply conservative health transitions and return the failure category."""
    now = timezone.now()
    signal = instagram_safety_signal(exc)
    with transaction.atomic():
        account = InstagramAccount.objects.select_for_update().get(pk=account_id)
        account.consecutive_failures = min(255, account.consecutive_failures + 1)
        if signal == "feedback":
            category = "feedback_cooldown"
            account.health_status = InstagramAccount.Health.COOLDOWN
            account.cooldown_until = now + timedelta(
                seconds=settings.INSTRADAR_FEEDBACK_COOLDOWN_SECONDS)
            account.health_score = max(0, account.health_score - 25)
        elif signal == "rate":
            category = "rate_cooldown"
            account.health_status = InstagramAccount.Health.COOLDOWN
            account.cooldown_until = now + timedelta(
                seconds=settings.INSTRADAR_RATE_COOLDOWN_SECONDS)
            account.health_score = max(0, account.health_score - 20)
        elif getattr(exc, "permanent", False) or signal == "login":
            category = "quarantined"
            account.health_status = InstagramAccount.Health.QUARANTINED
            account.cooldown_until = None
            account.health_score = max(0, account.health_score - 40)
            account.status = "needs_login"
            account.last_error = (message or str(exc))[:500]
        else:
            category = "transient"
            account.health_score = max(0, account.health_score - 5)
            if account.consecutive_failures >= settings.INSTRADAR_FAILURES_BEFORE_COOLDOWN:
                account.health_status = InstagramAccount.Health.COOLDOWN
                multiplier = min(8, 2 ** (
                    account.consecutive_failures - settings.INSTRADAR_FAILURES_BEFORE_COOLDOWN))
                account.cooldown_until = now + timedelta(
                    seconds=settings.INSTRADAR_FAILURE_COOLDOWN_SECONDS * multiplier)
        account.save(update_fields=[
            "consecutive_failures", "health_score", "health_status", "cooldown_until",
            "status", "last_error"])
        InstagramAccountHealthEvent.objects.create(
            account=account, observation_id=observation_id, kind=category,
            message=(message or str(exc))[:300], cooldown_until=account.cooldown_until)
        if observation_id and category in (
                "quarantined", "feedback_cooldown", "rate_cooldown"):
            observation = ObservationJob.objects.select_related("account_pool").get(
                pk=observation_id)
            since = now - timedelta(
                seconds=settings.INSTRADAR_CIRCUIT_BREAKER_WINDOW_SECONDS)
            safety_kinds = ("quarantined", "feedback_cooldown", "rate_cooldown")
            if observation.account_pool_id:
                recent = InstagramAccountHealthEvent.objects.filter(
                    account__pool_memberships__pool_id=observation.account_pool_id,
                    account__pool_memberships__is_active=True,
                    kind__in=safety_kinds, created_at__gte=since).distinct().count()
                affected = ObservationJob.objects.filter(
                    account_pool_id=observation.account_pool_id,
                    status__in=(ObservationJob.Status.QUEUED,
                                ObservationJob.Status.RUNNING))
            else:
                recent = InstagramAccountHealthEvent.objects.filter(
                    observation=observation, kind__in=safety_kinds,
                    created_at__gte=since).count()
                affected = ObservationJob.objects.filter(
                    pk=observation.pk,
                    status__in=(ObservationJob.Status.QUEUED,
                                ObservationJob.Status.RUNNING))
            if recent >= settings.INSTRADAR_CIRCUIT_BREAKER_EVENTS:
                affected_ids = list(affected.values_list("pk", flat=True))
                affected.update(
                    status=ObservationJob.Status.PAUSED,
                    error=("Circuit Breaker: چند هشدار ایمنی Instagram در بازهٔ کوتاه؛ "
                           "رصدها برای بررسی دستی متوقف شدند"),
                    updated_at=now)
                CommentSyncJob.objects.filter(
                    observation_id__in=affected_ids,
                    status=CommentSyncJob.Status.QUEUED).update(scheduled_at=None)
    return category


@transaction.atomic
def dispatch_comment_jobs(observation, enqueue=None):
    """Reserve queue slots once so repeated orchestrator ticks stay idempotent."""
    observation = ObservationJob.objects.select_for_update().select_related(
        "account_pool").get(pk=observation.pk)
    now = timezone.now()
    stale = now - timedelta(seconds=settings.INSTRADAR_COMMENT_DISPATCH_STALE_SECONDS)
    CommentSyncJob.objects.filter(
        observation=observation, status=CommentSyncJob.Status.QUEUED,
        scheduled_at__lt=stale).update(scheduled_at=None)
    active = CommentSyncJob.objects.filter(
        observation=observation).filter(
            Q(status=CommentSyncJob.Status.RUNNING) |
            Q(status=CommentSyncJob.Status.QUEUED, scheduled_at__isnull=False)
        ).count()
    slots = max(0, comment_concurrency(observation) - active)
    if not slots:
        return 0
    rows = list(CommentSyncJob.objects.select_for_update().filter(
        observation=observation, status=CommentSyncJob.Status.QUEUED,
        scheduled_at__isnull=True,
    ).filter(Q(lease_until__isnull=True) | Q(lease_until__lte=now)).order_by(
        "updated_at", "created_at", "pk")[:slots])
    if not rows:
        return 0
    CommentSyncJob.objects.filter(pk__in=[row.pk for row in rows]).update(
        scheduled_at=now)
    if enqueue is None:
        from .comment_tasks import queue_comments as enqueue
    for row in rows:
        transaction.on_commit(lambda item=row: enqueue(item))
    return len(rows)


@transaction.atomic
def reset_account_health(account):
    account = InstagramAccount.objects.select_for_update().get(pk=account.pk)
    now = timezone.now()
    if account.cooldown_until and account.cooldown_until > now:
        raise ValueError("Cooldown هنوز تمام نشده است")
    if (account.health_status == InstagramAccount.Health.QUARANTINED or
            account.status == "needs_login"):
        raise ValueError("برای خروج از قرنطینه باید نشست تازه از Chrome دریافت شود")
    account.health_status = InstagramAccount.Health.READY
    account.health_score = max(50, account.health_score)
    account.cooldown_until = None
    account.consecutive_failures = 0
    account.last_error = ""
    if account.encrypted_cookies and account.status == "error":
        account.status = "captured"
    account.save(update_fields=[
        "health_status", "health_score", "cooldown_until", "consecutive_failures",
        "last_error", "status"])
    InstagramAccountHealthEvent.objects.create(
        account=account, kind="manual_reset", message="بازنشانی دستی سلامت حساب")
    return account
