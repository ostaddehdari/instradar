from datetime import datetime, timezone as dt_timezone
from types import SimpleNamespace as Obj
from unittest.mock import AsyncMock, patch
import asyncio
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from radar.comment_store import normalize, save_page
from radar.comment_tasks import _commit_page, _complete, _fetch
from radar.models import (Comment, CommentSyncJob, Contact, InstagramAccount, Post,
                          MediaSyncJob, PostSnapshot, ReplySyncCursor)


class CommentStageTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("admin_stage3", password="example-test-password")
        self.other = get_user_model().objects.create_user("other_stage3", password="example-test-password")
        self.account = InstagramAccount.objects.create(owner=self.user, username="test", encrypted_cookies=b"encrypted")
        self.post = Post.objects.create(media_pk="123", shortcode="code123", owner_pk="456",
                                        owner_username="target", discovered_from="target", comment_count=2)
        media_job = MediaSyncJob.objects.create(owner=self.user, account=self.account, target="target")
        PostSnapshot.objects.create(job=media_job, post=self.post, caption_hash="")
        self.job = CommentSyncJob.objects.create(owner=self.user, account=self.account,
                                                 post=self.post, expected_count=2)

    def raw(self, pk, text, reply_count=0):
        return Obj(pk=pk, text=text, child_comment_count=reply_count,
                   created_at_utc=datetime(2026, 9, 1, tzinfo=dt_timezone.utc), like_count=3,
                   user=Obj(pk="90", username="author"))

    def test_page_commit_upsert_mentions_reply_cursor(self):
        comment = normalize(self.raw("c1", "Hi @friend and @friend", reply_count=1))
        _commit_page(self.job, None, [self.raw("c1", "Hi @friend and @friend", 1)], "next", 20)
        self.assertEqual(comment["author_username"], "author")
        self.assertEqual(Comment.objects.count(), 1)
        self.assertEqual(Comment.objects.get().author_identity.current_username, "author")
        self.assertTrue(Contact.objects.filter(
            owner=self.user, identity=Comment.objects.get().author_identity).exists())
        self.client.force_login(self.user)
        self.assertContains(self.client.get(reverse("radar:contact_list")), "@author")
        self.assertEqual(Comment.objects.get().mentions.count(), 1)
        self.assertEqual(ReplySyncCursor.objects.get(job=self.job).expected_count, 1)
        _commit_page(self.job, None, [self.raw("c1", "edited @second", 1)], "", 10)
        self.assertEqual(Comment.objects.count(), 1)
        self.assertEqual(set(Comment.objects.get().mentions.values_list("username", flat=True)), {"second"})
        reply = ReplySyncCursor.objects.select_related("parent").get(job=self.job)
        _commit_page(self.job, reply, [self.raw("c2", "reply")], "", 10)
        self.assertEqual(Comment.objects.get(instagram_pk="c2").parent_instagram_pk, "c1")
        self.assertTrue(_complete(self.job))
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, "done")

    def test_limited_comments_not_misreported_as_complete(self):
        _commit_page(self.job, None, [], "", 10)
        self.assertTrue(_complete(self.job))
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, "partial")
        self.assertIn("0", self.job.error)

    @override_settings(INSTRADAR_MIN_DELAY_MS=0, INSTRADAR_MAX_DELAY_MS=0,
                       INSTRADAR_COMMENT_REQUEST_TIMEOUT=5,
                       INSTRADAR_COMMENT_PAGE_SIZE=30)
    def test_private_false_empty_falls_back_to_captured_web_graphql(self):
        client = Obj(
            media_comments_chunk=AsyncMock(return_value=([], "")),
            media_comments_gql_chunk=AsyncMock(return_value=([{
                "id": "gql-1", "text": "web comment", "created_at": 1700000000,
                "owner": {"id": "901", "username": "web_author"},
                "edge_liked_by": {"count": 2},
                "edge_threaded_comments": {"count": 0, "edges": []},
            }], "next-gql")),
        )
        items, cursor, api, notice = asyncio.run(_fetch(
            client, self.post.media_pk, self.job.expected_count,
            self.job.top_cursor))
        self.assertEqual((len(items), cursor, api), (1, "next-gql", "gql"))
        self.assertIn("GraphQL", notice)

    @override_settings(INSTRADAR_MIN_DELAY_MS=0, INSTRADAR_MAX_DELAY_MS=0,
                       INSTRADAR_COMMENT_REQUEST_TIMEOUT=5,
                       INSTRADAR_COMMENT_PAGE_SIZE=30)
    def test_rate_limit_stops_before_fallback_route(self):
        client = Obj(
            media_comments_chunk=AsyncMock(
                side_effect=RuntimeError("PleaseWaitFewMinutes")),
            media_comments_gql_chunk=AsyncMock(return_value=([], "")),
        )
        with self.assertRaisesRegex(RuntimeError, "PleaseWaitFewMinutes"):
            asyncio.run(_fetch(client, self.post.media_pk,
                               self.job.expected_count, self.job.top_cursor))
        client.media_comments_gql_chunk.assert_not_awaited()

    def test_sql_report_comment_details(self):
        save_page(self.job, [normalize(self.raw("report1", "hello"))])
        with connection.cursor() as cursor:
            cursor.execute("SELECT comment_id, commenter_username, post_shortcode, post_url FROM comment_details")
            self.assertEqual(cursor.fetchone(), ("report1", "author", "code123",
                                              "https://www.instagram.com/p/code123/"))

    def test_page_failure_does_not_advance_cursor(self):
        with self.assertRaises(ValueError):
            _commit_page(self.job, None, [Obj(pk="")], "cursor", 2)
        self.job.refresh_from_db()
        self.assertEqual(self.job.top_cursor, "")

    def test_comment_archive_is_data_only_and_owner_scoped(self):
        save_page(self.job, [normalize(self.raw("archive1", "متن قابل جستجو"))])
        self.client.force_login(self.user)
        archive = self.client.get(reverse("radar:comment_list") + "?q=%D9%82%D8%A7%D8%A8%D9%84")
        self.assertContains(archive, "متن قابل جستجو")
        self.assertContains(archive, reverse("radar:observe"))
        self.client.force_login(self.other)
        self.assertNotContains(self.client.get(reverse("radar:comment_list")), "code123")
