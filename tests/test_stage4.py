import asyncio
from datetime import timedelta
from types import SimpleNamespace as Obj
from unittest.mock import AsyncMock, MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from radar.models import (AccountAlias, AccountRelationship, InstagramAccount,
                          InstagramIdentity, MediaSyncJob, Post, PostSnapshot, RelationshipChange,
                          RelationshipScanJob, RelationshipSnapshot,
                          RelationshipSyncState)
from radar.relationship_store import (reconcile_relationship_stream,
                                      save_relationship_page, upsert_identity)
from radar.browser_relationships import (BrowserRelationshipCollector,
                                         extract_relationship_cursor,
                                         extract_relationship_users,
                                         relationship_response_kind,
                                         response_json_payloads)
from radar.relationship_tasks import (FOLLOW_LIST_DOC_IDS, RelationshipFetchError,
                                      _commit_page, _connect,
                                      _fetch_page, _finish, recover_relationships,
                                      _pause_empty_relationship, relationship_page)


def instagram_user(pk, username, full_name=""):
    return Obj(pk=str(pk), username=username, full_name=full_name,
               is_private=False, is_verified=False, profile_pic_url="")


class RelationshipStageTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("stage4", password="stage4-password")
        self.other = get_user_model().objects.create_user("stage4-other", password="stage4-password")
        self.account = InstagramAccount.objects.create(
            owner=self.user, username="runner", encrypted_cookies=b"encrypted")
        self.seed = upsert_identity(instagram_user(100, "seed"))[0]

    def job(self, *, mode="both", expected_followers=0, expected_following=0,
            status="running", max_pages=0):
        return RelationshipScanJob.objects.create(
            owner=self.user, account=self.account, target="seed", target_pk="100",
            seed_identity=self.seed, mode=mode, status=status,
            expected_followers=expected_followers,
            expected_following=expected_following, max_pages=max_pages)

    def test_direction_idempotence_mutual_and_username_history(self):
        job = self.job(expected_followers=1, expected_following=1)
        follower = instagram_user(200, "old_name", "Contact")
        self.assertEqual(save_relationship_page(job, "followers", [follower]), (1, 0))
        self.assertTrue(AccountRelationship.objects.filter(
            source__instagram_pk="200", target=self.seed).exists())
        # The same Instagram PK renamed must remain one identity and retain both aliases.
        self.assertEqual(save_relationship_page(
            job, "followers", [instagram_user(200, "new_name", "Contact")]), (1, 1))
        self.assertEqual(InstagramIdentity.objects.filter(instagram_pk="200").count(), 1)
        identity = InstagramIdentity.objects.get(instagram_pk="200")
        self.assertEqual(identity.current_username, "new_name")
        self.assertEqual(set(AccountAlias.objects.filter(identity=identity).values_list(
            "username", flat=True)), {"old_name", "new_name"})
        self.assertEqual(job.snapshots.count(), 1)
        # Following direction is seed -> contact, making this pair mutual.
        save_relationship_page(job, "following", [instagram_user(200, "new_name")])
        self.assertEqual(AccountRelationship.objects.count(), 2)
        self.assertTrue(AccountRelationship.objects.filter(
            source=self.seed, target=identity, is_active=True).exists())

    def test_complete_snapshot_ends_missing_edge_and_reactivation_is_recorded(self):
        first = self.job(mode="followers", expected_followers=2)
        state1 = RelationshipSyncState.objects.create(
            job=first, stream="followers", finished=True)
        save_relationship_page(first, "followers", [instagram_user(201, "one"),
                                                     instagram_user(202, "two")])
        self.assertEqual(reconcile_relationship_stream(first, state1), 0)

        second = self.job(mode="followers", expected_followers=1)
        state2 = RelationshipSyncState.objects.create(
            job=second, stream="followers", finished=True)
        save_relationship_page(second, "followers", [instagram_user(201, "one")])
        self.assertEqual(reconcile_relationship_stream(second, state2), 1)
        ended = AccountRelationship.objects.get(source__instagram_pk="202", target=self.seed)
        self.assertFalse(ended.is_active)
        self.assertTrue(second.changes.filter(
            relationship=ended, change_type=RelationshipChange.Type.UNFOLLOWED).exists())

        third = self.job(mode="followers", expected_followers=2)
        save_relationship_page(third, "followers", [instagram_user(201, "one"),
                                                     instagram_user(202, "two")])
        ended.refresh_from_db()
        self.assertTrue(ended.is_active)
        self.assertTrue(third.changes.filter(
            relationship=ended, change_type=RelationshipChange.Type.REACTIVATED).exists())

    def test_incomplete_snapshot_never_ends_existing_relationships(self):
        first = self.job(mode="followers", expected_followers=2)
        save_relationship_page(first, "followers", [instagram_user(210, "one"),
                                                     instagram_user(211, "two")])
        second = self.job(mode="followers", expected_followers=2)
        state = RelationshipSyncState.objects.create(job=second, stream="followers", finished=True)
        save_relationship_page(second, "followers", [instagram_user(210, "one")])
        self.assertEqual(reconcile_relationship_stream(second, state), 0)
        state.refresh_from_db()
        self.assertIn("1", state.issue)
        self.assertEqual(AccountRelationship.objects.filter(is_active=True).count(), 2)
        self.assertFalse(second.changes.filter(change_type="unfollowed").exists())

    def test_cursor_checkpoint_page_cap_and_resume(self):
        job = self.job(mode="followers", expected_followers=2, max_pages=1)
        state = RelationshipSyncState.objects.create(job=job, stream="followers")
        _commit_page(job, state, [instagram_user(220, "page_one")], "next-cursor",
                     "private", "", 33)
        state.refresh_from_db()
        self.assertEqual((state.cursor, state.pages, state.seen), ("next-cursor", 1, 1))
        self.assertTrue(state.finished)
        self.assertIn("سقف صفحه", state.issue)
        self.assertTrue(_finish(job))
        job.refresh_from_db()
        self.assertEqual(job.status, "partial")
        self.client.force_login(self.user)
        with patch("radar.views.queue_relationships"):
            response = self.client.post(reverse("radar:resume_relationship_job", args=[job.pk]))
        self.assertEqual(response.status_code, 302)
        state.refresh_from_db()
        job.refresh_from_db()
        self.assertEqual(state.cursor, "next-cursor")
        self.assertFalse(state.finished)
        self.assertEqual(job.status, "queued")
        self.assertEqual(job.max_pages, 0)

    def test_repeated_browser_page_stops_partial_without_fake_progress(self):
        job = self.job(mode="followers", expected_followers=10)
        state = RelationshipSyncState.objects.create(
            job=job, stream="followers", cursor="offset:1", pages=1,
            seen=1, api="browser")
        repeated = instagram_user(220, "same_browser_row")
        save_relationship_page(job, "followers", [repeated])

        _commit_page(job, state, [repeated], "offset:2", "browser", "", 12)

        state.refresh_from_db()
        event = job.events.filter(stream="followers").latest("created_at")
        self.assertTrue(state.finished)
        self.assertEqual(state.cursor, "")
        self.assertEqual(state.seen, 1)
        self.assertIn("همان 1 رابطه", state.issue)
        self.assertEqual(event.records, 0)
        self.assertIn("0 رابطهٔ جدید", event.message)

    def test_empty_private_result_uses_graphql(self):
        state = RelationshipSyncState(stream="followers", pages=0, cursor="")
        client = Obj(
            user_followers_v1_chunk=AsyncMock(return_value=([], "")),
            user_followers_gql_chunk=AsyncMock(return_value=([instagram_user(230, "fallback")], "gql-next")),
        )
        with override_settings(INSTRADAR_RELATIONSHIP_PAGE_SIZE=10,
                               INSTRADAR_MIN_DELAY_MS=1, INSTRADAR_MAX_DELAY_MS=1), \
                patch("radar.relationship_tasks.asyncio.sleep", new_callable=AsyncMock):
            users, cursor, api, notice = asyncio.run(_fetch_page(client, state, "100", 10))
        self.assertEqual((len(users), cursor, api), (1, "gql-next", "gql"))
        self.assertIn("GraphQL", notice)
        client.user_followers_gql_chunk.assert_awaited_once()

    def test_empty_results_do_not_mark_page_complete_when_expected_count_still_exists(self):
        job = self.job(mode="followers", expected_followers=10)
        state = RelationshipSyncState.objects.create(job=job, stream="followers")
        _commit_page(job, state, [], "", "gql", "همه APIهای مستقیم پاسخ خالی دادند", 10)
        state.refresh_from_db()
        self.assertFalse(state.finished)
        self.assertIn("خالی", state.issue)

    def test_empty_no_cursor_pauses_relationship_job_instead_of_looping(self):
        job = self.job(mode="followers", expected_followers=10, status="running")
        state = RelationshipSyncState.objects.create(job=job, stream="followers", api="gql")
        _pause_empty_relationship(job, state)
        job.refresh_from_db()
        state.refresh_from_db()
        self.assertEqual(job.status, "partial")
        self.assertFalse(state.finished)
        self.assertIn("Cursor", state.issue)

    def test_resume_empty_relationship_starts_with_fresh_private_graphql(self):
        job = self.job(mode="followers", expected_followers=10, status="partial")
        state = RelationshipSyncState.objects.create(
            job=job,
            stream="followers",
            api="gql",
            pages=1,
            issue="پاسخ API خالی بود",
        )
        self.client.force_login(self.user)
        with patch("radar.views.queue_relationships"):
            response = self.client.post(
                reverse("radar:resume_relationship_job", args=[job.pk])
            )
        self.assertEqual(response.status_code, 302)
        state.refresh_from_db()
        self.assertEqual((state.api, state.cursor, state.pages),
                         ("private_gql", "", 0))

    def test_private_failure_uses_new_private_graphql_route(self):
        from aiograpi.exceptions import ClientError
        state = RelationshipSyncState(stream="followers", pages=0, cursor="", api="private")
        client = Obj(
            user_followers_v1_chunk=AsyncMock(side_effect=ClientError("do-not-store-this")),
            user_followers_private_gql_chunk=AsyncMock(
                return_value=([instagram_user(231, "private_gql")], "next-private-gql")),
        )
        with override_settings(INSTRADAR_RELATIONSHIP_PAGE_SIZE=10,
                               INSTRADAR_MIN_DELAY_MS=1, INSTRADAR_MAX_DELAY_MS=1), \
                patch("radar.relationship_tasks.asyncio.sleep", new_callable=AsyncMock):
            users, cursor, api, notice = asyncio.run(_fetch_page(client, state, "100", 10))
        self.assertEqual((len(users), cursor, api), (1, "next-private-gql", "private_gql"))
        self.assertIn("Mobile GraphQL", notice)

    def test_private_graphql_routes_send_current_registered_doc_ids(self):
        cases = (
            (
                "followers",
                "private_graphql_followers_list",
                "xdt_api__v1__friendships__followers",
            ),
            (
                "following",
                "private_graphql_following_list",
                "xdt_api__v1__friendships__following",
            ),
        )
        for stream, method_name, root_name in cases:
            with self.subTest(stream=stream):
                method = AsyncMock(return_value={
                    "data": {
                        root_name: {
                            "users": [{"pk": "234", "username": f"{stream}_user"}],
                            "next_max_id": "fresh-next",
                        }
                    }
                })
                client = Obj(
                    rank_token="rank-token",
                    _private_graphql_root=lambda raw, name: raw["data"].get(name, {}),
                    **{method_name: method},
                )
                state = RelationshipSyncState(
                    stream=stream,
                    pages=0,
                    cursor="",
                    api="private_gql",
                )
                with override_settings(
                        INSTRADAR_RELATIONSHIP_PAGE_SIZE=10,
                        INSTRADAR_MIN_DELAY_MS=1,
                        INSTRADAR_MAX_DELAY_MS=1), patch(
                            "radar.relationship_tasks.asyncio.sleep",
                            new_callable=AsyncMock):
                    users, cursor, api, _ = asyncio.run(
                        _fetch_page(client, state, "100", 10)
                    )
                self.assertEqual((users[0].username, cursor, api),
                                 (f"{stream}_user", "fresh-next", "private_gql"))
                self.assertEqual(
                    method.await_args.kwargs["client_doc_id"],
                    FOLLOW_LIST_DOC_IDS[stream],
                )

    def test_safe_failure_reports_routes_without_upstream_body(self):
        from aiograpi.exceptions import ClientError
        failure = ClientError("cookie-or-response-secret")
        state = RelationshipSyncState(stream="followers", pages=0, cursor="", api="private")
        client = Obj(
            user_followers_v1_chunk=AsyncMock(side_effect=failure),
            user_followers_private_gql_chunk=AsyncMock(side_effect=failure),
            user_followers_gql_chunk=AsyncMock(side_effect=failure),
            user_followers=AsyncMock(side_effect=failure),
        )
        with override_settings(INSTRADAR_MIN_DELAY_MS=1, INSTRADAR_MAX_DELAY_MS=1), \
                patch("radar.relationship_tasks.asyncio.sleep", new_callable=AsyncMock):
            with self.assertRaises(RelationshipFetchError) as caught:
                asyncio.run(_fetch_page(client, state, "100", 10))
        self.assertIn("Mobile Private:ClientError", str(caught.exception))
        self.assertIn("Web GraphQL:ClientError", str(caught.exception))
        self.assertNotIn("cookie-or-response-secret", str(caught.exception))

    def test_profile_resolution_has_independent_fallback(self):
        profile = Obj(pk="100", username="seed", full_name="Seed User",
                      is_private=True, is_verified=True, profile_pic_url="",
                      follower_count=12, following_count=7, media_count=4)
        client = Obj(
            user_info_by_username_v1=AsyncMock(side_effect=RuntimeError("mobile blocked")),
            user_info_by_username_v2_gql=AsyncMock(return_value=profile),
        )
        with patch("radar.relationship_tasks.aiograpi_client", new=AsyncMock(return_value=client)):
            connected, result, route = asyncio.run(_connect(self.account, "seed", ""))
        self.assertIs(connected, client)
        self.assertEqual(result.pk, "100")
        self.assertEqual(route, "Web-v2-Username")

    def test_profile_identity_persists_counts_and_visibility(self):
        profile = Obj(pk="100", username="seed", full_name="Seed User",
                      is_private=True, is_verified=True, profile_pic_url="",
                      follower_count=12, following_count=7, media_count=4)
        identity, _, _ = upsert_identity(profile)
        identity.refresh_from_db()
        self.assertEqual((identity.followers_count, identity.following_count,
                          identity.media_count), (12, 7, 4))
        self.assertTrue(identity.is_private)
        self.assertTrue(identity.is_verified)

    def test_aiograpi_failure_uses_same_account_browser_fallback(self):
        profile = instagram_user(100, "seed")
        with patch("radar.relationship_tasks.aiograpi_client",
                   new=AsyncMock(side_effect=RuntimeError("private api blocked"))), \
                patch("radar.relationship_tasks.browser.call",
                      return_value={"ok": True, "profile": profile.__dict__}) as browser_call:
            connected, result, route = asyncio.run(
                _connect(self.account, "seed", "100", account_id=self.account.pk))
        self.assertIsNone(connected)
        self.assertEqual(result.pk, "100")
        self.assertIn("Chrome Browser fallback", route)
        self.assertIn("RuntimeError", route)
        self.assertEqual(browser_call.call_args.args[:2], ("profile", self.account.pk))

    def test_web_only_session_skips_mobile_profile_and_keeps_client(self):
        profile = instagram_user(100, "seed")
        client = Obj(
            _instradar_private_ready=False,
            _instradar_session_error="LoginRequired/HTTP 401",
            user_info_v1=AsyncMock(side_effect=AssertionError("mobile must be skipped")),
            user_info_v2_gql=AsyncMock(return_value=profile),
        )
        with patch("radar.relationship_tasks.aiograpi_client",
                   new=AsyncMock(return_value=client)):
            connected, result, route = asyncio.run(
                _connect(self.account, "seed", "100", account_id=self.account.pk))
        self.assertIs(connected, client)
        self.assertEqual(result.pk, "100")
        self.assertIn("Web-ID", route)
        self.assertIn("Web session فعال", route)
        client.user_info_v1.assert_not_awaited()

    def test_web_only_session_skips_private_relationship_routes(self):
        state = RelationshipSyncState(stream="followers", pages=0, cursor="", api="private_gql")
        client = Obj(
            _instradar_private_ready=False,
            user_followers_v1_chunk=AsyncMock(side_effect=AssertionError("mobile must be skipped")),
            user_followers_private_gql_chunk=AsyncMock(
                side_effect=AssertionError("mobile gql must be skipped")),
            public_request=AsyncMock(return_value={
                "users": [{"pk": "202", "username": "web_only_user"}],
                "next_max_id": "web-only-next",
            }),
            user_followers_gql_chunk=AsyncMock(side_effect=AssertionError("fallback not expected")),
        )
        with override_settings(INSTRADAR_RELATIONSHIP_PAGE_SIZE=10,
                               INSTRADAR_MIN_DELAY_MS=1, INSTRADAR_MAX_DELAY_MS=1), \
                patch("radar.relationship_tasks.asyncio.sleep", new_callable=AsyncMock):
            users, cursor, api, _ = asyncio.run(
                _fetch_page(client, state, "100", 10, "seed", self.account.pk))
        self.assertEqual((users[0].username, cursor, api),
                         ("web_only_user", "web-only-next", "web_v1"))
        client.user_followers_v1_chunk.assert_not_awaited()
        client.user_followers_private_gql_chunk.assert_not_awaited()

    def test_browser_fallback_collects_relationship_page_without_new_session(self):
        state = RelationshipSyncState(stream="followers", pages=0, cursor="", api="browser")
        browser_result = {"users": [{"pk": "201", "username": "browser_follower"}],
                          "cursor": "next-browser"}
        with patch("radar.relationship_tasks.browser.call", return_value=browser_result) as browser_call, \
                patch("radar.relationship_tasks.asyncio.sleep", new_callable=AsyncMock):
            users, cursor, api, notice = asyncio.run(
                _fetch_page(None, state, "100", 1, "seed", self.account.pk))
        self.assertEqual((users[0].username, cursor, api),
                         ("browser_follower", "next-browser", "browser"))
        self.assertEqual(browser_call.call_args.args[:2], ("relationships", self.account.pk))

    def test_provider_pagination_reuses_cursor_and_records_provider(self):
        state = RelationshipSyncState(stream="followers", pages=0, cursor="", api="private")
        client = Obj(
            user_followers_v1_chunk=AsyncMock(side_effect=[
                ([instagram_user(301, "page_one")], "A"),
                ([instagram_user(302, "page_two")], "B"),
            ]),
            user_followers_private_gql_chunk=AsyncMock(
                side_effect=AssertionError("fallback not expected")),
            user_followers_gql_chunk=AsyncMock(
                side_effect=AssertionError("fallback not expected")),
        )
        with override_settings(INSTRADAR_RELATIONSHIP_PAGE_SIZE=1,
                               INSTRADAR_MIN_DELAY_MS=1, INSTRADAR_MAX_DELAY_MS=1), \
                patch("radar.relationship_tasks.asyncio.sleep", new_callable=AsyncMock):
            first = asyncio.run(_fetch_page(client, state, "100", 2))
            state.cursor = first[1]
            state.pages = 1
            second = asyncio.run(_fetch_page(client, state, "100", 2))
        self.assertEqual((first[2], first[1], first[0][0].username),
                         ("private", "A", "page_one"))
        self.assertEqual((second[2], second[1], second[0][0].username),
                         ("private", "B", "page_two"))
        calls = client.user_followers_v1_chunk.await_args_list
        self.assertEqual([call.kwargs["max_id"] for call in calls], ["", "A"])
        state.api = first[2]
        self.assertEqual(state.provider, "aiograpi")

    def test_browser_fallback_resets_foreign_provider_cursor(self):
        state = RelationshipSyncState(stream="followers", pages=1,
                                      cursor="A", api="private")
        client = Obj(
            user_followers_v1_chunk=AsyncMock(side_effect=RuntimeError("blocked")),
            user_followers_private_gql_chunk=AsyncMock(side_effect=RuntimeError("blocked")),
            user_followers_gql_chunk=AsyncMock(side_effect=RuntimeError("blocked")),
            user_followers=AsyncMock(side_effect=RuntimeError("blocked")),
        )
        with patch("radar.relationship_tasks.browser.call", return_value={
                "users": [{"pk": "303", "username": "browser_page_two"}], "cursor": "B"}) as call, \
                patch("radar.relationship_tasks.asyncio.sleep", new_callable=AsyncMock):
            users, cursor, api, _ = asyncio.run(
                _fetch_page(client, state, "100", 10, "seed", self.account.pk))
        self.assertEqual((users[0].username, cursor, api),
                         ("browser_page_two", "B", "browser"))
        self.assertEqual(call.call_args.kwargs["cursor"], "")

    def test_empty_later_page_still_tries_fallback_until_expected_count(self):
        state = RelationshipSyncState(stream="followers", pages=1,
                                      cursor="A", seen=1, api="private")
        client = Obj(
            user_followers_v1_chunk=AsyncMock(return_value=([], "")),
            user_followers_private_gql_chunk=AsyncMock(return_value=([], "")),
            user_followers_gql_chunk=AsyncMock(return_value=([
                instagram_user(304, "fallback_page")], "B")),
        )
        with override_settings(INSTRADAR_RELATIONSHIP_PAGE_SIZE=1,
                               INSTRADAR_MIN_DELAY_MS=1, INSTRADAR_MAX_DELAY_MS=1), \
                patch("radar.relationship_tasks.asyncio.sleep", new_callable=AsyncMock):
            users, cursor, api, _ = asyncio.run(_fetch_page(client, state, "100", 2))
        self.assertEqual((users[0].username, cursor, api),
                         ("fallback_page", "B", "gql"))
        client.user_followers_gql_chunk.assert_awaited_once()

    def test_known_seed_refreshes_profile_but_survives_refresh_failure(self):
        client = Obj(
            user_info_v1=AsyncMock(side_effect=RuntimeError("mobile unavailable")),
            user_info=AsyncMock(side_effect=RuntimeError("auto unavailable")),
        )
        with patch("radar.relationship_tasks.aiograpi_client", new=AsyncMock(return_value=client)):
            connected, result, route = asyncio.run(
                _connect(self.account, "seed", "100", has_seed=True))
        self.assertIs(connected, client)
        self.assertIsNone(result)
        self.assertIn("Refresh پروفایل ناموفق", route)
        client.user_info_v1.assert_awaited_once_with("100")

    def test_empty_mobile_routes_use_browser_cookie_web_api(self):
        from aiograpi.exceptions import ClientGraphqlError
        state = RelationshipSyncState(stream="followers", pages=0, cursor="", api="private")
        client = Obj(
            user_followers_v1_chunk=AsyncMock(return_value=([], "")),
            user_followers_private_gql_chunk=AsyncMock(
                side_effect=ClientGraphqlError("private gql empty")),
            public_request=AsyncMock(return_value={
                "users": [{"pk": "233", "username": "web_cookie_follower"}],
                "next_max_id": "web-next",
            }),
            user_followers_gql_chunk=AsyncMock(
                side_effect=AssertionError("legacy gql must not run")),
        )
        with override_settings(INSTRADAR_RELATIONSHIP_PAGE_SIZE=10,
                               INSTRADAR_MIN_DELAY_MS=1, INSTRADAR_MAX_DELAY_MS=1), \
                patch("radar.relationship_tasks.asyncio.sleep", new_callable=AsyncMock):
            users, cursor, api, notice = asyncio.run(
                _fetch_page(client, state, "100", 10, "seed"))
        self.assertEqual((len(users), cursor, api), (1, "web-next", "web_v1"))
        self.assertEqual(users[0].username, "web_cookie_follower")
        self.assertIn("Web Friendship API", notice)
        client.public_request.assert_awaited_once()

    def test_live_browser_parser_accepts_relationship_users_only(self):
        payload = {
            "data": {
                "xdt_api__v1__friendships__followers": {
                    "users": [
                        {"pk": "991", "username": "Visible.User", "full_name": "Visible"},
                        {"pk": "992", "username": "second_user", "is_verified": True},
                    ]
                },
                # An identity elsewhere in the GraphQL response must not leak
                # into the relationship graph merely because it looks like a user.
                "viewer": {"id": "777", "username": "logged_in_account"},
            }
        }
        users = extract_relationship_users(payload, "followers")
        self.assertEqual([item["pk"] for item in users], ["991", "992"])
        self.assertEqual(users[0]["username"], "visible.user")

    def test_graphql_friendly_name_classifies_new_relationship_request(self):
        response = Obj(
            url="https://www.instagram.com/graphql/query",
            request=Obj(
                post_data="doc_id=123",
                headers={
                    "x-fb-friendly-name": "PolarisFollowersListActionQuery",
                },
            ),
        )
        self.assertEqual(relationship_response_kind(response, "followers"), "graphql")
        self.assertEqual(
            relationship_response_kind(response, "following"),
            "possible_graphql",
        )

    def test_streamed_browser_response_is_decoded_without_response_json(self):
        class StreamedResponse:
            def json(self):
                raise ValueError("multipart")

            def body(self):
                return (
                    b'for (;;);{"data":{"xdt_api__v1__friendships__followers":'
                    b'{"users":[{"pk":"997","username":"stream_user"}],'
                    b'"next_max_id":"stream-next"}}}\n'
                    b'--graphql\r\n{"extensions":{"is_final":true}}'
                )

        payloads = response_json_payloads(StreamedResponse())
        users = [
            user
            for payload in payloads
            for user in extract_relationship_users(payload, "followers")
        ]
        self.assertEqual([user["pk"] for user in users], ["997"])
        self.assertEqual(
            extract_relationship_cursor(payloads[0], "followers"),
            "stream-next",
        )

    def test_trusted_graphql_collection_does_not_capture_viewer(self):
        payload = {
            "data": {
                "users": [{"id": "998", "username": "member"}],
                "viewer": {"id": "999", "username": "signed_in"},
            }
        }
        users = extract_relationship_users(
            payload,
            "followers",
            trusted_direct=True,
        )
        self.assertEqual([user["pk"] for user in users], ["998"])

    def test_nested_relay_json_string_and_current_id_fields_are_decoded(self):
        payload = {
            "payload": '{"data":{"users":[{"rest_id":"1001",'
                       '"username":"nested_member"}],"next_cursor":"nested-next"}}'
        }
        users = extract_relationship_users(payload, "followers", trusted_direct=True)
        self.assertEqual([user["pk"] for user in users], ["1001"])
        self.assertEqual(
            extract_relationship_cursor(payload, "followers", trusted_direct=True),
            "nested-next",
        )

    def test_dom_react_harvest_accepts_numeric_identity_and_excludes_target(self):
        dialog = MagicMock()
        dialog.evaluate.return_value = {
            "users": [
                {"pk": "100", "username": "seed"},
                {"user_id": "1002", "username": "visible_member"},
                {"pk": "not-numeric", "username": "invalid_member"},
            ],
            "handles": 3,
            "inspected": 12,
        }
        state = {
            "target": "seed",
            "dialog": dialog,
            "users": {},
            "diagnostics": {
                "dom_scans": 0, "dom_handles": 0, "dom_inspected": 0,
                "dom_matched": 0, "dom_errors": 0,
            },
        }
        BrowserRelationshipCollector()._harvest_dom(state, "followers")
        self.assertEqual(list(state["users"]), ["1002"])
        self.assertEqual(state["diagnostics"]["dom_handles"], 3)
        self.assertEqual(state["diagnostics"]["dom_matched"], 1)

    def test_live_browser_network_is_final_fallback_for_empty_apis(self):
        state = RelationshipSyncState(stream="followers", pages=0, cursor="", api="private")
        client = Obj(
            user_followers_v1_chunk=AsyncMock(return_value=([], "")),
            user_followers_private_gql_chunk=AsyncMock(return_value=([], "")),
            public_request=AsyncMock(return_value={"users": []}),
            user_followers_gql_chunk=AsyncMock(return_value=([], "")),
        )
        browser_result = {
            "users": [{
                "pk": "993", "username": "browser_follower", "full_name": "Browser",
                "is_private": False, "is_verified": False, "profile_pic_url": "",
            }],
            "cursor": "1",
        }
        with override_settings(INSTRADAR_RELATIONSHIP_PAGE_SIZE=10,
                               INSTRADAR_MIN_DELAY_MS=1, INSTRADAR_MAX_DELAY_MS=1,
                               INSTRADAR_RELATIONSHIP_REQUEST_TIMEOUT=30), \
                patch("radar.relationship_tasks.asyncio.sleep", new_callable=AsyncMock), \
                patch("radar.relationship_tasks.browser.call", return_value=browser_result) as call:
            users, cursor, api, notice = asyncio.run(
                _fetch_page(client, state, "100", 10, "seed", self.account.pk))
        self.assertEqual((len(users), cursor, api), (1, "1", "browser"))
        self.assertEqual(users[0].username, "browser_follower")
        self.assertIn("Chrome", notice)
        call.assert_called_once_with(
            "relationships", self.account.pk,
            target="seed", target_pk="100", stream="followers", cursor="", limit=10,
            expected_total=10)

    def test_browser_collector_caps_dom_results_at_expected_total(self):
        collector = BrowserRelationshipCollector()
        collector._browser_fetch = MagicMock(return_value={
            "users": [], "cursor": "", "captured": 0, "exhausted": False,
            "source": "instagram-browser-fetch", "diagnostic": {"status": 200},
        })
        page = MagicMock()
        page.is_closed.return_value = False
        dialog = MagicMock()
        dialog.evaluate.return_value = {
            "users": [
                {"pk": "1", "username": "actual_following"},
                {"pk": "2", "username": "suggested_one"},
                {"pk": "3", "username": "suggested_two"},
            ],
            "handles": 3, "inspected": 5,
        }
        state = {
            "page": page, "target": "seed", "dialog": dialog,
            "users": {}, "exhausted": False,
            "diagnostics": {
                "responses": 0, "json_payloads": 0, "capture_errors": 0,
                "active_responses": 0, "possible_graphql": 0,
                "direct_status": 200, "last_status": 200, "last_kind": "",
                "dom_scans": 0, "dom_handles": 0, "dom_inspected": 0,
                "dom_matched": 0, "dom_errors": 0,
            },
        }
        collector.sessions[(self.account.pk, "seed", "following")] = state
        collector._open = MagicMock(return_value=state)
        result = collector.fetch(
            MagicMock(), self.account.pk, "seed", "following",
            target_pk="100", limit=10, expected_total=1,
        )
        self.assertEqual([item["username"] for item in result["users"]],
                         ["actual_following"])

    def test_parser_accepts_current_pk_id_and_strong_id_fields(self):
        payload = {"users": [
            {"pk_id": "994", "username": "pk_id_user"},
            {"strong_id__": "995", "username": "strong_id_user"},
        ]}
        users = extract_relationship_users(payload, "following", trusted_direct=True)
        self.assertEqual([item["pk"] for item in users], ["994", "995"])

    def test_browser_origin_fetch_uses_target_id_and_returns_cursor(self):
        page = MagicMock()
        page.url = "https://www.instagram.com/seed/"
        login = MagicMock()
        login.count.return_value = 0
        page.locator.return_value = login
        page.evaluate.return_value = {
            "status": 200,
            "payload": {
                "users": [{"pk_id": "996", "username": "browser_web_user"}],
                "next_max_id": "browser-next",
            },
        }
        context = MagicMock()
        context.pages = [page]
        result = BrowserRelationshipCollector().fetch(
            context, self.account.pk, "seed", "followers", target_pk="100", limit=10)
        self.assertEqual(result["users"][0]["pk"], "996")
        self.assertEqual(result["cursor"], "direct:browser-next")
        self.assertEqual(result["source"], "instagram-browser-fetch")

    def test_browser_collector_accepts_current_non_exact_relationship_href(self):
        page = MagicMock()
        page.url = "https://www.instagram.com/seed/"
        page.is_closed.return_value = False
        login = MagicMock()
        login.count.return_value = 0
        link = MagicMock()
        dialog = MagicMock()

        def locator(selector):
            if selector == 'input[name="username"]':
                return login
            if selector == 'a[href*="/followers"]':
                return Obj(first=link)
            if selector == 'div[role="dialog"]':
                return Obj(last=dialog)
            raise AssertionError(selector)

        page.locator.side_effect = locator
        context = MagicMock()
        context.new_page.return_value = page
        state = BrowserRelationshipCollector()._open(context, "seed", "followers")
        self.assertIs(state["dialog"], dialog)
        link.wait_for.assert_called_once_with(state="visible", timeout=8000)
        link.click.assert_called_once_with(timeout=15000)

    def test_browser_collector_uses_direct_full_page_route_when_link_is_absent(self):
        page = MagicMock()
        page.url = "https://www.instagram.com/seed/"
        page.is_closed.return_value = False
        login = MagicMock()
        login.count.return_value = 0
        link = MagicMock()
        link.wait_for.side_effect = RuntimeError("old selector missing")
        dialog = MagicMock()
        dialog.wait_for.side_effect = RuntimeError("full page layout")
        main = MagicMock()

        def goto(url, **kwargs):
            page.url = url

        def locator(selector):
            if selector == 'input[name="username"]':
                return login
            if selector == 'a[href*="/following"]':
                return Obj(first=link)
            if selector == 'div[role="dialog"]':
                return Obj(last=dialog)
            if selector == "main":
                return Obj(first=main)
            raise AssertionError(selector)

        page.goto.side_effect = goto
        page.locator.side_effect = locator
        context = MagicMock()
        context.new_page.return_value = page
        state = BrowserRelationshipCollector()._open(context, "seed", "following")
        self.assertEqual(page.goto.call_args_list[-1].args[0],
                         "https://www.instagram.com/seed/following/")
        self.assertIs(state["dialog"], main)

    def test_relationship_task_crawls_with_saved_seed(self):
        job = self.job(mode="followers", expected_followers=1, status="queued")
        RelationshipSyncState.objects.create(job=job, stream="followers")
        client = Obj(get_settings=lambda: {"user_agent": "safe-test"})
        with patch("radar.relationship_tasks._connect", new=AsyncMock(
                return_value=(client, None, "شناسهٔ ذخیره‌شده PostgreSQL"))), \
                patch("radar.relationship_tasks._fetch_page", new=AsyncMock(
                    return_value=([instagram_user(232, "saved_seed_follower")], "", "private", ""))), \
                patch("radar.relationship_tasks.seal", return_value=b"encrypted-settings"):
            relationship_page(str(job.pk))
        job.refresh_from_db()
        self.assertEqual(job.status, "done")
        self.assertEqual(job.snapshots.count(), 1)
        self.assertTrue(job.events.filter(stream="profile").exists())

    def test_overflow_snapshot_is_reset_before_clean_resume(self):
        from radar.relationship_tasks import _reset_overflow_stream
        job = self.job(mode="following", expected_following=1)
        state = RelationshipSyncState.objects.create(
            job=job, stream="following", seen=30, pages=1,
            cursor="offset:30", api="browser", finished=True,
        )
        seed = InstagramIdentity.objects.create(
            instagram_pk="7000", current_username="overflow_seed")
        job.seed_identity = seed
        job.save(update_fields=["seed_identity"])
        for number in range(2):
            identity = InstagramIdentity.objects.create(
                instagram_pk=str(7100 + number), current_username=f"suggested_{number}")
            relation = AccountRelationship.objects.create(
                source=seed, target=identity, last_seen_job=job)
            RelationshipSnapshot.objects.create(
                job=job, relationship=relation, source=seed, target=identity)
        self.assertTrue(_reset_overflow_stream(job, state, 1))
        state.refresh_from_db()
        self.assertEqual((state.seen, state.pages, state.cursor), (0, 0, ""))
        self.assertFalse(state.finished)
        self.assertFalse(job.snapshots.exists())
        self.assertFalse(AccountRelationship.objects.filter(
            last_seen_job=job, is_active=True).exists())

    def test_stalled_worker_recovers_without_losing_cursor(self):
        job = self.job(mode="followers", expected_followers=10)
        job.lease_until = timezone.now() - timedelta(seconds=5)
        job.attempts = 1
        job.save(update_fields=["lease_until", "attempts"])
        state = RelationshipSyncState.objects.create(
            job=job, stream="followers", cursor="saved-cursor", pages=3, seen=8)
        with patch("radar.relationship_tasks.queue_relationships") as queue:
            recover_relationships()
        job.refresh_from_db()
        state.refresh_from_db()
        self.assertEqual(job.status, "queued")
        self.assertEqual((state.cursor, state.pages, state.seen), ("saved-cursor", 3, 8))
        queue.assert_called_once()

    def test_create_pages_visibility_and_mutual_badge(self):
        self.client.force_login(self.user)
        with patch("radar.views.queue_relationships") as queue:
            response = self.client.post(reverse("radar:create_relationship_job"), {
                "account_id": self.account.pk, "target": "target.page",
                "mode": "both", "max_pages": "1"})
        self.assertEqual(response.status_code, 302)
        created = RelationshipScanJob.objects.get(target="target.page")
        self.assertEqual(set(created.streams.values_list("stream", flat=True)),
                         {"followers", "following"})
        self.assertEqual(set(created.streams.values_list("api", flat=True)),
                         {"private_gql"})
        queue.assert_called_once()

        visible = self.job(expected_followers=1, expected_following=1, status="done")
        save_relationship_page(visible, "followers", [instagram_user(240, "mutual")])
        save_relationship_page(visible, "following", [instagram_user(240, "mutual")])
        for route in ("relationship_jobs", "contact_list", "relationship_list",
                      "relationship_changes"):
            self.assertEqual(self.client.get(reverse("radar:" + route)).status_code, 200)
        self.assertContains(self.client.get(reverse("radar:relationship_list")), "دوطرفه")
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(reverse(
            "radar:relationship_job_detail", args=[visible.pk])).status_code, 404)
        self.assertNotContains(self.client.get(reverse("radar:contact_list")), "mutual")

    def test_relationship_job_reuses_post_owner_instagram_id(self):
        media_job = MediaSyncJob.objects.create(
            owner=self.user, account=self.account, target="seed", target_pk="100", status="done")
        post = Post.objects.create(
            media_pk="stage4-known", shortcode="STAGE4KNOWN", owner_pk="100",
            owner_username="seed", owner_identity=self.seed, discovered_from="seed")
        PostSnapshot.objects.create(job=media_job, post=post, caption_hash="")
        self.client.force_login(self.user)
        with patch("radar.views.queue_relationships"):
            response = self.client.post(reverse("radar:create_relationship_job"), {
                "account_id": self.account.pk, "target": "seed",
                "mode": "followers", "max_pages": "1"})
        self.assertEqual(response.status_code, 302)
        job = RelationshipScanJob.objects.get(target="seed")
        self.assertEqual(job.target_pk, "100")
        self.assertEqual(job.seed_identity, self.seed)
