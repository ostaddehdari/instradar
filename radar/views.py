from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.db import IntegrityError, connection
from django.db import transaction
from django.db.models import (Case, Count, Exists, F, OuterRef, Q, Subquery,
                              Value, When)
from django.db.models.functions import Coalesce
from django.http import FileResponse, Http404, HttpResponse, JsonResponse
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST
from . import browser
from .crypto import seal
from .models import (AccountRelationship, Comment, CommentSyncEvent, CommentSyncJob, CrawlJob,
                     GraphWorkspace, InstagramAccount, InstagramAccountLease, InstagramAccountPool,
                     InstagramAccountPoolMember,
                     InstagramIdentity, JobLogCapture, MediaAsset, MediaSyncEvent,
                     MediaSyncJob, ObservationJob, Post, PostLike, PostSnapshot, PostSyncCursor, ProfileSnapshot,
                     PublishAttempt, PublishItem, PublishJob, PublishJobDestination,
                     RelationshipChange, RelationshipScanJob, RelationshipSnapshot,
                     RelationshipScanEvent, RelationshipSyncState, SiteSettings, TelegramAccount,
                     TelegramDestination, TelegramMessage, TelegramSettings)
from .tasks import enqueue
from .media_tasks import queue_media
from .comment_tasks import queue_comments
from .monitor_tasks import finish_job_log_capture
from .monitoring import metric_payload
from .relationship_tasks import queue_relationships
from .graph_engine import GraphOptions, build_graph
from .publish_tasks import publish_next, queue_publish
from .observation_tasks import queue_observation
from .observation_progress import (TASK_META, decorate_observation,
                                   enabled_task_keys, normalized_task_order)
from .telegram_service import (TelegramServiceError, request_login_code,
                               sync_destinations, verify_login)
from datetime import date, timedelta
from django.utils import timezone
import asyncio
import csv
import io
import json
import re
from xml.sax.saxutils import escape
from .relationship_target import known_relationship_target
from .session import (InstagramSessionError, aiograpi_client,
                      normalize_browser_cookies, session_user_id)
from .account_pool import reset_account_health


@never_cache
@login_required
def dashboard(request):
    observation_jobs = list(ObservationJob.objects.filter(owner=request.user).exclude(
        status=ObservationJob.Status.ARCHIVED).select_related(
            "account", "account_pool", "media_job", "relationship_job", "publish_job", "crawl_session"
        ).prefetch_related(
            "media_job__streams", "relationship_job__streams", "comment_jobs"
        ).order_by("-created_at")[:8])
    for job in observation_jobs:
        _decorate_observation_progress(job)
    return render(request, "radar/dashboard.html", {
        "accounts_count": InstagramAccount.objects.filter(owner=request.user, is_active=True).count(),
        "snapshots_count": ProfileSnapshot.objects.filter(job__owner=request.user).count(),
        "posts_count": Post.objects.filter(postsnapshot__job__owner=request.user).distinct().count(),
        "contacts_count": InstagramIdentity.objects.filter(
            pk__in=_visible_identity_ids(request.user)).count(),
        "active_relations_count": AccountRelationship.objects.filter(
            pk__in=_visible_relationship_ids(request.user), is_active=True).count(),
        "observation_jobs": observation_jobs})


@never_cache
@login_required
def accounts(request):
    if request.method == "POST":
        bulk = request.POST.get("bulk_usernames", "").strip()
        if bulk:
            values = list(dict.fromkeys(
                item.strip().lstrip("@").lower()
                for item in re.split(r"[\s,;]+", bulk) if item.strip()))
            invalid = [item for item in values if (
                len(item) > 100 or not item or
                not all(c.isascii() and (c.isalnum() or c in "._") for c in item))]
            if invalid or len(values) > 500:
                messages.error(request, "فهرست نام‌های کاربری نامعتبر است یا بیش از ۵۰۰ مورد دارد.")
            else:
                existing = set(InstagramAccount.objects.filter(
                    owner=request.user, username__in=values, is_active=True).values_list(
                        "username", flat=True))
                rows = [InstagramAccount(owner=request.user, username=item)
                        for item in values if item not in existing]
                InstagramAccount.objects.bulk_create(rows, batch_size=250)
                messages.success(request, f"{len(rows)} حساب ساخته شد؛ ورود هر نشست باید در Chrome تأیید شود.")
        else:
            username = request.POST.get("username", "").strip().lstrip("@").lower()
            if not username or len(username) > 100 or not all(
                    c.isascii() and (c.isalnum() or c in "._") for c in username):
                messages.error(request, "نام کاربری معتبر وارد کنید.")
            else:
                InstagramAccount.objects.create(owner=request.user, username=username)
                messages.success(request, "حساب ایجاد شد؛ برای ورود مرورگر را باز کنید.")
        return redirect("radar:accounts")
    account_rows = list(InstagramAccount.objects.filter(
        owner=request.user, is_active=True).prefetch_related(
            "pool_memberships__pool").order_by("-id"))
    pools = list(InstagramAccountPool.objects.filter(
        owner=request.user, is_active=True).prefetch_related(
            "members__account").order_by("name"))
    for pool in pools:
        member_ids = {member.account_id for member in pool.members.all()}
        pool.account_options = [(account, account.pk in member_ids)
                                for account in account_rows]
    return render(request, "radar/accounts.html", {
        "accounts": account_rows, "pools": pools,
        "pool_concurrency_cap": settings.INSTRADAR_POOL_MAX_CONCURRENCY,
        "pool_hourly_cap": settings.INSTRADAR_POOL_HARD_HOURLY_LIMIT,
        "pool_daily_cap": settings.INSTRADAR_POOL_HARD_DAILY_LIMIT,
        "account_hourly_cap": settings.INSTRADAR_ACCOUNT_HARD_HOURLY_LIMIT,
        "account_daily_cap": settings.INSTRADAR_ACCOUNT_HARD_DAILY_LIMIT,
        "account_interval_floor": settings.INSTRADAR_ACCOUNT_MIN_INTERVAL_FLOOR,
    })


def _pool_values(request):
    name = request.POST.get("name", "").strip()
    concurrency = int(request.POST.get("max_concurrency") or 2)
    hourly = int(request.POST.get("hourly_request_limit") or 300)
    daily = int(request.POST.get("daily_request_limit") or 2000)
    if not name or len(name) > 100:
        raise ValueError("نام Pool معتبر نیست")
    if not 1 <= concurrency <= settings.INSTRADAR_POOL_MAX_CONCURRENCY:
        raise ValueError("هم‌زمانی Pool خارج از سقف ایمنی است")
    if (not 1 <= hourly <= settings.INSTRADAR_POOL_HARD_HOURLY_LIMIT or
            not 1 <= daily <= settings.INSTRADAR_POOL_HARD_DAILY_LIMIT):
        raise ValueError("بودجهٔ درخواست Pool معتبر نیست")
    account_ids = [int(value) for value in request.POST.getlist("account_ids")
                   if value.isdigit()]
    return name, concurrency, hourly, daily, account_ids


@login_required
@require_POST
def create_account_pool(request):
    try:
        name, concurrency, hourly, daily, account_ids = _pool_values(request)
        with transaction.atomic():
            pool = InstagramAccountPool.objects.create(
                owner=request.user, name=name, max_concurrency=concurrency,
                hourly_request_limit=hourly, daily_request_limit=daily)
            accounts_qs = list(InstagramAccount.objects.filter(
                owner=request.user, is_active=True, pk__in=account_ids))
            InstagramAccountPoolMember.objects.bulk_create([
                InstagramAccountPoolMember(pool=pool, account=account)
                for account in accounts_qs
            ])
        messages.success(request, f"Pool «{pool.name}» با {len(accounts_qs)} عضو ذخیره شد.")
    except ValueError as exc:
        messages.error(request, str(exc))
    except IntegrityError:
        messages.error(request, "Pool دیگری با همین نام وجود دارد.")
    return redirect("radar:accounts")


@login_required
@require_POST
def update_account_pool(request, pk):
    pool = get_object_or_404(InstagramAccountPool, pk=pk, owner=request.user)
    try:
        name, concurrency, hourly, daily, account_ids = _pool_values(request)
    except ValueError as exc:
        messages.error(request, str(exc))
        return redirect("radar:accounts")
    try:
        with transaction.atomic():
            pool.name = name
            pool.max_concurrency = concurrency
            pool.hourly_request_limit = hourly
            pool.daily_request_limit = daily
            pool.save(update_fields=[
                "name", "max_concurrency", "hourly_request_limit",
                "daily_request_limit", "updated_at"])
            owned_ids = set(InstagramAccount.objects.filter(
                owner=request.user, is_active=True, pk__in=account_ids).values_list(
                    "pk", flat=True))
            pool.members.exclude(account_id__in=owned_ids).delete()
            InstagramAccountPoolMember.objects.bulk_create([
                InstagramAccountPoolMember(pool=pool, account_id=account_id)
                for account_id in owned_ids
            ], ignore_conflicts=True)
    except IntegrityError:
        messages.error(request, "Pool دیگری با همین نام وجود دارد.")
        return redirect("radar:accounts")
    messages.success(request, f"تنظیمات Pool «{pool.name}» ذخیره شد.")
    return redirect("radar:accounts")


@login_required
@require_POST
def disable_account_pool(request, pk):
    pool = get_object_or_404(InstagramAccountPool, pk=pk, owner=request.user)
    if pool.observation_jobs.filter(status__in=("queued", "running")).exists():
        messages.error(request, "این Pool رصد فعال دارد؛ ابتدا رصد را متوقف کنید.")
    else:
        pool.is_active = False
        pool.save(update_fields=["is_active", "updated_at"])
        messages.success(request, "Pool غیرفعال شد؛ حساب‌ها و سوابق حذف نشدند.")
    return redirect("radar:accounts")


@login_required
@require_POST
def reset_instagram_account_health(request, pk):
    account = get_object_or_404(InstagramAccount, pk=pk, owner=request.user, is_active=True)
    try:
        reset_account_health(account)
    except ValueError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, f"سلامت @{account.username} بازنشانی شد.")
    return redirect("radar:accounts")


@login_required
@require_POST
def update_instagram_account_safety(request, pk):
    account = get_object_or_404(InstagramAccount, pk=pk, owner=request.user, is_active=True)
    try:
        hourly = int(request.POST.get("hourly_request_limit") or 60)
        daily = int(request.POST.get("daily_request_limit") or 300)
        interval = int(request.POST.get("min_interval_seconds") or 10)
        if (not 1 <= hourly <= settings.INSTRADAR_ACCOUNT_HARD_HOURLY_LIMIT or
                not 1 <= daily <= settings.INSTRADAR_ACCOUNT_HARD_DAILY_LIMIT or
                not settings.INSTRADAR_ACCOUNT_MIN_INTERVAL_FLOOR <= interval <= 3600):
            raise ValueError
    except ValueError:
        messages.error(request, "مقادیر ایمنی حساب معتبر نیستند.")
        return redirect("radar:accounts")
    account.hourly_request_limit = hourly
    account.daily_request_limit = daily
    account.min_interval_seconds = interval
    account.save(update_fields=[
        "hourly_request_limit", "daily_request_limit", "min_interval_seconds"])
    messages.success(request, f"سقف‌های ایمنی @{account.username} ذخیره شد.")
    return redirect("radar:accounts")


@login_required
@require_POST
def launch(request, pk):
    account = get_object_or_404(InstagramAccount, pk=pk, owner=request.user)
    if InstagramAccountLease.objects.filter(
            account=account, expires_at__gt=timezone.now()).exists():
        messages.error(request, "این حساب در حال اجرای یک صفحهٔ رصد است؛ کمی بعد Chrome را باز کنید.")
        return redirect("radar:accounts")
    try:
        browser.call("launch", account.id)
        messages.success(request, "Chrome در noVNC باز شد. پس از ورود، «دریافت نشست» را بزنید.")
    except Exception as exc:
        messages.error(request, f"مرورگر آماده نیست ({type(exc).__name__}).")
    return redirect("radar:accounts")


@login_required
@require_POST
def capture(request, pk):
    account = get_object_or_404(InstagramAccount, pk=pk, owner=request.user)
    if InstagramAccountLease.objects.filter(
            account=account, expires_at__gt=timezone.now()).exists():
        messages.error(request, "این حساب اکنون در حال استفاده است؛ پس از پایان صفحه دوباره تلاش کنید.")
        return redirect("radar:accounts")
    try:
        data = browser.call("capture", account.id)
        cookies = normalize_browser_cookies(data["cookies"])
        user_agent = str(data.get("user_agent") or "").strip()
        cookie_values = {item.get("name"): item.get("value") for item in cookies
                         if isinstance(item, dict) and item.get("domain", "").lower().lstrip(".").endswith("instagram.com")}
        sessionid = str(cookie_values.get("sessionid") or "")
        instagram_pk = session_user_id(sessionid)
        if not user_agent or not sessionid or not instagram_pk or not cookie_values.get("csrftoken"):
            raise ValueError("Instagram authentication cookies are incomplete")
        account.encrypted_cookies = seal(cookies)
        # Never mix a fresh browser session with cached Private API
        # Authorization/cookies. Stable device data is rebuilt by session.py.
        account.encrypted_aiograpi_settings = None
        account.instagram_pk = instagram_pk
        account.browser_user_agent = user_agent
        account.status = "captured"
        account.last_error = ""
        account.verified_at = None
        account.browser_captured_at = timezone.now()
        account.save(update_fields=["encrypted_cookies", "encrypted_aiograpi_settings",
                                    "instagram_pk", "browser_user_agent", "status", "last_error", "verified_at",
                                    "browser_captured_at"])
        client = asyncio.run(aiograpi_client(account))
        authenticated_username = (client.username or "").strip().lstrip("@").lower()
        if not authenticated_username:
            raise InstagramSessionError("Instagram username was not returned")
        client_settings = client.get_settings()
        account.username = authenticated_username
        account.instagram_pk = instagram_pk
        account.encrypted_aiograpi_settings = seal(client_settings) if isinstance(client_settings, dict) else None
        account.status = "active"
        account.health_status = InstagramAccount.Health.READY
        account.health_score = max(50, account.health_score)
        account.cooldown_until = None
        account.consecutive_failures = 0
        account.verified_at = timezone.now()
        account.last_error = ""
        account.save(update_fields=["username", "instagram_pk", "encrypted_aiograpi_settings",
                                    "status", "health_status", "health_score",
                                    "cooldown_until", "consecutive_failures",
                                    "last_error", "verified_at"])
        api_mode = ("MOBILE + WEB" if getattr(client, "_instradar_private_ready", True)
                    else "WEB COOKIE FALLBACK")
        messages.success(request, f"Account: @{authenticated_username} | Instagram ID: {instagram_pk} | Session: VALID | Browser: AUTHENTICATED | API: {api_mode}")
    except Exception as exc:
        account.status = "needs_login"
        account.last_error = str(exc)[:500] or "Instagram session validation failed"
        account.save(update_fields=["status", "last_error"])
        messages.error(request, f"اعتبارسنجی نشست Instagram ناموفق بود ({type(exc).__name__}: {str(exc)[:120]}).")
    return redirect("radar:accounts")


@never_cache
@login_required
def jobs(request):
    observations = list(ObservationJob.objects.filter(owner=request.user).exclude(
        status=ObservationJob.Status.ARCHIVED).select_related(
            "account", "account_pool", "media_job", "relationship_job", "publish_job", "crawl_session"
        ).prefetch_related(
            "media_job__streams", "relationship_job__streams", "comment_jobs"
        ).order_by("-priority", "created_at")[:200])
    for job in observations:
        _decorate_observation_progress(job)
    transfer_query = PublishJob.objects.filter(
        owner=request.user, observation__isnull=True).select_related(
            "telegram_account", "instagram_account").order_by("-created_at")
    transfer_active = transfer_query.filter(
        status__in=("waiting", "queued", "running")).count()
    transfers = list(transfer_query[:100])
    return render(request, "radar/jobs.html", {
        "observations": observations, "transfers": transfers,
        "active_count": sum(1 for item in observations if item.status in ("queued", "running")) +
                        transfer_active,
    })


def _decorate_observation_progress(job):
    return decorate_observation(job)


@login_required
@require_POST
def reorder_jobs(request):
    ordered = request.POST.getlist("job_ids[]") or request.POST.getlist("job_ids")
    owned = {str(pk) for pk in ObservationJob.objects.filter(
        owner=request.user, pk__in=ordered).values_list("pk", flat=True)}
    clean = [value for value in ordered if value in owned]
    with transaction.atomic():
        for index, value in enumerate(clean):
            # Earlier rows get a larger queue priority.
            priority = max(1, min(99, len(clean) - index))
            ObservationJob.objects.filter(pk=value, owner=request.user).update(priority=priority)
    return JsonResponse({"ok": True, "updated": len(clean)})


@login_required
@require_POST
def reorder_job_stages(request, pk):
    job = get_object_or_404(ObservationJob, pk=pk, owner=request.user)
    requested = request.POST.getlist("stages[]") or request.POST.getlist("stages")
    allowed = enabled_task_keys(job)
    legacy = {"media": ("posts", "reels"),
              "relationships": ("followers", "following")}
    clean = []
    for item in requested:
        for key in legacy.get(item, (item,)):
            if key in allowed and key not in clean:
                clean.append(key)
    clean += [item for item in allowed if item not in clean]
    job.stage_order = clean
    job.save(update_fields=["stage_order", "updated_at"])
    if job.status in (ObservationJob.Status.QUEUED, ObservationJob.Status.RUNNING):
        try:
            queue_observation(job)
        except Exception:
            pass
    return JsonResponse({"ok": True, "stage_order": clean})


@login_required
@require_POST
def new_job(request):
    account = get_object_or_404(InstagramAccount, pk=request.POST.get("account_id"), owner=request.user)
    target = request.POST.get("target", "").strip().lstrip("@").lower()
    if not target or len(target) > 100 or not all(c.isalnum() or c in "._" for c in target):
        messages.error(request, "نام کاربری هدف نامعتبر است.")
    elif not account.encrypted_cookies:
        messages.error(request, "ابتدا ورود noVNC را کامل کنید.")
    else:
        job = CrawlJob.objects.create(owner=request.user, account=account, target=target)
        try:
            enqueue(job)
        except Exception:
            messages.warning(request, "جاب در دیتابیس ثبت شد؛ Worker در بازیابی دوره‌ای آن را اجرا خواهد کرد.")
        else:
            messages.success(request, "رصد پروفایل در صف قرار گرفت.")
    return redirect("radar:jobs")


@login_required
@require_POST
def resume_job(request, pk):
    job = get_object_or_404(CrawlJob, pk=pk, owner=request.user)
    if job.status in (CrawlJob.Status.FAILED, CrawlJob.Status.PAUSED):
        job.status = CrawlJob.Status.QUEUED
        job.error = ""
        job.save(update_fields=["status", "error", "updated_at"])
        try:
            enqueue(job)
        except Exception:
            pass
    return redirect("radar:jobs")


@never_cache
def health(request):
    version_file = settings.BASE_DIR / "VERSION"
    try:
        runtime_version = version_file.read_text(encoding="utf-8").strip()
    except Exception:
        runtime_version = "unknown"
    result = {"version": runtime_version, "database": False, "browser": False,
              "worker": False, "telegram": False}
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            result["database"] = cursor.fetchone()[0] == 1
    except Exception:
        pass
    try:
        status = browser.call("health")
        result["browser"] = status["display"] == ":99" and status["x_socket"]
    except Exception:
        pass
    try:
        from redis import Redis
        redis = Redis.from_url(settings.CELERY_BROKER_URL, socket_timeout=1)
        result["worker"] = bool(redis.get("instradar:worker:heartbeat"))
        result["telegram"] = bool(redis.get("instradar:telegram:heartbeat"))
    except Exception:
        pass
    return JsonResponse(result, status=200 if all((result["database"], result["browser"],
                                                   result["worker"], result["telegram"])) else 503)


@never_cache
@login_required
def observe(request):
    show_archived = request.GET.get("archived") == "1"
    jobs = ObservationJob.objects.filter(owner=request.user)
    jobs = (jobs.filter(status=ObservationJob.Status.ARCHIVED) if show_archived
            else jobs.exclude(status=ObservationJob.Status.ARCHIVED))
    visible_jobs = list(jobs.select_related(
        "account", "account_pool", "telegram_account", "media_job", "relationship_job",
        "publish_job", "crawl_session").prefetch_related(
            "media_job__streams", "relationship_job__streams", "comment_jobs"
        ).order_by("-priority", "created_at")[:100])
    for item in visible_jobs:
        _decorate_observation_progress(item)
    return render(request, "radar/observe.html", {
        "accounts": InstagramAccount.objects.filter(owner=request.user, is_active=True).order_by("username"),
        "account_pools": InstagramAccountPool.objects.filter(
            owner=request.user, is_active=True,
            members__is_active=True, members__account__is_active=True,
            members__account__encrypted_cookies__isnull=False,
        ).distinct().prefetch_related("members__account").order_by("name"),
        "pool_concurrency_cap": settings.INSTRADAR_POOL_MAX_CONCURRENCY,
        "jobs": visible_jobs,
        "show_archived": show_archived,
        "telegram_accounts": TelegramAccount.objects.filter(
            owner=request.user, is_active=True, status=TelegramAccount.Status.ACTIVE).prefetch_related(
                "destinations").order_by("title"),
        "transfer_targets": _visible_posts(request.user).exclude(owner_username="").values_list(
            "owner_username", flat=True).distinct().order_by("owner_username")})


@login_required
@require_POST
def create_media_job(request):
    pool_id = request.POST.get("account_pool_id", "").strip()
    account_pool = None
    if pool_id:
        account_pool = get_object_or_404(
            InstagramAccountPool, owner=request.user, is_active=True, pk=pool_id)
        pool_accounts = InstagramAccount.objects.filter(
            owner=request.user, is_active=True, encrypted_cookies__isnull=False,
            pool_memberships__pool=account_pool,
            pool_memberships__is_active=True,
        ).exclude(health_status=InstagramAccount.Health.QUARANTINED).order_by(
            "pool_memberships__last_used_at", "pk")
        account = pool_accounts.first()
        if account is None:
            messages.error(request, "Pool انتخاب‌شده هیچ حساب آماده و نشست‌داری ندارد.")
            return redirect("radar:observe")
    else:
        account = get_object_or_404(
            InstagramAccount, owner=request.user, pk=request.POST.get("account_id"))
    target = request.POST.get("target", "").strip().lstrip("@").lower()
    mode = request.POST.get("mode", "full")
    def parsed_date(value):
        if not value:
            return None
        try:
            return date.fromisoformat(value)
        except ValueError:
            from .jalali import parse_jalali
            return parse_jalali(value)
    try:
        date_from = parsed_date(request.POST.get("date_from", ""))
        date_to = parsed_date(request.POST.get("date_to", ""))
        max_pages = int(request.POST.get("max_pages") or 0)
        priority = int(request.POST.get("priority") or 5)
        max_active_accounts = int(request.POST.get("max_active_accounts") or (
            account_pool.max_concurrency if account_pool else 1))
    except ValueError:
        messages.error(request, "بازهٔ تاریخ یا سقف صفحه نامعتبر است.")
        return redirect("radar:observe")
    telegram_account_id = request.POST.get("telegram_account_id", "").strip()
    destination_ids = [value for value in request.POST.getlist("telegram_destination_ids")
                       if value.isdigit()]
    telegram_account = None
    destinations = TelegramDestination.objects.none()
    if telegram_account_id:
        telegram_account = get_object_or_404(
            TelegramAccount, pk=telegram_account_id, owner=request.user,
            status=TelegramAccount.Status.ACTIVE)
        destinations = TelegramDestination.objects.filter(
            pk__in=destination_ids, account=telegram_account, is_active=True, can_post=True)
    legacy_form = request.POST.get("options_present") != "1"
    include_posts = legacy_form or request.POST.get("include_posts") == "on"
    include_reels = legacy_form or request.POST.get("include_reels") == "on"
    include_followers = request.POST.get("include_followers") == "on"
    include_following = request.POST.get("include_following") == "on"
    include_comments = request.POST.get("include_comments") == "on"
    include_likes = request.POST.get("include_likes") == "on"
    comments_from_archive = include_comments and not (include_posts or include_reels)
    if not any((include_posts, include_reels, include_followers,
                include_following, include_comments)):
        messages.error(request, "حداقل یک بخش برای رصد انتخاب کنید.")
    elif not target or len(target) > 100 or not all(c.isascii() and (c.isalnum() or c in "._") for c in target):
        messages.error(request, "نام کاربری هدف نامعتبر است.")
    elif telegram_account and not (include_posts or include_reels):
        messages.error(request, "برای انتقال هم‌زمان، پست یا ریلز را هم انتخاب کنید.")
    elif telegram_account and not destinations.exists():
        messages.error(request, "برای ارسال هم‌زمان، دست‌کم یک کانال یا گروه انتخاب کنید.")
    elif mode not in ("full", "incremental") or (mode == "incremental" and (date_from or date_to)):
        messages.error(request, "حالت همگام‌سازی نامعتبر است؛ بازهٔ تاریخ فقط برای پیمایش کامل است.")
    elif (date_from and date_to and date_from > date_to) or not 0 <= max_pages <= 10000 or not 1 <= priority <= 9:
        messages.error(request, "بازهٔ تاریخ یا سقف صفحه نامعتبر است.")
    elif not 1 <= max_active_accounts <= settings.INSTRADAR_POOL_MAX_CONCURRENCY:
        messages.error(request, "تعداد حساب هم‌زمان خارج از سقف ایمنی سامانه است.")
    elif account_pool and max_active_accounts > account_pool.max_concurrency:
        messages.error(request, "تعداد حساب هم‌زمان از سقف Pool بیشتر است.")
    elif not account_pool and max_active_accounts != 1:
        messages.error(request, "برای حساب تکی، هم‌زمانی باید یک باشد.")
    elif not account.encrypted_cookies:
        messages.error(request, "ابتدا نشست حساب را با noVNC دریافت کنید.")
    elif not account_pool and account.health_status == InstagramAccount.Health.QUARANTINED:
        messages.error(request, "این حساب در قرنطینه است؛ پس از بررسی نشست، سلامت آن را بازنشانی کنید.")
    else:
        task_order = []
        if include_posts:
            task_order.append("posts")
        if include_reels:
            task_order.append("reels")
        if include_followers:
            task_order.append("followers")
        if include_following:
            task_order.append("following")
        if include_comments:
            task_order.append("comments")
        if telegram_account:
            task_order.append("telegram")
        with transaction.atomic():
            job = ObservationJob.objects.create(
                owner=request.user, account=account, account_pool=account_pool,
                max_active_accounts=max_active_accounts,
                target=target, mode=mode,
                include_posts=include_posts, include_reels=include_reels,
                include_followers=include_followers, include_following=include_following,
                include_comments=include_comments, include_likes=include_likes,
                stage_order=task_order,
                publish_to_telegram=bool(telegram_account),
                telegram_account=telegram_account, caption_template=request.POST.get(
                    "caption_template", "")[:4000], priority=priority,
                date_from=date_from, date_to=date_to, max_pages=max_pages)
            if telegram_account:
                job.telegram_destinations.set(destinations)
        try:
            queue_observation(job)
        except Exception:
            messages.warning(request, "جاب ذخیره شد؛ بازیابی دوره‌ای Worker آن را اجرا می‌کند.")
        if comments_from_archive:
            messages.info(
                request,
                "رصد کامنت ثبت شد؛ فقط پست‌های قبلاً استخراج‌شدهٔ همین پیج بررسی می‌شوند.",
            )
        return redirect("radar:observation_detail", job.pk)
    return redirect("radar:observe")


@never_cache
@login_required
def observation_detail(request, pk):
    job = get_object_or_404(ObservationJob.objects.select_related(
        "account", "account_pool", "telegram_account", "media_job", "relationship_job",
        "publish_job", "crawl_session").prefetch_related(
            "media_job__streams", "relationship_job__streams", "comment_jobs__post",
            "comment_jobs__account"
        ), pk=pk, owner=request.user)
    media = getattr(job, "media_job", None)
    relation = getattr(job, "relationship_job", None)
    publish = getattr(job, "publish_job", None)
    _decorate_observation_progress(job)
    return render(request, "radar/observation_detail.html", {
        "job": job, "media": media, "relation": relation, "publish": publish,
        "comment_jobs": list(job.comment_jobs.all())[:100],
        "subtasks": job.subtasks,
        "posts_count": PostSnapshot.objects.filter(job=media).count() if media else 0,
        "comments_count": Comment.objects.filter(last_seen_job__observation=job).count(),
        "relationships_count": RelationshipSnapshot.objects.filter(job=relation).count() if relation else 0,
        "job_events": _observation_events(job, media, relation, publish),
        "log_captures": _observation_log_captures(
            request.user, job, media, relation, publish),
    })


@login_required
@require_POST
def pause_observation(request, pk):
    job = get_object_or_404(ObservationJob, pk=pk, owner=request.user)
    with transaction.atomic():
        ObservationJob.objects.filter(pk=job.pk).update(status="paused")
        MediaSyncJob.objects.filter(observation=job, status__in=("queued", "running")).update(status="paused", lease_until=None)
        RelationshipScanJob.objects.filter(observation=job, status__in=("queued", "running")).update(status="paused", lease_until=None)
        CommentSyncJob.objects.filter(observation=job, status__in=("queued", "running")).update(
            status="paused", lease_until=None, scheduled_at=None)
        PublishJob.objects.filter(observation=job, status__in=("waiting", "queued", "running")).update(status="paused", lease_until=None)
    messages.info(request, "رصد جامع در نقطهٔ امن متوقف شد.")
    return redirect("radar:observation_detail", pk)


@login_required
@require_POST
def resume_observation(request, pk):
    job = get_object_or_404(ObservationJob, pk=pk, owner=request.user)
    with transaction.atomic():
        ObservationJob.objects.filter(pk=job.pk).update(
            status="queued", error="", completed_at=None)
        media = MediaSyncJob.objects.filter(observation=job).first()
        if media and media.status in ("paused", "failed", "partial"):
            for stream in media.streams.exclude(issue=""):
                capped = "سقف صفحه" in stream.issue
                stream.issue = ""
                stream.finished = False
                if not capped:
                    stream.cursor = ""
                    stream.pages = 0
                    stream.seen = 0
                    stream.api = "private"
                    stream.top_media_pk = ""
                    stream.last_shortcode = ""
                stream.save(update_fields=["issue", "finished", "cursor", "pages", "seen",
                                           "api", "top_media_pk", "last_shortcode"])
            if media.status == "partial" and not media.streams.filter(finished=False).exists():
                fallback_name = "timeline" if job.include_posts else "reels"
                media.streams.filter(stream=fallback_name).update(
                    finished=False, cursor="", pages=0, seen=0, api="private",
                    top_media_pk="", last_shortcode="")
            media.status, media.error, media.lease_until, media.attempts = "queued", "", None, 0
            media.save(update_fields=["status", "error", "lease_until", "attempts", "updated_at"])
        relation = RelationshipScanJob.objects.filter(observation=job).first()
        if relation and relation.status in ("paused", "failed", "partial"):
            for state in relation.streams.exclude(issue=""):
                capped = "سقف صفحه" in state.issue
                state.issue, state.finished, state.reconciled = "", False, False
                if not capped:
                    state.cursor = ""
                    state.pages, state.seen, state.api = 0, 0, "private_gql"
                state.save(update_fields=["issue", "finished", "reconciled",
                                          "cursor", "pages", "seen", "api"])
            relation.status, relation.error = "queued", ""
            relation.lease_until, relation.completed_at, relation.attempts = None, None, 0
            relation.save(update_fields=["status", "error", "lease_until", "completed_at",
                                         "attempts", "updated_at"])
        CommentSyncJob.objects.filter(
            observation=job, status__in=("paused", "failed", "partial")).update(
                status="queued", lease_until=None, scheduled_at=None, attempts=0, error="")
        publish = PublishJob.objects.filter(observation=job).first()
        if publish and publish.status in ("paused", "failed", "partial"):
            publish.items.filter(status__in=(PublishItem.Status.FAILED,
                                             PublishItem.Status.RUNNING)).update(
                status=PublishItem.Status.QUEUED, attempts=0, error="")
            publish.status, publish.error = "queued", ""
            publish.lease_until, publish.completed_at = None, None
            publish.save(update_fields=["status", "error", "lease_until",
                                        "completed_at", "updated_at"])
    job.refresh_from_db()
    queue_observation(job)
    messages.success(request, "رصد از آخرین Cursor ذخیره‌شده ادامه پیدا می‌کند.")
    return redirect("radar:observation_detail", pk)


@login_required
@require_POST
def archive_observation(request, pk):
    job = get_object_or_404(ObservationJob, pk=pk, owner=request.user)
    if job.status in ("queued", "running"):
        messages.error(request, "ابتدا جاب فعال را متوقف کنید.")
    else:
        job.status, job.archived_at = ObservationJob.Status.ARCHIVED, timezone.now()
        job.save(update_fields=["status", "archived_at", "updated_at"])
        messages.success(request, "جاب بایگانی شد و در فهرست اصلی نمایش داده نمی‌شود.")
    return redirect("radar:observe")


@login_required
@require_POST
def delete_observation(request, pk):
    job = get_object_or_404(ObservationJob, pk=pk, owner=request.user)
    if job.status in ("queued", "running"):
        messages.error(request, "برای حذف، ابتدا جاب را متوقف کنید.")
        return redirect("radar:observation_detail", pk)
    job.delete()
    messages.success(request, "جاب و زیرجاب‌های اجرایی آن حذف شد؛ داده‌های کشف‌شده حفظ شدند.")
    return redirect("radar:observe")


@never_cache
@login_required
def media_job_detail(request, pk):
    job = get_object_or_404(MediaSyncJob.objects.select_related("account"), pk=pk, owner=request.user)
    events = list(job.events.order_by("-created_at")[:200])
    return render(request, "radar/media_job_detail.html", {"job": job,
        "streams": job.streams.order_by("id"), "events": events,
        "job_events": [{"time": item.created_at, "stage": item.stream,
            "status": job.get_status_display(), "records": item.records,
            "duration_ms": item.duration_ms, "message": item.message} for item in events],
        "found": PostSnapshot.objects.filter(job=job).count(),
        "log_captures": JobLogCapture.objects.filter(owner=request.user, job_kind="media",
                                                      job_id=str(job.pk)).order_by("-started_at")[:8]})


@login_required
@require_POST
def pause_media_job(request, pk):
    job = get_object_or_404(MediaSyncJob, pk=pk, owner=request.user)
    MediaSyncJob.objects.filter(pk=job.pk, status__in=("running", "queued")).update(status="paused")
    return redirect("radar:media_job_detail", pk)


@login_required
@require_POST
def resume_media_job(request, pk):
    with transaction.atomic():
        job = get_object_or_404(MediaSyncJob.objects.select_for_update(), pk=pk, owner=request.user)
        if job.status not in ("failed", "paused", "partial"):
            return redirect("radar:media_job_detail", pk)
        if job.status == "partial":
            job.max_pages = 0  # no cap when resuming an intentionally capped scan
            for stream in job.streams.all():
                if not stream.issue and stream.finished:
                    continue
                stream.finished = False
                # Replay incomplete detail/cursor errors from the first page;
                # a page cap alone must keep its checkpoint to continue forward.
                if not stream.cursor or "سقف صفحه" not in stream.issue:
                    stream.cursor = ""
                    stream.api = "private"
                    stream.pages = 0
                    stream.seen = 0
                    stream.top_media_pk = ""
                    stream.last_shortcode = ""
                stream.issue = ""
                stream.save()
            if not job.streams.filter(finished=False).exists():
                job.streams.filter(stream="timeline").update(
                    finished=False, cursor="", pages=0, seen=0, api="private",
                    top_media_pk="", last_shortcode="")
        job.status = "queued"
        job.error = ""
        job.attempts = 0
        job.lease_until = None
        job.save()
    try:
        queue_media(job)
    except Exception:
        pass
    return redirect("radar:media_job_detail", pk)


def _visible_posts(user):
    return Post.objects.filter(postsnapshot__job__owner=user).distinct()


def _page_size(request, user):
    config, _ = SiteSettings.objects.get_or_create(owner=user)
    try:
        value = int(request.GET.get("page_size", config.default_page_size))
    except (TypeError, ValueError):
        value = config.default_page_size
    return value if value in (20, 40, 80, 120) else 40


def _filter_date(raw):
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        try:
            from .jalali import parse_jalali
            return parse_jalali(raw)
        except (TypeError, ValueError):
            return None


def _filter_int(raw):
    try:
        return max(0, int(raw)) if str(raw).strip() else None
    except (TypeError, ValueError):
        return None


def _clean_usernames(values):
    result = []
    for value in values:
        value = value.strip().lstrip("@").lower()[:100]
        if value and all(char.isascii() and (char.isalnum() or char in "._") for char in value):
            result.append(value)
    return list(dict.fromkeys(result))[:100]


def _post_filters(request, user):
    posts = _visible_posts(user).annotate(
        effective_views=Coalesce("view_count", "play_count"))
    kind = request.GET.get("kind", "all")
    if kind not in ("all", "posts", "reels"):
        kind = "all"
    if kind == "reels":
        posts = posts.filter(is_reel=True)
    elif kind == "posts":
        posts = posts.filter(is_reel=False)

    selected_profiles = _clean_usernames(
        request.GET.getlist("profiles") or request.GET.getlist("profile"))
    if selected_profiles:
        posts = posts.filter(owner_username__in=selected_profiles)
    query = request.GET.get("q", "").strip()[:200]
    if query:
        needle = query.lstrip("#@")
        posts = posts.filter(
            Q(caption__icontains=query) |
            Q(owner_username__icontains=needle) |
            Q(shortcode__icontains=needle) |
            Q(hashtags__tag__icontains=needle) |
            Q(mentions__username__icontains=needle)
        ).distinct()
    date_from = _filter_date(request.GET.get("date_from"))
    date_to = _filter_date(request.GET.get("date_to"))
    if date_from:
        posts = posts.filter(published_at__date__gte=date_from)
    if date_to:
        posts = posts.filter(published_at__date__lte=date_to)
    min_likes = _filter_int(request.GET.get("min_likes"))
    max_likes = _filter_int(request.GET.get("max_likes"))
    min_views = _filter_int(request.GET.get("min_views"))
    max_views = _filter_int(request.GET.get("max_views"))
    if min_likes is not None:
        posts = posts.filter(like_count__gte=min_likes)
    if max_likes is not None:
        posts = posts.filter(like_count__lte=max_likes)
    if min_views is not None:
        posts = posts.filter(effective_views__gte=min_views)
    if max_views is not None:
        posts = posts.filter(effective_views__lte=max_views)

    sort = request.GET.get("sort", "published_at")
    direction = "asc" if request.GET.get("dir") == "asc" else "desc"
    sort_map = {
        "page": "owner_username", "caption": "caption", "kind": "is_reel",
        "published_at": "published_at", "likes": "like_count",
        "views": "effective_views", "comments": "comment_count",
    }
    field = sort_map.get(sort, "published_at")
    prefix = "" if direction == "asc" else "-"
    posts = posts.order_by(f"{prefix}{field}", "-id")
    state = {
        "kind": kind, "query": query, "selected_profiles": selected_profiles,
        "date_from": request.GET.get("date_from", ""),
        "date_to": request.GET.get("date_to", ""),
        "min_likes": request.GET.get("min_likes", ""),
        "max_likes": request.GET.get("max_likes", ""),
        "min_views": request.GET.get("min_views", ""),
        "max_views": request.GET.get("max_views", ""),
        "sort": sort, "direction": direction,
    }
    return posts, state


@never_cache
@login_required
def post_list(request):
    profile_choices = list(_visible_posts(request.user).exclude(
        owner_username="").values_list("owner_username", flat=True).distinct().order_by(
            "owner_username"))
    posts, state = _post_filters(request, request.user)
    page_size = _page_size(request, request.user)
    context = {
        "page": Paginator(posts.prefetch_related("hashtags", "mentions"), page_size).get_page(
            request.GET.get("page")),
        "page_size": page_size, "profiles": profile_choices,
        "accounts": InstagramAccount.objects.filter(owner=request.user)
            .exclude(encrypted_cookies__isnull=True).order_by("username"),
    }
    context.update(state)
    return render(request, "radar/post_list.html", context)


def _excel_datetime(value):
    if not value:
        return ""
    from .jalali import gregorian_to_jalali
    local = timezone.localtime(value)
    year, month, day = gregorian_to_jalali(local.year, local.month, local.day)
    return f"{year:04d}/{month:02d}/{day:02d} {local:%H:%M:%S}"


@login_required
def export_posts_xlsx(request):
    from .xlsx_export import xlsx_response
    posts, _ = _post_filters(request, request.user)
    posts = posts.prefetch_related("hashtags", "mentions")

    def rows():
        for post in posts.iterator(chunk_size=500):
            yield (
                f"@{post.owner_username}", post.caption,
                "ریلز" if post.is_reel else "پست", post.comment_count,
                post.like_count, post.display_view_count,
                " ".join(f"#{item.tag}" for item in post.hashtags.all()),
                " ".join(f"@{item.username}" for item in post.mentions.all()),
                _excel_datetime(post.published_at), post.url,
            )

    return xlsx_response(
        ("پیج", "کپشن", "نوع", "تعداد کامنت", "تعداد پسند", "بازدید",
         "هشتگ‌ها", "منشن‌ها", "زمان انتشار", "لینک Instagram"),
        rows(), filename="instradar-posts.xlsx", sheet_name="پست‌ها و ریلزها",
        widths=(20, 70, 12, 14, 14, 14, 45, 45, 22, 42),
    )


@never_cache
@login_required
def post_detail(request, pk):
    post = get_object_or_404(_visible_posts(request.user), pk=pk)
    timeline = _visible_posts(request.user).filter(owner_username=post.owner_username)
    newer = timeline.filter(published_at__gt=post.published_at).order_by("published_at").first() if post.published_at else None
    older = timeline.filter(published_at__lt=post.published_at).order_by("-published_at").first() if post.published_at else None
    return render(request, "radar/post_detail.html", {"post": post,
        "newer": newer, "older": older,
        "accounts": InstagramAccount.objects.filter(owner=request.user, encrypted_cookies__isnull=False),
        "telegram_accounts": TelegramAccount.objects.filter(
            owner=request.user, is_active=True, status=TelegramAccount.Status.ACTIVE
        ).prefetch_related("destinations").order_by("title"),
        "recent_comments": post.comments.order_by("-published_at", "-id")[:20],
        "recent_likers": post.likers.select_related("identity").order_by("-last_seen_at")[:24],
        "stored_likers": post.likers.count(),
        "stored_comments": post.comments.count(),
        "assets": post.assets.order_by("position"), "hashtags": post.hashtags.order_by("tag"),
        "mentions": post.mentions.order_by("username"),
        "history": post.metric_history.filter(job__owner=request.user).order_by("-recorded_at")[:40]})






@login_required
@require_POST
def instant_publish_post(request, pk):
    """Send one post synchronously and outside the Telegram Celery queue."""
    post = get_object_or_404(_visible_posts(request.user), pk=pk)
    telegram = get_object_or_404(
        TelegramAccount, pk=request.POST.get("telegram_account_id"), owner=request.user,
        is_active=True, status=TelegramAccount.Status.ACTIVE)
    instagram = get_object_or_404(
        InstagramAccount, pk=request.POST.get("instagram_account_id"), owner=request.user,
        is_active=True, encrypted_cookies__isnull=False)
    destination_ids = [value for value in request.POST.getlist("telegram_destination_ids") if value.isdigit()]
    destinations = list(TelegramDestination.objects.filter(
        pk__in=destination_ids, account=telegram, is_active=True, can_post=True))
    if not destinations:
        messages.error(request, "حداقل یک کانال یا گروه مدیریتی انتخاب کنید.")
        return redirect("radar:post_detail", post.pk)
    with transaction.atomic():
        job = PublishJob.objects.create(
            owner=request.user, telegram_account=telegram, instagram_account=instagram,
            mode=PublishJob.Mode.TRANSFER, target_username=post.owner_username,
            total_items=1, caption_template=request.POST.get("caption_template", "")[:4000],
                force_resend=request.POST.get("force_resend") == "1")
        PublishJobDestination.objects.bulk_create([
            PublishJobDestination(job=job, destination=destination) for destination in destinations])
        PublishItem.objects.create(
            job=job, post=post, sequence=1,
            attempts=max(0, settings.INSTRADAR_TELEGRAM_MAX_RETRIES - 1))
    publish_next(str(job.pk))
    job.refresh_from_db()
    if job.status == PublishJob.Status.DONE:
        messages.success(request, f"پست همان لحظه به {len(destinations)} مقصد تلگرام ارسال شد.")
    else:
        messages.error(request, f"ارسال فوری کامل نشد: {job.error or job.get_status_display()}")
    return redirect("radar:post_detail", post.pk)


@never_cache
@login_required
def explore(request):
    posts = _visible_posts(request.user).prefetch_related("assets", "hashtags").order_by(
        "-discovered_at", "-published_at", "-id")
    return render(request, "radar/explore.html", {
        "page": Paginator(posts, 36).get_page(request.GET.get("page")),
        "accounts": InstagramAccount.objects.filter(
            owner=request.user, is_active=True, encrypted_cookies__isnull=False),
    })


@never_cache
@login_required
def instagram_profile(request, username):
    username = username.strip().lstrip("@").lower()
    posts = _visible_posts(request.user).filter(owner_username__iexact=username).prefetch_related(
        "assets").order_by("-published_at", "-id")
    identity = InstagramIdentity.objects.filter(
        pk__in=_visible_identity_ids(request.user),
        current_username__iexact=username,
    ).first()
    known_target = ObservationJob.objects.filter(
        owner=request.user, target__iexact=username).exists()
    if not posts.exists() and identity is None and not known_target:
        raise Http404
    config, _ = SiteSettings.objects.get_or_create(owner=request.user)
    try:
        page_size = int(request.GET.get("page_size", config.default_page_size))
    except ValueError:
        page_size = config.default_page_size
    page_size = page_size if page_size in (20, 40, 80, 120) else 40
    extracted_followers = AccountRelationship.objects.filter(
        target=identity, pk__in=_visible_relationship_ids(request.user), is_active=True).count() if identity else 0
    extracted_following = AccountRelationship.objects.filter(
        source=identity, pk__in=_visible_relationship_ids(request.user), is_active=True).count() if identity else 0
    return render(request, "radar/instagram_profile.html", {
        "profile": identity, "username": username,
        "page": Paginator(posts, page_size).get_page(request.GET.get("page")),
        "posts_count": posts.count(), "page_size": page_size,
        "extracted_followers": extracted_followers,
        "extracted_following": extracted_following,
    })


@never_cache
@login_required
def search(request):
    query = request.GET.get("q", "").strip()[:100]
    posts = _visible_posts(request.user)
    comments = Comment.objects.filter(post__in=posts).select_related("post")
    if query:
        posts = posts.filter(Q(caption__icontains=query) | Q(owner_username__icontains=query) |
                             Q(shortcode__icontains=query) | Q(hashtags__tag__icontains=query.lstrip("#")) |
                             Q(mentions__username__icontains=query.lstrip("@"))).distinct()
        comments = comments.filter(Q(text__icontains=query) |
                                   Q(author_username__icontains=query.lstrip("@")) |
                                   Q(post__owner_username__icontains=query)).distinct()
    else:
        posts = posts.none()
        comments = comments.none()
    post_results = list(posts.order_by("-published_at", "-id")[:100])
    needle = query.lower().lstrip("#@").strip()
    for post in post_results:
        matches = []
        if query.lower() in (post.caption or "").lower(): matches.append("کپشن")
        if post.hashtags.filter(tag__icontains=needle).exists(): matches.append("هشتگ")
        if post.mentions.filter(username__icontains=needle).exists(): matches.append("منشن")
        if query.lower().lstrip("@") in post.owner_username.lower(): matches.append("نام پیج")
        post.match_places = matches
    return render(request, "radar/search.html", {"query": query,
        "posts": post_results, "comments": comments.order_by("-published_at", "-id")[:100]})




@login_required
@require_POST
def create_transfer_job(request):
    instagram = get_object_or_404(InstagramAccount, owner=request.user,
                                  pk=request.POST.get("instagram_account_id"))
    telegram = get_object_or_404(TelegramAccount, owner=request.user,
                                 pk=request.POST.get("telegram_account_id"),
                                 status=TelegramAccount.Status.ACTIVE)
    destination_ids = [value for value in request.POST.getlist("telegram_destination_ids")
                       if value.isdigit()]
    destinations = list(TelegramDestination.objects.filter(
        pk__in=destination_ids, account=telegram, is_active=True, can_post=True))
    target = request.POST.get("target", "").strip().lstrip("@").lower()
    posts = _visible_posts(request.user).filter(owner_username__iexact=target).order_by(
        "published_at", "id")
    post_ids = list(posts.values_list("pk", flat=True)[:10001])
    if not instagram.encrypted_cookies:
        messages.error(request, "نشست Instagram برای دانلود فایل‌ها موجود نیست.")
    elif not destinations:
        messages.error(request, "حداقل یک مقصد مدیریتی تلگرام انتخاب کنید.")
    elif not post_ids:
        messages.error(request, "برای این پیج محتوای رصدشده‌ای وجود ندارد.")
    elif len(post_ids) > 10000:
        messages.error(request, "هر جاب انتقال حداکثر ۱۰هزار محتوا دارد.")
    else:
        with transaction.atomic():
            job = PublishJob.objects.create(
                owner=request.user, telegram_account=telegram,
                instagram_account=instagram, mode=PublishJob.Mode.TRANSFER,
                target_username=target, total_items=len(post_ids),
                caption_template=request.POST.get("caption_template", "")[:4000],
                force_resend=request.POST.get("force_resend") == "1")
            PublishJobDestination.objects.bulk_create([
                PublishJobDestination(job=job, destination=destination)
                for destination in destinations])
            PublishItem.objects.bulk_create([
                PublishItem(job=job, post_id=post_id, sequence=index)
                for index, post_id in enumerate(post_ids, 1)], batch_size=500)
        try:
            queue_publish(job)
        except Exception:
            messages.warning(request, "جاب ذخیره شد؛ Worker تلگرام آن را بازیابی می‌کند.")
        return redirect("radar:publish_job_detail", job.pk)
    return redirect("radar:observe")


@never_cache
@login_required
def publish_job_detail(request, pk):
    job = get_object_or_404(PublishJob.objects.select_related(
        "telegram_account", "instagram_account", "media_job"), pk=pk, owner=request.user)
    attempts = PublishAttempt.objects.filter(item__job=job).select_related(
        "item__post").order_by("-created_at")[:300]
    return render(request, "radar/publish_job_detail.html", {
        "job": job, "items": job.items.select_related("post").order_by("sequence")[:500],
        "destinations": job.destination_links.select_related("destination"),
        "messages_sent": TelegramMessage.objects.filter(job=job).count(),
        "job_events": [{
            "time": item.created_at, "stage": f"تلگرام / {item.item.sequence}",
            "status": item.status, "records": 1, "duration_ms": item.duration_ms,
            "message": item.message,
        } for item in attempts],
        "log_captures": JobLogCapture.objects.filter(
            owner=request.user, job_kind="transfer", job_id=str(job.pk)).order_by("-started_at")[:8],
    })


@login_required
@require_POST
def pause_publish_job(request, pk):
    job = get_object_or_404(PublishJob, pk=pk, owner=request.user)
    PublishJob.objects.filter(pk=job.pk, status__in=("queued", "running")).update(
        status=PublishJob.Status.PAUSED, lease_until=None)
    return redirect("radar:publish_job_detail", pk)


@login_required
@require_POST
def resume_publish_job(request, pk):
    job = get_object_or_404(PublishJob, pk=pk, owner=request.user)
    if job.status in (PublishJob.Status.PAUSED, PublishJob.Status.PARTIAL,
                      PublishJob.Status.FAILED):
        job.items.filter(status__in=(PublishItem.Status.FAILED,
                                     PublishItem.Status.RUNNING)).update(
            status=PublishItem.Status.QUEUED, attempts=0, error="")
        job.status, job.error, job.lease_until, job.completed_at = "queued", "", None, None
        job.save()
        queue_publish(job)
    return redirect("radar:publish_job_detail", pk)






















@never_cache
@login_required
def graph_builder(request):
    workspace = None
    workspace_id = request.GET.get("workspace", "")
    if workspace_id:
        workspace = get_object_or_404(GraphWorkspace, pk=workspace_id, owner=request.user)
    seeds = InstagramIdentity.objects.filter(pk__in=_visible_identity_ids(request.user)).order_by(
        "current_username").distinct()
    if workspace is None:
        workspace = GraphWorkspace.objects.filter(owner=request.user).select_related("seed").first()
    return render(request, "radar/graph_builder.html", {
        "workspace": workspace, "seeds": seeds,
        "workspaces": GraphWorkspace.objects.filter(owner=request.user).select_related("seed")[:30],
    })


def _graph_options(request, workspace=None):
    def number(name, default):
        try:
            return int(request.GET.get(name, default))
        except (TypeError, ValueError):
            return default

    def flag(name, default):
        raw = request.GET.get(name)
        return default if raw is None else raw.lower() in ("1", "true", "yes", "on")

    return GraphOptions(
        depth=number("depth", workspace.depth if workspace else 1),
        node_limit=number("limit", workspace.node_limit if workspace else 150),
        followers=flag("followers", workspace.include_followers if workspace else True),
        following=flag("following", workspace.include_following if workspace else True),
        mutual=flag("mutual", workspace.include_mutual if workspace else True),
        comments=flag("comments", workspace.include_comments if workspace else True),
    ).normalized()


def _graph_request(request):
    workspace = None
    if request.GET.get("workspace"):
        workspace = get_object_or_404(GraphWorkspace, pk=request.GET["workspace"], owner=request.user)
    seed_id = request.GET.get("seed") or (str(workspace.seed_id) if workspace else "")
    if not seed_id.isdigit():
        from django.http import Http404
        raise Http404
    seed = get_object_or_404(InstagramIdentity, pk=seed_id,
                             pk__in=_visible_identity_ids(request.user))
    return seed, _graph_options(request, workspace)


@login_required
@require_POST
def save_graph_workspace(request):
    seed_id = request.POST.get("seed", "")
    if not seed_id.isdigit():
        messages.error(request, "ابتدا یک مخاطب را به‌عنوان هستهٔ گراف انتخاب کنید.")
        return redirect("radar:graph_builder")
    seed = get_object_or_404(InstagramIdentity, pk=seed_id,
                             pk__in=_visible_identity_ids(request.user))
    try:
        depth = int(request.POST.get("depth", "1"))
        node_limit = int(request.POST.get("limit", "150"))
    except ValueError:
        messages.error(request, "عمق یا سقف Node معتبر نیست.")
        return redirect("radar:graph_builder")
    if not 1 <= depth <= 3 or not 25 <= node_limit <= 2000:
        messages.error(request, "عمق باید ۱ تا ۳ و سقف Node باید ۲۵ تا ۲۰۰۰ باشد.")
        return redirect("radar:graph_builder")
    flags = {name: request.POST.get(name) == "on" for name in
             ("followers", "following", "mutual", "comments")}
    if not any(flags.values()):
        messages.error(request, "حداقل یک نوع رابطه را انتخاب کنید.")
        return redirect("radar:graph_builder")
    name = request.POST.get("name", "").strip()[:120] or f"گراف @{seed.current_username}"
    workspace_id = request.POST.get("workspace_id", "")
    if workspace_id:
        workspace = get_object_or_404(GraphWorkspace, pk=workspace_id, owner=request.user)
        workspace.name, workspace.seed = name, seed
        workspace.depth, workspace.node_limit = depth, node_limit
        workspace.include_followers = flags["followers"]
        workspace.include_following = flags["following"]
        workspace.include_mutual = flags["mutual"]
        workspace.include_comments = flags["comments"]
        workspace.save()
    else:
        workspace = GraphWorkspace.objects.create(
            owner=request.user, name=name, seed=seed, depth=depth, node_limit=node_limit,
            include_followers=flags["followers"], include_following=flags["following"],
            include_mutual=flags["mutual"], include_comments=flags["comments"],
        )
    messages.success(request, "تنظیمات گراف ذخیره شد.")
    return redirect(f"{reverse('radar:graph_builder')}?workspace={workspace.pk}")


@login_required
@require_POST
def delete_graph_workspace(request, pk):
    get_object_or_404(GraphWorkspace, pk=pk, owner=request.user).delete()
    messages.success(request, "گراف ذخیره‌شده حذف شد؛ داده‌های مخاطبان و روابط حذف نشدند.")
    return redirect("radar:graph_builder")


@never_cache
@login_required
def graph_data(request):
    seed, options = _graph_request(request)
    try:
        payload = build_graph(request.user, seed, options)
    except ValueError:
        return JsonResponse({"error": "حداقل یک نوع رابطه را انتخاب کنید."}, status=400)
    return JsonResponse(payload)


def _graph_filename(seed, extension):
    safe = "".join(char for char in seed.current_username if char.isalnum() or char in "._")[:80]
    return f"instradar-{safe or seed.pk}-graph.{extension}"


@login_required
def graph_export(request, file_format):
    if file_format not in ("json", "csv", "graphml"):
        from django.http import Http404
        raise Http404
    seed, options = _graph_request(request)
    try:
        payload = build_graph(request.user, seed, options)
    except ValueError:
        return HttpResponse("حداقل یک نوع رابطه را انتخاب کنید.", status=400,
                            content_type="text/plain; charset=utf-8")
    if file_format == "json":
        content = json.dumps(payload, ensure_ascii=False, indent=2)
        content_type = "application/json; charset=utf-8"
    elif file_format == "csv":
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(("record_type", "id", "source", "target", "username", "relation_type",
                         "weight", "depth", "community", "influence", "path"))
        for node in payload["nodes"]:
            writer.writerow(("node", node["id"], "", "", node["username"], "", "",
                             node["depth"], node["community"], node["influence"],
                             " > ".join(node["path"])))
        for edge in payload["edges"]:
            writer.writerow(("edge", edge["id"], edge["source"], edge["target"], "",
                             edge["type"], edge["weight"], "", "", "", ""))
        content = "\ufeff" + output.getvalue()
        content_type = "text/csv; charset=utf-8"
    else:
        lines = ['<?xml version="1.0" encoding="UTF-8"?>',
                 '<graphml xmlns="http://graphml.graphdrawing.org/xmlns">',
                 '<key id="username" for="node" attr.name="username" attr.type="string"/>',
                 '<key id="depth" for="node" attr.name="depth" attr.type="int"/>',
                 '<key id="community" for="node" attr.name="community" attr.type="int"/>',
                 '<key id="relation" for="edge" attr.name="relation" attr.type="string"/>',
                 '<key id="weight" for="edge" attr.name="weight" attr.type="int"/>',
                 '<graph edgedefault="directed">']
        for node in payload["nodes"]:
            lines.append(f'<node id="n{node["id"]}"><data key="username">{escape(node["username"])}</data>'
                         f'<data key="depth">{node["depth"]}</data><data key="community">'
                         f'{node["community"]}</data></node>')
        for edge in payload["edges"]:
            lines.append(f'<edge id="{escape(edge["id"])}" source="n{edge["source"]}" '
                         f'target="n{edge["target"]}"><data key="relation">{edge["type"]}</data>'
                         f'<data key="weight">{edge["weight"]}</data></edge>')
        lines.extend(("</graph>", "</graphml>"))
        content = "\n".join(lines)
        content_type = "application/graphml+xml; charset=utf-8"
    response = HttpResponse(content, content_type=content_type)
    response["Content-Disposition"] = f'attachment; filename="{_graph_filename(seed, file_format)}"'
    response["Cache-Control"] = "private, no-store"
    return response


@never_cache
@login_required
def comment_list(request):
    posts = _visible_posts(request.user)
    profiles = list(posts.exclude(owner_username="").values_list(
        "owner_username", flat=True).distinct().order_by("owner_username"))
    comments, state = _comment_filters(request, request.user)
    selected_profiles = state["selected_profiles"]
    filter_posts = posts.filter(owner_username__in=selected_profiles) if selected_profiles else posts
    page_size = _page_size(request, request.user)
    context = {
        "profiles": profiles, "page_size": page_size,
        "posts": filter_posts.order_by("-published_at", "-id")[:1000],
        "page": Paginator(comments.prefetch_related("mentions"), page_size).get_page(
            request.GET.get("page")),
    }
    context.update(state)
    return render(request, "radar/comments.html", context)


def _comment_filters(request, user):
    posts = _visible_posts(user)
    comments = Comment.objects.filter(post__in=posts).select_related(
        "post", "parent", "author_identity")
    query = request.GET.get("q", "").strip()[:200]
    if query:
        needle = query.lstrip("@#")
        comments = comments.filter(
            Q(text__icontains=query) |
            Q(author_username__icontains=needle) |
            Q(post__owner_username__icontains=needle) |
            Q(post__caption__icontains=query) |
            Q(post__shortcode__icontains=needle) |
            Q(mentions__username__icontains=needle)
        ).distinct()
    selected_profiles = _clean_usernames(
        request.GET.getlist("profiles") or request.GET.getlist("profile"))
    if selected_profiles:
        comments = comments.filter(post__owner_username__in=selected_profiles)
    selected_posts = [int(value) for value in request.GET.getlist("posts")
                      if value.isdigit()][:1000]
    legacy_post = request.GET.get("post", "")
    if legacy_post.isdigit() and int(legacy_post) not in selected_posts:
        selected_posts.append(int(legacy_post))
    if selected_posts:
        comments = comments.filter(post_id__in=selected_posts)
    comment_type = request.GET.get("type", "all")
    if comment_type == "comments":
        comments = comments.filter(parent__isnull=True)
    elif comment_type == "replies":
        comments = comments.filter(parent__isnull=False)
    else:
        comment_type = "all"
    date_from = _filter_date(request.GET.get("date_from"))
    date_to = _filter_date(request.GET.get("date_to"))
    if date_from:
        comments = comments.filter(published_at__date__gte=date_from)
    if date_to:
        comments = comments.filter(published_at__date__lte=date_to)
    min_likes = _filter_int(request.GET.get("min_likes"))
    max_likes = _filter_int(request.GET.get("max_likes"))
    if min_likes is not None:
        comments = comments.filter(like_count__gte=min_likes)
    if max_likes is not None:
        comments = comments.filter(like_count__lte=max_likes)
    author = request.GET.get("author", "").strip().lstrip("@")[:100]
    if author:
        comments = comments.filter(author_username__icontains=author)
    sort = request.GET.get("sort", "published_at")
    direction = "asc" if request.GET.get("dir") == "asc" else "desc"
    sort_map = {
        "author": "author_username", "text": "text",
        "page": "post__owner_username", "likes": "like_count",
        "published_at": "published_at",
    }
    prefix = "" if direction == "asc" else "-"
    comments = comments.order_by(f"{prefix}{sort_map.get(sort, 'published_at')}", "-id")
    return comments, {
        "query": query, "selected_profiles": selected_profiles,
        "selected_posts": selected_posts, "comment_type": comment_type,
        "author": author, "date_from": request.GET.get("date_from", ""),
        "date_to": request.GET.get("date_to", ""),
        "min_likes": request.GET.get("min_likes", ""),
        "max_likes": request.GET.get("max_likes", ""),
        "sort": sort, "direction": direction,
    }


@login_required
def export_comments_xlsx(request):
    from .xlsx_export import xlsx_response
    comments, _ = _comment_filters(request, request.user)

    def rows():
        for comment in comments.iterator(chunk_size=1000):
            yield (
                f"@{comment.post.owner_username}",
                f"@{comment.author_username}" if comment.author_username else "نامشخص",
                comment.text, comment.like_count, _excel_datetime(comment.published_at),
                "پاسخ" if comment.parent_id else "کامنت",
                comment.post.url,
            )

    return xlsx_response(
        ("پیج", "نویسنده کامنت", "متن کامنت", "میزان پسند",
         "زمان انتشار", "نوع", "لینک پست"),
        rows(), filename="instradar-comments.xlsx", sheet_name="کامنت‌ها",
        widths=(20, 22, 80, 14, 22, 12, 42),
    )










def _owned_job(user, kind, job_id):
    mapping = {"observation": ObservationJob, "profile": CrawlJob, "media": MediaSyncJob, "comment": CommentSyncJob,
               "relationship": RelationshipScanJob,
               "transfer": PublishJob}
    model = mapping.get(kind)
    if model is None:
        return None
    query = model.objects.filter(pk=job_id)
    if not user.is_superuser:
        query = query.filter(owner=user)
    return query.first()


def _job_detail_url(kind, job):
    if kind == "observation":
        return reverse("radar:observation_detail", args=[job.pk])
    if kind == "media":
        return reverse("radar:media_job_detail", args=[job.pk])
    if kind == "relationship":
        return reverse("radar:relationship_job_detail", args=[job.pk])
    if kind == "transfer":
        return reverse("radar:publish_job_detail", args=[job.pk])
    if kind == "comment" and job.observation_id:
        return reverse("radar:observation_detail", args=[job.observation_id])
    return reverse("radar:jobs")


def _observation_events(job, media=None, relation=None, publish=None):
    rows = []
    if media:
        rows.extend({
            "time": item.created_at, "stage": f"محتوا / {item.stream}",
            "status": media.get_status_display(), "records": item.records,
            "duration_ms": item.duration_ms, "message": item.message,
        } for item in media.events.order_by("-created_at")[:120])
    if relation:
        rows.extend({
            "time": item.created_at, "stage": f"روابط / {item.stream}",
            "status": relation.get_status_display(), "records": item.records,
            "duration_ms": item.duration_ms, "message": item.message,
        } for item in relation.events.order_by("-created_at")[:120])
    rows.extend({
        "time": item.created_at,
        "stage": f"کامنت / {item.stage} / {item.job.post.shortcode}",
        "status": item.job.get_status_display(), "records": item.records,
        "duration_ms": item.duration_ms, "message": item.message,
    } for item in CommentSyncEvent.objects.filter(
        job__observation=job).select_related("job__post").order_by("-created_at")[:160])
    if publish:
        rows.extend({
            "time": item.created_at, "stage": f"تلگرام / {item.item.sequence}",
            "status": item.status, "records": 1,
            "duration_ms": item.duration_ms, "message": item.message,
        } for item in PublishAttempt.objects.filter(
            item__job=publish).select_related("item").order_by("-created_at")[:120])
    return sorted(rows, key=lambda item: item["time"], reverse=True)[:300]


def _observation_log_captures(user, job, media=None, relation=None, publish=None):
    criteria = Q(job_kind="observation", job_id=str(job.pk))
    if media:
        criteria |= Q(job_kind="media", job_id=str(media.pk))
    if relation:
        criteria |= Q(job_kind="relationship", job_id=str(relation.pk))
    if publish:
        criteria |= Q(job_kind="transfer", job_id=str(publish.pk))
    comment_ids = [str(value) for value in job.comment_jobs.values_list("pk", flat=True)]
    if comment_ids:
        criteria |= Q(job_kind="comment", job_id__in=comment_ids)
    return JobLogCapture.objects.filter(owner=user).filter(criteria).order_by("-started_at")[:20]


@never_cache
@login_required
@user_passes_test(lambda user: user.is_superuser)
def monitoring(request):
    current, rx_rate, tx_rate, points = metric_payload()
    active_profile = CrawlJob.objects.filter(status__in=("queued", "running")).count()
    active_media = MediaSyncJob.objects.filter(status__in=("queued", "running")).count()
    active_comments = CommentSyncJob.objects.filter(status__in=("queued", "running")).count()
    active_relationships = RelationshipScanJob.objects.filter(status__in=("queued", "running")).count()
    active_transfers = PublishJob.objects.filter(status__in=("waiting", "queued", "running")).count()
    active_observations = ObservationJob.objects.filter(status__in=("queued", "running")).count()
    captures = list(JobLogCapture.objects.order_by("-started_at")[:30])
    for capture in captures:
        job = _owned_job(request.user, capture.job_kind, capture.job_id)
        capture.job_url = _job_detail_url(capture.job_kind, job) if job else reverse("radar:jobs")
    return render(request, "radar/monitoring.html", {
        "metric": current, "rx_rate": rx_rate, "tx_rate": tx_rate,
        "active_profile": active_profile, "active_media": active_media,
        "active_comments": active_comments, "active_relationships": active_relationships,
        "active_transfers": active_transfers,
        "active_observations": active_observations,
        "log_window_default": settings.INSTRADAR_LOG_WINDOW_DEFAULT,
        "captures": captures,
    })


@never_cache
@login_required
@user_passes_test(lambda user: user.is_superuser)
def monitoring_data(request):
    current, rx_rate, tx_rate, points = metric_payload()
    return JsonResponse({
        "captured_at": current.captured_at.isoformat(), "cpu": current.cpu_percent,
        "memory": current.memory_percent, "memory_used": current.memory_used_bytes,
        "memory_total": current.memory_total_bytes, "disk": current.disk_percent,
        "rx_rate": round(rx_rate, 2), "tx_rate": round(tx_rate, 2),
        "series": [{"time": point.captured_at.isoformat(), "cpu": point.cpu_percent,
                    "memory": point.memory_percent} for point in points],
        "active": {
            "observations": ObservationJob.objects.filter(status__in=("queued", "running")).count(),
            "profile": CrawlJob.objects.filter(status__in=("queued", "running")).count(),
            "media": MediaSyncJob.objects.filter(status__in=("queued", "running")).count(),
            "comments": CommentSyncJob.objects.filter(status__in=("queued", "running")).count(),
            "relationships": RelationshipScanJob.objects.filter(status__in=("queued", "running")).count(),
            "transfers": PublishJob.objects.filter(status__in=("waiting", "queued", "running")).count(),
        },
    })


@login_required
@user_passes_test(lambda user: user.is_superuser)
@require_POST
def capture_job_log(request, kind, pk):
    job = _owned_job(request.user, kind, pk)
    if job is None:
        from django.http import Http404
        raise Http404
    try:
        window = int(request.POST.get("window_seconds") or settings.INSTRADAR_LOG_WINDOW_DEFAULT)
    except ValueError:
        window = settings.INSTRADAR_LOG_WINDOW_DEFAULT
    window = min(settings.INSTRADAR_LOG_WINDOW_MAX, max(10, window))
    end = timezone.now()
    capture = JobLogCapture.objects.create(owner=request.user, job_kind=kind, job_id=str(job.pk),
        window_seconds=window, started_at=end - timedelta(seconds=window), ends_at=end)
    try:
        finish_job_log_capture.delay(str(capture.pk))
    except Exception:
        # A stored capture is still recoverable manually; keep the requested time range.
        messages.warning(request, "درخواست لاگ ذخیره شد، اما Worker در دسترس نبود.")
    return redirect(f"{_job_detail_url(kind, job)}#job-activity")


@never_cache
@login_required
def relationship_jobs(request):
    return render(request, "radar/relationship_jobs.html", {
        "accounts": InstagramAccount.objects.filter(owner=request.user).exclude(encrypted_cookies__isnull=True),
        "jobs": RelationshipScanJob.objects.filter(owner=request.user).select_related("account").order_by("-created_at")[:100],
    })


@login_required
@require_POST
def create_relationship_job(request):
    account = get_object_or_404(InstagramAccount, pk=request.POST.get("account_id"), owner=request.user)
    target = request.POST.get("target", "").strip().lstrip("@").lower()
    mode = request.POST.get("mode", RelationshipScanJob.Mode.BOTH)
    try:
        max_pages = int(request.POST.get("max_pages") or 0)
    except ValueError:
        max_pages = -1
    if not target or len(target) > 100 or not all(c.isascii() and (c.isalnum() or c in "._") for c in target):
        messages.error(request, "نام کاربری هدف معتبر نیست.")
    elif mode not in {choice for choice, _ in RelationshipScanJob.Mode.choices}:
        messages.error(request, "نوع رصد روابط معتبر نیست.")
    elif not 0 <= max_pages <= 10000:
        messages.error(request, "سقف صفحه باید صفر یا عددی تا ۱۰۰۰۰ باشد.")
    elif not account.encrypted_cookies:
        messages.error(request, "ابتدا نشست حساب را از noVNC دریافت کنید.")
    elif RelationshipScanJob.objects.filter(account=account, status__in=("queued", "running")).exists():
        messages.error(request, "این حساب یک جاب روابط فعال دارد؛ ابتدا همان جاب را تمام یا متوقف کنید.")
    else:
        seed, target_pk, follower_count, following_count = known_relationship_target(
            request.user.pk, target)
        with transaction.atomic():
            job = RelationshipScanJob.objects.create(
                owner=request.user, account=account, target=target, mode=mode,
                max_pages=max_pages, depth=1, seed_identity=seed, target_pk=target_pk,
                expected_followers=follower_count, expected_following=following_count,
            )
            streams = []
            if mode in (RelationshipScanJob.Mode.BOTH, RelationshipScanJob.Mode.FOLLOWERS):
                streams.append(RelationshipSyncState(
                    job=job, stream=RelationshipSyncState.Stream.FOLLOWERS,
                    api="private_gql"))
            if mode in (RelationshipScanJob.Mode.BOTH, RelationshipScanJob.Mode.FOLLOWING):
                streams.append(RelationshipSyncState(
                    job=job, stream=RelationshipSyncState.Stream.FOLLOWING,
                    api="private_gql"))
            RelationshipSyncState.objects.bulk_create(streams)
        try:
            queue_relationships(job)
        except Exception:
            messages.warning(request, "جاب ذخیره شد و بازیابی دوره‌ای Worker آن را اجرا می‌کند.")
        else:
            messages.success(request, "رصد روابط در صف قرار گرفت.")
        return redirect("radar:relationship_job_detail", job.pk)
    return redirect("radar:relationship_jobs")


@never_cache
@login_required
def relationship_job_detail(request, pk):
    job = get_object_or_404(RelationshipScanJob.objects.select_related("account", "seed_identity"),
                            pk=pk, owner=request.user)
    events = list(job.events.order_by("-created_at")[:200])
    return render(request, "radar/relationship_job_detail.html", {
        "job": job,
        "streams": job.streams.order_by("id"),
        "events": events,
        "job_events": [{"time": item.created_at, "stage": item.stream,
            "status": job.get_status_display(), "records": item.records,
            "duration_ms": item.duration_ms, "message": item.message} for item in events],
        "snapshots": job.snapshots.count(),
        "new_count": job.changes.filter(change_type="followed").count(),
        "ended_count": job.changes.filter(change_type="unfollowed").count(),
        "log_captures": JobLogCapture.objects.filter(
            owner=request.user, job_kind="relationship", job_id=str(job.pk)
        ).order_by("-started_at")[:8],
    })


@login_required
@require_POST
def pause_relationship_job(request, pk):
    job = get_object_or_404(RelationshipScanJob, pk=pk, owner=request.user)
    RelationshipScanJob.objects.filter(pk=job.pk, status__in=("queued", "running")).update(
        status="paused", lease_until=None)
    return redirect("radar:relationship_job_detail", pk)


@login_required
@require_POST
def resume_relationship_job(request, pk):
    with transaction.atomic():
        job = get_object_or_404(RelationshipScanJob.objects.select_for_update(), pk=pk, owner=request.user)
        if job.status not in ("paused", "failed", "partial"):
            return redirect("radar:relationship_job_detail", pk)
        if not job.target_pk or not job.seed_identity_id:
            seed, target_pk, follower_count, following_count = known_relationship_target(
                request.user.pk, job.target)
            if target_pk:
                job.target_pk = target_pk
            if seed:
                job.seed_identity = seed
            if follower_count is not None:
                job.expected_followers = follower_count
            if following_count is not None:
                job.expected_following = following_count
        if job.status == "partial":
            for state in job.streams.all():
                if not state.issue:
                    continue
                capped = "سقف صفحه" in state.issue
                if not capped:
                    if job.seed_identity_id:
                        snapshots = job.snapshots.all()
                        if state.stream == RelationshipSyncState.Stream.FOLLOWERS:
                            snapshots = snapshots.filter(relationship__target=job.seed_identity)
                        else:
                            snapshots = snapshots.filter(relationship__source=job.seed_identity)
                        snapshots.delete()
                    state.cursor, state.pages, state.seen, state.api = "", 0, 0, "private_gql"
                state.issue, state.finished, state.reconciled = "", False, False
                state.save()
            job.scan_started_at = timezone.now()
        job.max_pages = 0
        job.status, job.error, job.attempts, job.lease_until = "queued", "", 0, None
        job.completed_at = None
        job.save()
    try:
        queue_relationships(job)
    except Exception:
        pass
    return redirect("radar:relationship_job_detail", pk)


def _visible_identity_ids(user):
    return RelationshipSnapshot.objects.filter(job__owner=user).values_list("source_id", flat=True).union(
        RelationshipSnapshot.objects.filter(job__owner=user).values_list("target_id", flat=True),
        Comment.objects.filter(post__postsnapshot__job__owner=user,
                               author_identity__isnull=False).values_list("author_identity_id", flat=True),
        Comment.objects.filter(post__postsnapshot__job__owner=user,
                               post__owner_identity__isnull=False).values_list("post__owner_identity_id", flat=True),
        Post.objects.filter(postsnapshot__job__owner=user,
                            owner_identity__isnull=False).values_list("owner_identity_id", flat=True),
        PostLike.objects.filter(post__postsnapshot__job__owner=user).values_list(
            "identity_id", flat=True),
    )


def _visible_relationship_ids(user):
    return RelationshipSnapshot.objects.filter(job__owner=user).values_list(
        "relationship_id", flat=True)


@never_cache
@login_required
def contact_list(request):
    identities, state = _contact_filters(request, request.user)
    page_size = _page_size(request, request.user)
    context = {
        "page_size": page_size,
        "page": Paginator(identities, page_size).get_page(request.GET.get("page")),
    }
    context.update(state)
    return render(request, "radar/contact_list.html", context)


def _contact_filters(request, user):
    identities = InstagramIdentity.objects.filter(pk__in=_visible_identity_ids(user))
    visible_relationships = _visible_relationship_ids(user)
    query = request.GET.get("q", "").strip()[:100]
    if query:
        identities = identities.filter(Q(current_username__icontains=query.lstrip("@")) |
                                       Q(full_name__icontains=query) |
                                       Q(aliases__username__icontains=query.lstrip("@"))).distinct()
    identities = identities.prefetch_related("aliases").annotate(
        following_edges=Count("outgoing_relationships", filter=Q(
            outgoing_relationships__is_active=True,
            outgoing_relationships__pk__in=visible_relationships), distinct=True),
        follower_edges=Count("incoming_relationships", filter=Q(
            incoming_relationships__is_active=True,
            incoming_relationships__pk__in=visible_relationships), distinct=True),
        extracted_posts=Count("posts", filter=Q(
            posts__postsnapshot__job__owner=user), distinct=True),
    )
    min_followers = _filter_int(request.GET.get("min_followers"))
    if min_followers is not None:
        identities = identities.filter(followers_count__gte=min_followers)
    sort = request.GET.get("sort", "username")
    direction = "asc" if request.GET.get("dir") == "asc" else "desc"
    sort_map = {
        "username": "current_username", "name": "full_name",
        "posts": "media_count", "followers": "followers_count",
        "following": "following_count", "extracted": "extracted_posts",
    }
    prefix = "" if direction == "asc" else "-"
    identities = identities.order_by(f"{prefix}{sort_map.get(sort, 'current_username')}", "pk")
    return identities, {"query": query, "min_followers": request.GET.get(
        "min_followers", ""), "sort": sort, "direction": direction}


@login_required
def export_contacts_xlsx(request):
    from .xlsx_export import xlsx_response
    identities, _ = _contact_filters(request, request.user)

    def rows():
        for identity in identities.iterator(chunk_size=1000):
            yield (
                f"@{identity.current_username}", identity.full_name,
                identity.media_count, identity.followers_count,
                identity.following_count, identity.biography,
            )

    return xlsx_response(
        ("نام پیج", "نام کامل", "تعداد پست", "فالور", "فالوینگ", "بایو"),
        rows(), filename="instradar-contacts.xlsx", sheet_name="مخاطبین",
        widths=(22, 28, 14, 16, 16, 80),
    )


@never_cache
@login_required
def relationship_list(request):
    relationships = AccountRelationship.objects.filter(
        pk__in=_visible_relationship_ids(request.user)).select_related("source", "target")
    status = request.GET.get("status", "active")
    if status not in ("active", "ended", "all"):
        status = "active"
    if status == "active":
        relationships = relationships.filter(is_active=True)
    elif status == "ended":
        relationships = relationships.filter(is_active=False)
    query = request.GET.get("q", "").strip()[:100]
    if query:
        relationships = relationships.filter(Q(source__current_username__icontains=query.lstrip("@")) |
                                             Q(target__current_username__icontains=query.lstrip("@")))
    relationships = relationships.annotate(mutual=Exists(
        AccountRelationship.objects.filter(
            source_id=OuterRef("target_id"), target_id=OuterRef("source_id"),
            relation_type=AccountRelationship.Type.FOLLOWS, is_active=True,
            pk__in=_visible_relationship_ids(request.user),
        )))
    return render(request, "radar/relationship_list.html", {
        "query": query, "status": status,
        "page": Paginator(relationships.order_by("-last_seen_at"), 50).get_page(request.GET.get("page")),
    })


def _audience_relation_filters(request, user):
    snapshots = RelationshipSnapshot.objects.filter(
        job__owner=user, job__seed_identity__isnull=False).select_related(
            "job__seed_identity", "relationship", "source", "target")
    latest = RelationshipSnapshot.objects.filter(
        job__owner=user,
        job__seed_identity_id=OuterRef("job__seed_identity_id"),
        relationship_id=OuterRef("relationship_id"),
    ).order_by("-observed_at", "-pk").values("pk")[:1]
    snapshots = snapshots.annotate(
        latest_context_snapshot=Subquery(latest),
        relation_kind=Case(
            When(source_id=F("job__seed_identity_id"), then=Value("following")),
            default=Value("follower"),
        ),
        audience_username=Case(
            When(source_id=F("job__seed_identity_id"),
                 then=F("target__current_username")),
            default=F("source__current_username"),
        ),
    ).filter(pk=F("latest_context_snapshot"))

    selected_sources = [int(value) for value in request.GET.getlist("sources")
                        if value.isdigit()][:100]
    if selected_sources:
        snapshots = snapshots.filter(job__seed_identity_id__in=selected_sources)
    direction = request.GET.get("direction", "all")
    if direction in ("follower", "following"):
        snapshots = snapshots.filter(relation_kind=direction)
    else:
        direction = "all"
    status = request.GET.get("status", "active")
    if status == "active":
        snapshots = snapshots.filter(relationship__is_active=True)
    elif status == "ended":
        snapshots = snapshots.filter(relationship__is_active=False)
    else:
        status = "all"
    query = request.GET.get("q", "").strip().lstrip("@")[:100]
    if query:
        snapshots = snapshots.filter(
            Q(job__seed_identity__current_username__icontains=query) |
            Q(source__current_username__icontains=query) |
            Q(target__current_username__icontains=query))
    sort = request.GET.get("sort", "last_seen")
    direction_sort = "asc" if request.GET.get("dir") == "asc" else "desc"
    sort_map = {
        "source": "job__seed_identity__current_username",
        "audience": "audience_username", "kind": "relation_kind",
        "first_seen": "relationship__first_seen_at",
        "last_seen": "relationship__last_seen_at",
    }
    prefix = "" if direction_sort == "asc" else "-"
    snapshots = snapshots.order_by(f"{prefix}{sort_map.get(sort, 'relationship__last_seen_at')}", "-pk")
    return snapshots, {
        "selected_sources": selected_sources, "relation_direction": direction,
        "status": status, "query": query, "sort": sort,
        "direction": direction_sort,
    }


@never_cache
@login_required
def audience_relations(request):
    snapshots, state = _audience_relation_filters(request, request.user)
    sources = InstagramIdentity.objects.filter(
        seed_jobs__owner=request.user).distinct().order_by("current_username")
    page_size = _page_size(request, request.user)
    context = {
        "sources": sources, "page_size": page_size,
        "page": Paginator(snapshots, page_size).get_page(request.GET.get("page")),
    }
    context.update(state)
    return render(request, "radar/audience_relations.html", context)


@login_required
def export_audience_relations_xlsx(request):
    from .xlsx_export import xlsx_response
    snapshots, _ = _audience_relation_filters(request, request.user)

    def rows():
        for snapshot in snapshots.iterator(chunk_size=1000):
            yield (
                f"@{snapshot.job.seed_identity.current_username}",
                f"@{snapshot.audience_identity.current_username}",
                snapshot.audience_kind_fa,
                "فعال" if snapshot.relationship.is_active else "پایان‌یافته",
                _excel_datetime(snapshot.relationship.first_seen_at),
                _excel_datetime(snapshot.relationship.last_seen_at),
            )

    return xlsx_response(
        ("پیج مبدأ", "مخاطب", "فالور یا فالوینگ", "وضعیت",
         "اولین مشاهده", "آخرین مشاهده"),
        rows(), filename="instradar-audience-relations.xlsx",
        sheet_name="نسبت مخاطبین", widths=(24, 24, 20, 16, 22, 22),
    )


@never_cache
@login_required
def relationship_changes(request):
    changes = RelationshipChange.objects.filter(job__owner=request.user).select_related(
        "relationship__source", "relationship__target", "job")
    kind = request.GET.get("kind", "all")
    if kind in {choice for choice, _ in RelationshipChange.Type.choices}:
        changes = changes.filter(change_type=kind)
    return render(request, "radar/relationship_changes.html", {
        "kind": kind,
        "page": Paginator(changes.order_by("-happened_at"), 50).get_page(request.GET.get("page")),
    })


@never_cache
@login_required
def telegram_accounts(request):
    try:
        config = TelegramSettings.objects.get(owner=request.user)
    except TelegramSettings.DoesNotExist:
        config = None
    return render(request, "radar/telegram_accounts.html", {
        "telegram_settings": config,
        "accounts": TelegramAccount.objects.filter(owner=request.user, is_active=True).prefetch_related(
            "destinations").order_by("-created_at"),
    })


@login_required
@require_POST
def save_telegram_settings(request):
    try:
        api_id = int(request.POST.get("api_id", ""))
        api_hash = request.POST.get("api_hash", "").strip()
        if api_id <= 0 or not (20 <= len(api_hash) <= 100):
            raise ValueError
    except ValueError:
        messages.error(request, "API ID یا API Hash معتبر نیست.")
    else:
        TelegramSettings.objects.update_or_create(
            owner=request.user,
            defaults={"api_id": api_id, "encrypted_api_hash": seal(api_hash)})
        messages.success(request, "تنظیمات برنامهٔ Telegram به‌صورت رمزگذاری‌شده ذخیره شد.")
    return redirect("radar:telegram_accounts")


@login_required
@require_POST
def add_telegram_account(request):
    title = request.POST.get("title", "").strip()[:100]
    phone = request.POST.get("phone", "").strip().replace(" ", "")[:32]
    if not title or not phone.startswith("+") or not phone[1:].isdigit():
        messages.error(request, "عنوان و شماره با قالب بین‌المللی، مانند ‎+98912...، لازم است.")
    elif not TelegramSettings.objects.filter(owner=request.user).exists():
        messages.error(request, "ابتدا API ID و API Hash را ثبت کنید.")
    else:
        account, created = TelegramAccount.objects.get_or_create(
            owner=request.user, phone=phone, defaults={"title": title})
        if not created:
            account.title = title
            account.save(update_fields=["title", "updated_at"])
        messages.success(request, "حساب ثبت شد؛ اکنون درخواست کد ورود را بزنید.")
    return redirect("radar:telegram_accounts")


@login_required
@require_POST
def telegram_request_code(request, pk):
    account = get_object_or_404(TelegramAccount, pk=pk, owner=request.user)
    try:
        request_login_code(account)
    except TelegramServiceError as exc:
        account.status, account.last_error = TelegramAccount.Status.ERROR, str(exc)[:500]
        account.save(update_fields=["status", "last_error", "updated_at"])
        messages.error(request, str(exc))
    else:
        messages.success(request, "کد ورود به برنامه Telegram شما ارسال شد.")
    return redirect("radar:telegram_accounts")


@login_required
@require_POST
def telegram_verify_code(request, pk):
    account = get_object_or_404(TelegramAccount, pk=pk, owner=request.user)
    code = request.POST.get("code", "").strip().replace(" ", "")
    if not code:
        messages.error(request, "کد ورود را وارد کنید.")
    else:
        try:
            complete = verify_login(account, code=code)
        except TelegramServiceError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, "ورود کامل شد." if complete else
                             "کد پذیرفته شد؛ رمز دومرحله‌ای Telegram را وارد کنید.")
    return redirect("radar:telegram_accounts")


@login_required
@require_POST
def telegram_verify_password(request, pk):
    account = get_object_or_404(TelegramAccount, pk=pk, owner=request.user)
    password = request.POST.get("password", "")
    try:
        verify_login(account, password=password)
    except TelegramServiceError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "حساب Telegram با تأیید دومرحله‌ای متصل شد.")
    return redirect("radar:telegram_accounts")


@login_required
@require_POST
def telegram_sync_destinations(request, pk):
    account = get_object_or_404(TelegramAccount, pk=pk, owner=request.user)
    try:
        count = sync_destinations(account)
    except TelegramServiceError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, f"{count} کانال یا گروه مدیریتی قابل ارسال همگام شد.")
    return redirect("radar:telegram_accounts")


@never_cache
@login_required
def system_settings(request):
    config, _ = SiteSettings.objects.get_or_create(owner=request.user)
    telegram = TelegramSettings.objects.filter(owner=request.user).first()
    return render(request, "radar/settings.html", {
        "config": config, "telegram_settings": telegram,
    })


@login_required
@require_POST
def save_system_settings(request):
    try:
        concurrency = int(request.POST.get("max_concurrent_observations", "2"))
        hour = int(request.POST.get("enrichment_hour", "3"))
        minute = int(request.POST.get("enrichment_minute", "0"))
        page_size = int(request.POST.get("default_page_size", "40"))
        if (not 1 <= concurrency <= 10 or not 0 <= hour <= 23 or
                not 0 <= minute <= 59 or page_size not in (20, 40, 80, 120)):
            raise ValueError
    except ValueError:
        messages.error(request, "مقادیر هم‌زمانی یا زمان اجرای روزانه معتبر نیست.")
        return redirect("radar:system_settings")
    SiteSettings.objects.update_or_create(owner=request.user, defaults={
        "max_concurrent_observations": concurrency,
        "default_page_size": page_size,
        "enrichment_hour": hour, "enrichment_minute": minute,
        "enrichment_enabled": request.POST.get("enrichment_enabled") == "on",
    })
    api_id_raw = request.POST.get("api_id", "").strip()
    api_hash = request.POST.get("api_hash", "").strip()
    if api_id_raw:
        try:
            api_id = int(api_id_raw)
            if api_id <= 0:
                raise ValueError
        except ValueError:
            messages.error(request, "API ID تلگرام معتبر نیست.")
            return redirect("radar:system_settings")
        current = TelegramSettings.objects.filter(owner=request.user).first()
        encrypted_hash = seal(api_hash) if api_hash else (
            current.encrypted_api_hash if current else None)
        if encrypted_hash is None:
            messages.error(request, "برای ثبت اولیه، API Hash تلگرام لازم است.")
            return redirect("radar:system_settings")
        TelegramSettings.objects.update_or_create(owner=request.user, defaults={
            "api_id": api_id, "encrypted_api_hash": encrypted_hash})
    messages.success(request, "تنظیمات سامانه و زمان‌بندی روزانه ذخیره شد.")
    return redirect("radar:system_settings")


@login_required
@require_POST
def delete_instagram_account(request, pk):
    account = get_object_or_404(InstagramAccount, pk=pk, owner=request.user)
    if InstagramAccountLease.objects.filter(
            account=account, expires_at__gt=timezone.now()).exists():
        messages.error(request, "حساب Lease فعال دارد؛ پس از پایان صفحه دوباره تلاش کنید.")
        return redirect("radar:accounts")
    if ObservationJob.objects.filter(
            Q(account=account) | Q(account_pool__members__account=account),
            status__in=("queued", "running")).exists():
        messages.error(request, "این حساب رصد فعال دارد؛ ابتدا جاب را متوقف کنید.")
        return redirect("radar:accounts")
    try:
        browser.call("close", account.id)
    except Exception:
        pass
    account.is_active = False
    account.encrypted_cookies = None
    account.encrypted_aiograpi_settings = None
    account.status = "deleted"
    account.last_error = "حساب و نشست آن توسط مدیر حذف شد"
    account.save(update_fields=["is_active", "encrypted_cookies",
                                "encrypted_aiograpi_settings", "status", "last_error"])
    messages.success(request, "حساب Instagram و نشست رمزگذاری‌شدهٔ آن حذف شد؛ سوابق رصد حفظ شدند.")
    return redirect("radar:accounts")


@login_required
@require_POST
def delete_telegram_account(request, pk):
    account = get_object_or_404(TelegramAccount, pk=pk, owner=request.user)
    if PublishJob.objects.filter(telegram_account=account, status__in=("waiting", "queued", "running")).exists():
        messages.error(request, "این حساب جاب انتقال فعال دارد؛ ابتدا جاب را متوقف کنید.")
        return redirect("radar:telegram_accounts")
    account.is_active = False
    account.encrypted_session = None
    account.encrypted_pending_session = None
    account.encrypted_phone_code_hash = None
    account.status = TelegramAccount.Status.ERROR
    account.last_error = "حساب و نشست آن توسط مدیر حذف شد"
    account.destinations.update(is_active=False, can_post=False)
    account.save(update_fields=["is_active", "encrypted_session", "encrypted_pending_session",
                                "encrypted_phone_code_hash", "status", "last_error", "updated_at"])
    messages.success(request, "حساب Telegram و StringSession رمزگذاری‌شدهٔ آن حذف شد.")
    return redirect("radar:telegram_accounts")


# INSTRADAR-NOVNC-20260914
@never_cache
def novnc_authorize(request):
    """Only authenticated superusers may reach the shared VNC display."""
    from django.http import HttpResponse

    if request.user.is_authenticated and request.user.is_active and request.user.is_superuser:
        return HttpResponse(status=204)
    return HttpResponse(status=403)
