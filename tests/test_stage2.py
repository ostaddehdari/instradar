import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from radar.media_store import parse_media, save_likers, save_media
from radar.media_tasks import _fetch_page, _finish, _persist_page, recover_media_sync
from radar.models import (InstagramAccount, InstagramIdentity, MediaSyncJob, ObservationJob,
                          Post, PostAsset, PostHashtag, PostMetricHistory, PostSnapshot,
                          PostLike, PostSyncCursor)


def media(pk="123", likes=5, caption="سلام #ایران @friend", resources=None):
    return SimpleNamespace(pk=pk, code="ABC"+pk, taken_at=timezone.now(), media_type=1,
        product_type="", user=SimpleNamespace(pk="999", username="author"), caption_text=caption,
        like_count=likes, view_count=None, play_count=None, comment_count=2,
        resources=resources or [], usertags=[], coauthor_producers=[], location=None,
        thumbnail_url="https://example.com/pic.jpg", video_url=None)


class StageTwoTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("st2", password="test-password-st2")
        self.account = InstagramAccount.objects.create(owner=self.user, username="logintest")
        self.job = MediaSyncJob.objects.create(owner=self.user, account=self.account, target="author",
            target_pk="999", status="running", expected_media_count=1)
        self.timeline = PostSyncCursor.objects.create(job=self.job, stream="timeline")
        self.reels = PostSyncCursor.objects.create(job=self.job, stream="reels")

    def test_parser_distinguishes_missing_views_and_tags(self):
        parsed = parse_media(media(), "author")
        self.assertIsNone(parsed["view_count"])
        self.assertEqual(parsed["hashtags"], {"ایران"})
        self.assertEqual(parsed["mentions"], {"friend"})

    def test_parser_preserves_real_zero_view_counter(self):
        item = media()
        item.view_count = 0
        item.video_view_count = 50
        item.play_count = 0
        item.video_play_count = 60
        parsed = parse_media(item, "author")
        self.assertEqual(parsed["view_count"], 0)
        self.assertEqual(parsed["play_count"], 0)

    def test_idempotence_history_and_carousel(self):
        carousel = [SimpleNamespace(media_type=1, thumbnail_url="https://example.com/1", video_url=None),
                    SimpleNamespace(media_type=2, thumbnail_url="", video_url="https://example.com/2")]
        post, created = save_media(self.job, media(resources=carousel), "author", detailed=True)
        self.assertTrue(created)
        self.assertEqual(post.owner_identity.current_username, "author")
        self.assertEqual(InstagramIdentity.objects.filter(instagram_pk="999").count(), 1)
        self.assertEqual(PostAsset.objects.filter(post=post).count(), 2)
        self.assertEqual(PostHashtag.objects.filter(post=post).count(), 1)
        save_media(self.job, media(resources=carousel), "author", detailed=True, stream="reels")
        self.assertEqual(Post.objects.count(), 1)
        self.assertEqual(PostSnapshot.objects.count(), 1)
        self.assertEqual(PostMetricHistory.objects.count(), 1)
        save_media(self.job, media(likes=9, resources=carousel), "author", detailed=True)
        self.assertEqual(PostMetricHistory.objects.get().like_count, 9)
        self.assertEqual(post.assets.count(), 2)
        self.assertEqual(Post.objects.get(pk=post.pk).view_count, None)

    def test_listing_does_not_erase_rich_detail(self):
        post, _ = save_media(self.job, media(), "author", detailed=True)
        save_media(self.job, media(caption="", likes=11), "author", detailed=False)
        post.refresh_from_db()
        self.assertEqual(post.caption, "سلام #ایران @friend")
        self.assertEqual(post.detail_status, "full")
        self.assertEqual(post.like_count, 11)
        self.assertEqual(PostHashtag.objects.filter(post=post).count(), 1)

    def test_cursors_checkpoint_and_recovery(self):
        _persist_page(self.job, self.timeline, [(media(), True)], "next-page", "mobile", False, 101)
        self.timeline.refresh_from_db()
        self.assertEqual((self.timeline.cursor, self.timeline.pages), ("next-page", 1))
        self.assertEqual(self.job.events.get().duration_ms, 101)
        self.job.lease_until = timezone.now() - timedelta(seconds=5)
        self.job.save(update_fields=["lease_until"])
        with patch("radar.media_tasks.queue_media") as enqueue:
            recover_media_sync()
        self.job.refresh_from_db()
        self.timeline.refresh_from_db()
        self.assertEqual(self.job.status, "queued")
        self.assertEqual(self.timeline.cursor, "next-page")
        enqueue.assert_called_once()

    def test_partial_does_not_pass_as_complete(self):
        self.job.expected_media_count = 3
        self.job.save(update_fields=["expected_media_count"])
        _persist_page(self.job, self.timeline, [(media(), True)], "", "mobile", False)
        _persist_page(self.job, self.reels, [], "", "mobile", False)
        self.assertTrue(_finish(self.job))
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, "partial")

    def test_date_range_is_not_reconciled_against_all_posts(self):
        self.job.date_from = timezone.now().date() - timedelta(days=1)
        self.job.expected_media_count = 100
        self.job.save(update_fields=["date_from", "expected_media_count"])
        _persist_page(self.job, self.timeline, [(media(), True)], "", "mobile", False)
        _persist_page(self.job, self.reels, [], "", "mobile", False)
        self.assertTrue(_finish(self.job))
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, "done")
        from radar.models import PostCrawlState
        self.assertFalse(PostCrawlState.objects.filter(account=self.account).exists())

    def test_private_empty_triggers_web_fallback(self):
        client = SimpleNamespace(user_medias_paginated_v1=AsyncMock(return_value=([], "")),
            user_medias_paginated_gql=AsyncMock(return_value=([media()], "")),
            media_info=AsyncMock(return_value=media()))
        with override_settings(INSTRADAR_MEDIA_PAGE_SIZE=5, INSTRADAR_MIN_DELAY_MS=1,
                               INSTRADAR_MAX_DELAY_MS=1), patch("radar.media_tasks.asyncio.sleep", new_callable=AsyncMock):
            found, cursor, api, incomplete = asyncio.run(_fetch_page(client, self.timeline, "999", 10))
        self.assertEqual((len(found), api, incomplete), (1, "gql", False))
        client.user_medias_paginated_gql.assert_awaited_once()

    def test_accessible_likers_are_saved_as_contacts_idempotently(self):
        post, _ = save_media(self.job, media(), "author", detailed=True)
        liker = SimpleNamespace(pk="777", username="liker", full_name="Liker",
                                is_private=False, is_verified=False, profile_pic_url="")
        self.assertEqual(save_likers(self.job, post, [liker]), 1)
        self.assertEqual(save_likers(self.job, post, [liker]), 1)
        self.assertEqual(PostLike.objects.filter(post=post).count(), 1)
        self.client.force_login(self.user)
        self.assertContains(self.client.get(reverse("radar:contact_list")), "@liker")

    def test_liker_api_is_optional_and_does_not_change_media_contract(self):
        client = SimpleNamespace(
            user_medias_paginated_v1=AsyncMock(return_value=([media()], "")),
            media_info=AsyncMock(return_value=media()),
            media_likers=AsyncMock(return_value=[SimpleNamespace(pk="777", username="liker")]),
        )
        with override_settings(INSTRADAR_MEDIA_PAGE_SIZE=5, INSTRADAR_MIN_DELAY_MS=0,
                               INSTRADAR_MAX_DELAY_MS=0):
            found, cursor, api, incomplete = asyncio.run(
                _fetch_page(client, self.timeline, "999", 1, include_likers=True))
        self.assertEqual(len(found[0][2]), 1)
        self.assertFalse(incomplete)
        client.media_likers.assert_awaited_once()

    def test_pages_and_ownership(self):
        self.client.force_login(self.user)
        for route in ("dashboard", "observe", "jobs", "post_list", "comment_list",
                      "search", "graph_builder"):
            self.assertEqual(self.client.get(reverse("radar:" + route)).status_code, 200)
        post, _ = save_media(self.job, media(), "author", detailed=True)
        self.assertEqual(self.client.get(reverse("radar:post_detail", args=[post.pk])).status_code, 200)
        self.assertContains(self.client.get(reverse("radar:search") + "?q=%23%D8%A7%DB%8C%D8%B1%D8%A7%D9%86"), "ABC123")
        other = get_user_model().objects.create_user("other-st2", password="other-password")
        self.client.force_login(other)
        self.assertEqual(self.client.get(reverse("radar:post_detail", args=[post.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse("radar:media_job_detail", args=[self.job.pk])).status_code, 404)

    def test_create_job_uses_connected_account_and_checkpoints(self):
        self.client.force_login(self.user)
        response = self.client.post(reverse("radar:create_media_job"), {"account_id": self.account.pk,
            "target": "newtarget", "mode": "full", "max_pages": "1"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(MediaSyncJob.objects.count(), 1)  # no unverified cookie jar
        self.account.encrypted_cookies = b"encrypted_test_bytes"
        self.account.save(update_fields=["encrypted_cookies"])
        self.job.status = "done"
        self.job.save(update_fields=["status"])
        with patch("radar.views.queue_observation") as enqueue:
            response = self.client.post(reverse("radar:create_media_job"), {"account_id": self.account.pk,
                "target": "newtarget", "mode": "full", "max_pages": "1"})
        self.assertEqual(response.status_code, 302)
        observation = ObservationJob.objects.get(target="newtarget")
        self.assertEqual(observation.max_pages, 1)
        self.assertFalse(MediaSyncJob.objects.filter(observation=observation).exists())
        enqueue.assert_called_once()

    def test_partial_missing_details_replays_incomplete_page(self):
        self.job.status = "partial"
        self.job.save(update_fields=["status"])
        self.timeline.cursor = "page-2"
        self.timeline.issue = "برخی جزئیات دریافت نشد؛ فهرست اولیه ذخیره شد"
        self.timeline.finished = True
        self.timeline.save()
        self.reels.finished = True
        self.reels.save()
        self.client.force_login(self.user)
        with patch("radar.views.queue_media"):
            self.client.post(reverse("radar:resume_media_job", args=[self.job.pk]))
        self.timeline.refresh_from_db()
        self.assertEqual(self.timeline.cursor, "")
        self.assertFalse(self.timeline.finished)
