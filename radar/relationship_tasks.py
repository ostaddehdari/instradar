"""Stage 4 resumable followers/following collection with safe reconciliation."""
from __future__ import annotations

import asyncio
import random
import time
from types import SimpleNamespace
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
from . import browser
from .models import (AccountRelationship, FollowerSnapshot, InstagramAccount,
                     RelationshipScanEvent, RelationshipScanJob,
                     RelationshipSnapshot, RelationshipSyncState)
from .relationship_store import (reconcile_relationship_stream, save_relationship_page,
                                 upsert_identity)
from .relationship_target import known_relationship_target
from .session import InstagramSessionError, aiograpi_client
from .crawl_state import ensure_session, checkpoint, save_profile_discovery, save_profile_to_session, ensure_progress


# Instagram refreshed these registered mobile GraphQL queries on 2026-09-18.
# Keep the values at the application boundary as well as pinning aiograpi so an
# already-created Client (or an old cached worker import) cannot silently use
# the retired defaults from aiograpi 1.12.15.
FOLLOW_LIST_DOC_IDS = {
    RelationshipSyncState.Stream.FOLLOWERS: "284797047911918316998205836755",
    RelationshipSyncState.Stream.FOLLOWING: "16104639286363954576550227636",
}


class RelationshipFetchError(RuntimeError):
    """Safe upstream error: contains route/type/status, never response bodies or cookies."""
    def __init__(self, phase, attempts, *, permanent=False):
        self.phase = phase
        self.attempts = attempts
        self.permanent = permanent
        super().__init__(f"{phase} ناموفق بود؛ " + "، ".join(attempts))


def _safe_failure(route, exc):
    if isinstance(exc, browser.BrowserCallError):
        return f"{route}:{str(exc)[:80]}"
    response = getattr(exc, "response", None)
    status = getattr(exc, "code", None) or getattr(response, "status_code", None)
    suffix = f"/HTTP {status}" if status else ""
    return f"{route}:{type(exc).__name__}{suffix}"


def _is_permanent(exc):
    name = type(exc).__name__.lower()
    return any(word in name for word in ("challenge", "loginrequired", "feedbackrequired")) or (
        isinstance(exc, (ValueError, AssertionError, RuntimeError)) and
        str(exc) in ("SessionRejected", "LoggedIntoDifferentAccount"))


def queue_relationships(job, countdown=0, stream=None):
    relationship_page.apply_async(
        args=[str(job.pk), stream or ""], countdown=countdown, priority=6,
        soft_time_limit=settings.INSTRADAR_RELATIONSHIP_TIMEOUT,
        time_limit=settings.INSTRADAR_RELATIONSHIP_TIMEOUT + 30,
    )


async def _connect(account, target, target_pk, has_seed=False, account_id=0):
    try:
        client = await aiograpi_client(account)
    except Exception as exc:
        if instagram_safety_signal(exc) in ("feedback", "rate"):
            raise
        if not account_id:
            raise
        try:
            data = await asyncio.to_thread(
                browser.call, "profile", account_id, target=target, target_pk=str(target_pk or ""))
            profile = SimpleNamespace(**data["profile"])
            return None, profile, f"Chrome Browser fallback؛ aiograpi={_safe_failure('session', exc)}"
        except Exception as browser_exc:
            raise InstagramSessionError(
                f"Instagram session and browser fallback failed ({type(browser_exc).__name__})",
                permanent=getattr(exc, "permanent", False),
            ) from exc
    attempts = []
    private_ready = bool(getattr(client, "_instradar_private_ready", True))
    session_error = str(getattr(client, "_instradar_session_error", "") or "")

    def route_result(route):
        if session_error:
            return f"{route}؛ Mobile session={session_error}; Web session فعال"
        return route

    if target_pk:
        routes = []
        if private_ready:
            routes.append(("Mobile-ID", lambda: client.user_info_v1(target_pk)))
        if hasattr(client, "user_info_v2_gql"):
            routes.append(("Web-ID", lambda: client.user_info_v2_gql(target_pk)))
        if private_ready:
            routes.append(("Auto-ID", lambda: client.user_info(target_pk)))
    else:
        routes = []
        if private_ready:
            routes.append(("Mobile-Username", lambda: client.user_info_by_username_v1(target)))
        if hasattr(client, "user_info_by_username_v2_gql"):
            routes.append(("Web-v2-Username", lambda: client.user_info_by_username_v2_gql(target)))
        routes.append(("Web-Username", lambda: client.user_info_by_username_gql(target)))
    for route, request in routes:
        try:
            profile = await asyncio.wait_for(
                request(), settings.INSTRADAR_RELATIONSHIP_REQUEST_TIMEOUT)
            if target_pk and (getattr(profile, "username", "") or "").casefold() != target.casefold():
                attempts.append(f"{route}:UsernameMismatch")
                break
            return client, profile, route_result(route)
        except Exception as exc:
            attempts.append(_safe_failure(route, exc))
            signal = instagram_safety_signal(exc)
            if signal:
                raise RelationshipFetchError(
                    "شناسایی پیج هدف", attempts, permanent=signal == "login") from exc
    # A stored Instagram ID can become stale after an earlier partial crawl.
    # Resolve the requested username again before trusting the old seed.
    if target_pk and any("UsernameMismatch" in item for item in attempts):
        username_routes = []
        if private_ready:
            username_routes.append(
                ("Mobile-Username", lambda: client.user_info_by_username_v1(target)))
        if hasattr(client, "user_info_by_username_v2_gql"):
            username_routes.append(
                ("Web-v2-Username", lambda: client.user_info_by_username_v2_gql(target)))
        username_routes.append(
            ("Web-Username", lambda: client.user_info_by_username_gql(target)))
        for route, request in username_routes:
            try:
                profile = await asyncio.wait_for(
                    request(), settings.INSTRADAR_RELATIONSHIP_REQUEST_TIMEOUT)
                return client, profile, route_result(
                    f"{route} (اصلاح Instagram ID قدیمی)")
            except Exception as exc:
                attempts.append(_safe_failure(route, exc))
                signal = instagram_safety_signal(exc)
                if signal:
                    raise RelationshipFetchError(
                        "شناسایی پیج هدف", attempts,
                        permanent=signal == "login") from exc

    # The browser already owns the canonical authenticated session.  Use its
    # profile response when all direct profile routes fail, while retaining a
    # web-capable aiograpi client for the relationship endpoint fallbacks.
    if account_id:
        try:
            data = await asyncio.to_thread(
                browser.call, "profile", account_id,
                target=target, target_pk=str(target_pk or ""))
            profile = SimpleNamespace(**data["profile"])
            detail = session_error or ("، ".join(attempts[-2:]) if attempts else "direct unavailable")
            return client, profile, f"Chrome Browser fallback؛ direct={detail}"
        except Exception as exc:
            attempts.append(_safe_failure("Chrome profile", exc))
    if target_pk:
        # The stable ID/seed remain usable for crawling, but the UI must state
        # that counts/privacy could not be refreshed.
        return client, None, route_result(
            "شناسهٔ ذخیره‌شده؛ Refresh پروفایل ناموفق: " + "، ".join(attempts))
    raise RelationshipFetchError(
        "شناسایی پیج هدف", attempts,
        permanent=bool(attempts) and all("LoginRequired" in item or "Challenge" in item
                                         for item in attempts))


async def _fetch_page(client, state, target_pk, expected, target_username="", account_id=0):
    await asyncio.sleep(random.randint(settings.INSTRADAR_MIN_DELAY_MS,
                                       settings.INSTRADAR_MAX_DELAY_MS) / 1000)
    size = settings.INSTRADAR_RELATIONSHIP_PAGE_SIZE

    async def private():
        if state.stream == RelationshipSyncState.Stream.FOLLOWERS:
            return await client.user_followers_v1_chunk(target_pk, max_amount=size,
                                                        max_id=active_cursor or "")
        return await client.user_following_v1_chunk(target_pk, max_amount=size,
                                                    max_id=active_cursor or "")

    async def graphql():
        if state.stream == RelationshipSyncState.Stream.FOLLOWERS:
            return await client.user_followers_gql_chunk(target_pk, max_amount=size,
                                                         end_cursor=active_cursor or None)
        return await client.user_following_gql_chunk(target_pk, max_amount=size,
                                                     end_cursor=active_cursor or None)

    async def private_graphql():
        # Call the registered-query wrapper directly so the current doc_id is
        # explicit.  This is the route fixed by aiograpi 2.0.5; keeping the id
        # here also makes the hotfix effective before a long-lived worker is
        # restarted during a rolling deployment.
        raw_method_name = (
            "private_graphql_followers_list"
            if state.stream == RelationshipSyncState.Stream.FOLLOWERS
            else "private_graphql_following_list"
        )
        root_name = (
            "xdt_api__v1__friendships__followers"
            if state.stream == RelationshipSyncState.Stream.FOLLOWERS
            else "xdt_api__v1__friendships__following"
        )
        raw_method = getattr(client, raw_method_name, None)
        root_parser = getattr(client, "_private_graphql_root", None)
        if raw_method and root_parser:
            from aiograpi.extractors import extract_user_short

            raw = await raw_method(
                target_pk,
                client.rank_token,
                client_doc_id=FOLLOW_LIST_DOC_IDS[state.stream],
                max_id=active_cursor or None,
            )
            root = root_parser(raw, root_name)
            if not root:
                raise RuntimeError("PrivateGraphqlPayloadMissing")
            return ([extract_user_short(item) for item in (root.get("users") or [])][:size],
                    root.get("next_max_id") or "")

        # Compatibility with clients that expose only the parsed chunk helper.
        chunk_method_name = (
            "user_followers_private_gql_chunk"
            if state.stream == RelationshipSyncState.Stream.FOLLOWERS
            else "user_following_private_gql_chunk"
        )
        chunk_method = getattr(client, chunk_method_name, None)
        if chunk_method:
            return await chunk_method(target_pk, max_amount=size, max_id=active_cursor)
        raise RuntimeError("PrivateGraphqlUnavailable")

    async def automatic():
        if state.stream == RelationshipSyncState.Stream.FOLLOWERS:
            result = await client.user_followers(target_pk, amount=size, use_cache=False)
        else:
            result = await client.user_following(target_pk, amount=size, use_cache=False)
        return list(result.values()) if isinstance(result, dict) else list(result), ""

    async def web_v1():
        endpoint = "followers" if state.stream == RelationshipSyncState.Stream.FOLLOWERS else "following"
        params = {"count": size, "search_surface": "follow_list_page", "query": ""}
        if active_cursor:
            params["max_id"] = active_cursor
        data = await client.public_request(
            f"https://www.instagram.com/api/v1/friendships/{target_pk}/{endpoint}/",
            params=params,
            headers={
                "X-IG-App-ID": "936619743392459",
                "X-ASBD-ID": "129477",
                "X-Requested-With": "XMLHttpRequest",
                "Referer": f"https://www.instagram.com/{target_username or target_pk}/",
            },
            return_json=True,
        )
        if not isinstance(data, dict):
            raise RuntimeError("WebFriendshipPayloadInvalid")
        from aiograpi.extractors import extract_user_short
        return ([extract_user_short(item) for item in (data.get("users") or [])][:size],
                data.get("next_max_id") or "")

    async def browser_network():
        if not account_id:
            raise RuntimeError("BrowserAccountMissing")
        data = await asyncio.to_thread(
            browser.call, "relationships", account_id,
            target=target_username,
            target_pk=str(target_pk),
            stream=state.stream,
            cursor=active_cursor,
            limit=min(size, 100),
            expected_total=expected,
        )
        raw_users = list(data.get("users") or [])
        remaining = None if expected is None else max(0, expected - state.seen)
        if remaining is not None:
            raw_users = raw_users[:remaining]
        users = [SimpleNamespace(**item) for item in raw_users]
        if not users:
            raise RuntimeError("BrowserRelationshipResponseEmpty")
        return users, str(data.get("cursor") or "")

    route_functions = {
        "private": private, "private_gql": private_graphql,
        "web_v1": web_v1, "gql": graphql, "auto": automatic,
        "browser": browser_network,
    }
    route_labels = {
        "private": "Mobile Private", "private_gql": "Mobile GraphQL",
        "web_v1": "Web Friendship API", "gql": "Web GraphQL",
        "auto": "aiograpi Auto", "browser": "شبکهٔ زندهٔ Chrome",
    }
    # A route may start returning an empty/blocked page after a successful
    # cursor. Keep the current API first, but retain real fallbacks on every
    # page. A fallback restarts from its own first page and DB upserts dedupe it.
    if client is None:
        route_order = ["browser"]
    elif getattr(client, "_instradar_private_ready", True):
        route_order = [state.api, "private", "private_gql", "web_v1", "gql", "browser"]
    else:
        # The browser cookie is valid but Mobile session import failed.  Do not
        # waste the whole request timeout on routes that require Mobile auth.
        preferred = state.api if state.api in ("web_v1", "gql") else "web_v1"
        route_order = [preferred, "web_v1", "gql", "browser"]
    route_order = list(dict.fromkeys(api for api in route_order if api in route_functions))
    failures, empty_results = [], []
    permanent = False
    for api in route_order:
        # Cursors are provider-specific.  A fallback must begin at its first
        # page; PostgreSQL upserts safely deduplicate overlap.  Once selected,
        # that provider receives its own cursor on the following task page.
        active_cursor = state.cursor if api == state.api else ""
        try:
            users, cursor = await asyncio.wait_for(
                route_functions[api](), settings.INSTRADAR_RELATIONSHIP_REQUEST_TIMEOUT)
            users = list(users or [])
            complete_by_count = (expected is not None and expected > 0
                                 and state.seen >= expected)
            if users or expected == 0 or complete_by_count:
                notice = ""
                if api != state.api:
                    notice = (f"مسیر {route_labels.get(state.api, state.api)} پاسخ نداد؛ "
                              f"{route_labels[api]} فعال شد")
                return users, str(cursor or ""), api, notice
            empty_results.append((users, cursor, api))
        except Exception as exc:
            failures.append(_safe_failure(route_labels[api], exc))
            signal = instagram_safety_signal(exc)
            if signal:
                raise RelationshipFetchError(
                    f"دریافت {state.get_stream_display()}", failures,
                    permanent=signal == "login") from exc
            permanent = permanent or _is_permanent(exc)
    if empty_results:
        users, cursor, api = empty_results[-1]
        notice = "همه APIهای مستقیم پاسخ خالی دادند"
        browser_failures = [item for item in failures if item.startswith("شبکهٔ زندهٔ Chrome:")]
        if browser_failures:
            notice += f"؛ fallback مرورگر ناموفق بود ({browser_failures[-1]})"
        return users, str(cursor or ""), api, notice
    raise RelationshipFetchError(
        f"دریافت {state.get_stream_display()}", failures, permanent=permanent)


def _observed(job, stream):
    query = RelationshipSnapshot.objects.filter(job=job)
    if stream == RelationshipSyncState.Stream.FOLLOWERS:
        query = query.filter(relationship__target=job.seed_identity)
    else:
        query = query.filter(relationship__source=job.seed_identity)
    return query.count()


@transaction.atomic
def _reset_overflow_stream(job, state, expected):
    """Discard a demonstrably contaminated browser snapshot before Resume.

    Older browser fallback builds could append Suggested accounts after the
    real list.  `seen > expected` is objective proof that this happened.  Mark
    the affected edges inactive, remove only this job/session's snapshots and
    restart the stream; valid rows are reactivated as the clean scan proceeds.
    """
    if expected is None or state.seen <= expected:
        return False
    snapshots = RelationshipSnapshot.objects.filter(job=job)
    if state.stream == state.Stream.FOLLOWERS:
        snapshots = snapshots.filter(relationship__target=job.seed_identity)
    else:
        snapshots = snapshots.filter(relationship__source=job.seed_identity)
    relationship_ids = list(snapshots.values_list("relationship_id", flat=True))
    now = timezone.now()
    AccountRelationship.objects.filter(
        pk__in=relationship_ids, last_seen_job=job,
    ).update(is_active=False, ended_at=now)
    snapshots.delete()
    if job.observation_id:
        session = ensure_session(job.observation)
        FollowerSnapshot.objects.filter(session=session, stream=state.stream).delete()
        checkpoint(session, state.stream, cursor="", last_item_id="", pages=0,
                   extracted=0, expected=expected, finished=False,
                   issue="", status="running")
    previous = state.seen
    state.cursor = ""
    state.api = "private_gql"
    state.pages = 0
    state.seen = 0
    state.finished = False
    state.reconciled = False
    state.issue = ""
    state.save()
    RelationshipScanEvent.objects.create(
        job=job, stream=state.stream, records=0,
        message=(f"بازنشانی امن Snapshot آلوده: {previous} رکورد برای شمارندهٔ "
                 f"{expected}؛ استخراج تمیز از ابتدا آغاز شد"),
    )
    return True


@transaction.atomic
def _commit_page(job, state, users, next_cursor, api, notice, elapsed_ms):
    previous_cursor = state.cursor
    expected = job.expected_followers if state.stream == state.Stream.FOLLOWERS else job.expected_following
    observed_before_page = _observed(job, state.stream)
    written, renamed = save_relationship_page(job, state.stream, users)
    state.cursor = next_cursor
    state.api = api
    state.pages += 1
    state.seen = _observed(job, state.stream)
    new_relationships = max(0, state.seen - observed_before_page)
    if next_cursor and next_cursor == previous_cursor:
        state.issue = "Cursor تکراری؛ پیمایش برای جلوگیری از حلقه متوقف شد"
        state.finished = True
    elif (api == "browser" and users and not new_relationships
          and expected not in (None, 0) and state.seen < expected):
        # The web dialog can keep returning the same virtualized rows while an
        # artificial offset advances. Stop the partial stream so the next
        # stream can run and a later Resume can retry fresh provider routes.
        state.cursor = ""
        next_cursor = ""
        state.issue = (
            f"مرورگر همان {state.seen} رابطهٔ یکتا را دوباره برگرداند؛ "
            f"اینستاگرام فهرست کامل {expected} موردی را به حساب رصدکننده نمایش نداد"
        )
        state.finished = True
    elif job.max_pages and state.pages >= job.max_pages and next_cursor:
        state.issue = "سقف صفحه رسید؛ Resume برای ادامه"
        state.finished = True
    elif not users and expected not in (None, 0):
        state.issue = state.issue or "پاسخ API خالی بود؛ رصد رابطه‌ها ناقص است و نیاز به Resume / fallback دارد"
        state.finished = False
    elif not users and expected == 0:
        state.issue = ""
        state.finished = True
    else:
        state.issue = state.issue or ""
        observed_after_page = state.seen
        state.finished = (
            expected == 0 or
            (expected is not None and observed_after_page >= expected) or
            (expected is None and not next_cursor and bool(users))
        )
        if expected not in (None, 0) and observed_after_page < expected and not next_cursor:
            state.issue = (
                f"فقط {observed_after_page} رابطه از {expected} مورد دریافت شد؛ "
                "pagination مرورگر متوقف شد و Resume لازم است"
            )
            state.finished = False
    state.save()
    if job.observation_id:
        session = ensure_session(job.observation)
        checkpoint(session, state.stream, cursor=next_cursor, last_item_id=(users[-1].pk if users else ""),
                   pages=state.pages, extracted=state.seen, expected=expected,
                   finished=state.finished, issue=state.issue, status="partial" if state.issue else "running")
    provider = {"private": "aiograpi", "private_gql": "graphql", "gql": "graphql",
                "web_v1": "graphql", "browser": "browser", "auto": "aiograpi"}.get(api, api)
    message = (f"صفحه {state.pages}: {len(users)} مورد دریافت، {new_relationships} رابطهٔ جدید "
               f"و {written} رابطه پردازش شد؛ "
               f"provider={provider}; API={api}")
    if renamed:
        message += f"؛ {renamed} تغییر Username"
    if notice:
        message += f"؛ {notice}"
    if not users and expected not in (None, 0):
        message += "؛ پاسخ خالی بدون داده واقعی برای شمارندهٔ غیرصفر"
    RelationshipScanEvent.objects.create(
        job=job, stream=state.stream, records=new_relationships,
        duration_ms=elapsed_ms, message=message,
    )


def _pause_empty_relationship(job, state):
    """Stop a no-cursor empty page from endlessly requeueing the same providers."""
    state.issue = state.issue or "همه Providerها صفحهٔ خالی بدون Cursor برگرداندند؛ Resume لازم است"
    state.finished = False
    state.save(update_fields=["issue", "finished"])
    job.status = RelationshipScanJob.Status.PARTIAL
    job.error = state.issue[:500]
    job.lease_until = None
    job.completed_at = timezone.now()
    job.save(update_fields=["status", "error", "lease_until", "completed_at", "updated_at"])
    RelationshipScanEvent.objects.create(
        job=job, stream=state.stream, records=0,
        message=f"Provider={state.provider}; استخراج متوقف شد تا دادهٔ خالی بدون Cursor دوباره تکرار نشود",
    )


@transaction.atomic
def _finish(job):
    states = list(RelationshipSyncState.objects.select_for_update().filter(job=job))
    if not states or not all(state.finished for state in states):
        return False
    issues = []
    ended = 0
    for state in states:
        expected = job.expected_followers if state.stream == state.Stream.FOLLOWERS else job.expected_following
        observed = _observed(job, state.stream)
        if expected is None:
            state.issue = state.issue or "شمارندهٔ پروفایل برای تطبیق موجود نیست"
            state.save(update_fields=["issue"])
        elif observed < expected:
            state.issue = state.issue or f"فقط {observed} رابطه از {expected} مورد قابل انتظار دیده شد"
            state.save(update_fields=["issue"])
        if not state.issue:
            ended += reconcile_relationship_stream(job, state)
        if state.issue:
            issues.append(f"{state.get_stream_display()}: {state.issue}")
    job.status = RelationshipScanJob.Status.PARTIAL if issues else RelationshipScanJob.Status.DONE
    job.error = "; ".join(issues)[:500]
    job.lease_until = None
    job.completed_at = timezone.now()
    job.save(update_fields=["status", "error", "lease_until", "completed_at", "updated_at"])
    RelationshipScanEvent.objects.create(
        job=job, stream="reconcile", records=ended,
        message=f"پایان امن Snapshot؛ {ended} رابطه پایان‌یافته؛ وضعیت {job.get_status_display()}",
    )
    if job.observation_id:
        from .observation_tasks import queue_observation
        transaction.on_commit(lambda: queue_observation(job.observation))
    return True


@shared_task(name="radar.relationship_tasks.relationship_page")
def relationship_page(job_id, stream_name=""):
    operation_lease = None
    with transaction.atomic():
        job = RelationshipScanJob.objects.select_for_update().select_related(
            "account", "observation", "observation__account_pool").get(pk=job_id)
        if (job.observation_id and job.observation.status not in (
                job.observation.Status.QUEUED, job.observation.Status.RUNNING)):
            return
        if job.status != job.Status.QUEUED or (job.lease_until and job.lease_until > timezone.now()):
            return
        operation_lease, unavailable = acquire_operation_account(
            job.account_id, job.observation_id, "relationships",
            settings.INSTRADAR_RELATIONSHIP_TIMEOUT + 60, cost=6)
        if unavailable:
            job.lease_until = timezone.now() + timedelta(
                seconds=unavailable.wait_seconds)
            job.save(update_fields=["lease_until", "updated_at"])
            RelationshipScanEvent.objects.create(
                job=job, stream="scheduler", message=unavailable.reason[:300])
            if job.observation_id:
                from .observation_tasks import queue_observation
                transaction.on_commit(lambda: queue_observation(
                    job.observation, countdown=unavailable.wait_seconds))
            else:
                transaction.on_commit(lambda: queue_relationships(
                    job, countdown=unavailable.wait_seconds))
            return
        if operation_lease.account_id != job.account_id:
            # Rotate pooled relationship pages to the next healthy browser
            # session while retaining the saved stream cursor.
            job.account_id = operation_lease.account_id
            job.account = InstagramAccount.objects.get(pk=operation_lease.account_id)
            job.save(update_fields=["account", "updated_at"])
        job.status = job.Status.RUNNING
        job.attempts += 1
        job.lease_until = timezone.now() + timedelta(seconds=settings.INSTRADAR_RELATIONSHIP_TIMEOUT + 30)
        job.save(update_fields=["status", "attempts", "lease_until", "updated_at"])
    phase = "اتصال نشست aiograpi"
    started_total = time.monotonic()
    try:
        if not job.target_pk or not job.seed_identity_id:
            seed, target_pk, follower_count, following_count = known_relationship_target(
                job.owner_id, job.target)
            if target_pk:
                job.target_pk = target_pk
            if seed:
                job.seed_identity = seed
            if follower_count is not None:
                job.expected_followers = follower_count
            if following_count is not None:
                job.expected_following = following_count
            job.save(update_fields=["target_pk", "seed_identity", "expected_followers",
                                    "expected_following", "updated_at"])
        with asyncio.Runner() as runner:
            client, profile, profile_route = runner.run(
                _connect(job.account, job.target, job.target_pk,
                         bool(job.seed_identity_id), job.account_id))
            if client is not None:
                InstagramAccount.objects.filter(pk=job.account_id).update(
                    encrypted_aiograpi_settings=seal(client.get_settings()), status="active",
                    last_error="", verified_at=timezone.now(),
                )
            if profile:
                phase = "ذخیره پروفایل هدف"
                if job.observation_id:
                    session = ensure_session(job.observation)
                    save_profile_to_session(session, profile)
                    save_profile_discovery(job.owner_id, profile)
                    ensure_progress(session, "profile", expected=1)
                    checkpoint(session, "profile", extracted=1, expected=1, pages=1, finished=True, status="done")
                seed, _, _ = upsert_identity(profile)
                job.target_pk = str(profile.pk)
                job.seed_identity = seed
                job.expected_followers = getattr(profile, "follower_count", None)
                job.expected_following = getattr(profile, "following_count", None)
                job.save(update_fields=["target_pk", "seed_identity", "expected_followers",
                                        "expected_following", "updated_at"])
            elif not job.seed_identity_id:
                raise RelationshipFetchError(
                    "شناسایی پیج هدف", ["شناسهٔ ذخیره‌شده بدون Identity معتبر"])
            RelationshipScanEvent.objects.create(
                job=job, stream="profile", records=1 if profile else 0,
                duration_ms=int((time.monotonic() - started_total) * 1000),
                message=f"پیج هدف با مسیر {profile_route} آماده شد؛ Instagram ID={job.target_pk}",
            )
            if (client is not None and getattr(client, "_instradar_private_ready", True)
                    and profile and getattr(profile, "is_private", False)
                    and str(getattr(client, "user_id", "") or "") != str(job.target_pk)):
                friendship = runner.run(client.user_friendship_v1(job.target_pk))
                if friendship is not None and not getattr(friendship, "following", False):
                    issue = ("پیج خصوصی است و حساب رصدکننده عضو تأییدشدهٔ آن نیست؛ "
                             "ابتدا با همین حساب Follow کنید و پس از پذیرش Resume بزنید")
                    job.streams.filter(finished=False).update(
                        issue=issue, finished=True, reconciled=False)
                    RelationshipScanEvent.objects.create(
                        job=job, stream="access", records=0, message=issue)
                    _finish(job)
                    return
            pages_this_task = (1 if job.observation_id else
                               settings.INSTRADAR_RELATIONSHIP_PAGES_PER_TASK)
            for _ in range(pages_this_task):
                job.refresh_from_db()
                if job.status != job.Status.RUNNING:
                    return
                states = job.streams.filter(finished=False)
                if stream_name:
                    states = states.filter(stream=stream_name)
                state = states.order_by("id").first()
                if state is None:
                    if not _finish(job) and job.observation_id:
                        from .observation_tasks import queue_observation
                        RelationshipScanJob.objects.filter(pk=job.pk).update(
                            status=job.Status.QUEUED, lease_until=None)
                        queue_observation(job.observation)
                    return
                expected = job.expected_followers if state.stream == state.Stream.FOLLOWERS else job.expected_following
                _reset_overflow_stream(job, state, expected)
                phase = f"دریافت {state.get_stream_display()}"
                started = time.monotonic()
                users, cursor, api, notice = runner.run(_fetch_page(
                    client, state, job.target_pk, expected, job.target, job.account_id))
                _commit_page(job, state, users, cursor, api, notice,
                             int((time.monotonic() - started) * 1000))
                record_account_success(job.account_id)
                if not users and not cursor and expected not in (None, 0):
                    _pause_empty_relationship(job, state)
                    if job.observation_id:
                        from .observation_tasks import queue_observation
                        queue_observation(job.observation)
                    return
                job.attempts = 0
                job.lease_until = timezone.now() + timedelta(seconds=settings.INSTRADAR_RELATIONSHIP_TIMEOUT + 30)
                job.save(update_fields=["attempts", "lease_until", "updated_at"])
                if _finish(job):
                    return
        if RelationshipScanJob.objects.filter(pk=job.pk, status=job.Status.RUNNING).update(
                status=job.Status.QUEUED, lease_until=None):
            if job.observation_id:
                from .observation_tasks import queue_observation
                queue_observation(job.observation)
            else:
                queue_relationships(job)
    except Exception as exc:
        exception_name = type(exc).__name__
        reason = (str(exc) if isinstance(exc, (RelationshipFetchError, InstagramSessionError))
                  else f"{phase} ناموفق بود؛ {exception_name}")[:500]
        record_account_failure(job.account_id, job.observation_id, exc, reason)
        permanent = getattr(exc, "permanent", False) or _is_permanent(exc)
        retry = not permanent and job.attempts < settings.INSTRADAR_MAX_RETRIES
        delay = min(settings.INSTRADAR_BACKOFF_MAX, settings.INSTRADAR_BACKOFF_BASE *
                    (2 ** max(0, job.attempts - 1)))
        changed = RelationshipScanJob.objects.filter(pk=job.pk, status=job.Status.RUNNING).update(
            status=job.Status.QUEUED if retry else job.Status.FAILED,
            error=reason, lease_until=timezone.now() + timedelta(seconds=delay) if retry else None,
        )
        if changed:
            RelationshipScanEvent.objects.create(
                job=job, stream="error", message=reason,
                duration_ms=int((time.monotonic() - started_total) * 1000))
            if retry:
                if job.observation_id:
                    from .observation_tasks import queue_observation
                    queue_observation(job.observation, countdown=delay)
                else:
                    queue_relationships(job, countdown=delay)
            elif permanent:
                InstagramAccount.objects.filter(pk=job.account_id).update(
                    status="needs_login", last_error=reason)
    finally:
        release_operation_lease(operation_lease)


@shared_task(name="radar.relationship_tasks.recover_relationships")
def recover_relationships():
    now = timezone.now()
    for pending in RelationshipScanJob.objects.filter(
            status=RelationshipScanJob.Status.RUNNING, lease_until__lt=now)[:100]:
        with transaction.atomic():
            job = RelationshipScanJob.objects.select_for_update().get(pk=pending.pk)
            if job.status != job.Status.RUNNING or not job.lease_until or job.lease_until >= now:
                continue
            job.status = job.Status.QUEUED if job.attempts < settings.INSTRADAR_MAX_RETRIES else job.Status.FAILED
            job.error = ("Worker قطع شد؛ ادامه از Cursor ذخیره‌شده" if job.status == job.Status.QUEUED
                         else "تعداد تلاش مجاز تمام شد")
            job.lease_until = None
            job.save(update_fields=["status", "error", "lease_until", "updated_at"])
        if job.status == job.Status.QUEUED:
            if job.observation_id:
                from .observation_tasks import queue_observation
                queue_observation(job.observation)
            else:
                queue_relationships(job)
    for job in RelationshipScanJob.objects.filter(
            status=RelationshipScanJob.Status.QUEUED,
            updated_at__lt=now - timedelta(minutes=2))[:100]:
        if job.lease_until and job.lease_until > now:
            continue
        if job.observation_id:
            from .observation_tasks import queue_observation
            queue_observation(job.observation)
        else:
            queue_relationships(job)
        RelationshipScanJob.objects.filter(pk=job.pk, status=job.Status.QUEUED).update(updated_at=now)
