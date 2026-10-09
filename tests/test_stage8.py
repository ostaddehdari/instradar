from datetime import datetime
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from radar.jalali import gregorian_to_jalali, parse_jalali
from radar.models import (InstagramAccount, MediaSyncJob, ObservationJob, Post,
                          PostAsset, PostSnapshot, PublishJob, SiteSettings,
                          TelegramAccount, TelegramDestination, TelegramSettings)
from radar.observation_tasks import orchestrate_observation


class StageEightTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("stage8", password="test-password")
        self.account = InstagramAccount.objects.create(
            owner=self.user, username="runner", encrypted_cookies=b"cookies", status="captured")
        self.client.force_login(self.user)

    @patch("radar.views.queue_observation")
    def test_unified_form_creates_parent_and_defers_children_to_orchestrator(self, queue_observation):
        response = self.client.post(reverse("radar:create_media_job"), {
            "account_id": self.account.pk, "target": "target", "mode": "full",
            "priority": "8", "options_present": "1", "include_posts": "on",
            "include_followers": "on", "include_comments": "on",
        })
        self.assertEqual(response.status_code, 302)
        job = ObservationJob.objects.get()
        self.assertTrue(job.include_posts)
        self.assertFalse(job.include_reels)
        self.assertEqual(job.priority, 8)
        self.assertFalse(MediaSyncJob.objects.filter(observation=job).exists())
        self.assertEqual(job.stage_order, ["posts", "followers", "comments"])
        queue_observation.assert_called_once()

    @patch("radar.views.queue_observation")
    def test_comments_can_be_the_only_user_selected_observation(self, queue_observation):
        response = self.client.post(reverse("radar:create_media_job"), {
            "account_id": self.account.pk,
            "target": "comments.only",
            "mode": "full",
            "priority": "5",
            "options_present": "1",
            "include_comments": "on",
        })

        job = ObservationJob.objects.get()
        self.assertRedirects(
            response, reverse("radar:observation_detail", args=[job.pk]),
            fetch_redirect_response=False)
        self.assertTrue(job.include_comments)
        self.assertFalse(job.include_posts)
        self.assertFalse(job.include_reels)
        self.assertEqual(job.stage_order, ["comments"])
        queue_observation.assert_called_once_with(job)

    @patch("radar.observation_tasks.queue_comments")
    @patch("radar.observation_tasks.queue_media")
    def test_comments_only_uses_already_extracted_target_posts(self, queue_media, queue_comments):
        archive = ObservationJob.objects.create(
            owner=self.user, account=self.account, target="half.page",
            include_posts=True, include_reels=False, stage_order=["posts"],
            status=ObservationJob.Status.DONE)
        media = MediaSyncJob.objects.create(
            observation=archive, owner=self.user, account=self.account,
            target="half.page", status=MediaSyncJob.Status.DONE)
        first = Post.objects.create(
            media_pk="810", shortcode="HALF810", owner_pk="81",
            owner_username="half.page", published_at=timezone.now())
        second = Post.objects.create(
            media_pk="811", shortcode="HALF811", owner_pk="81",
            owner_username="half.page", published_at=timezone.now())
        unrelated = Post.objects.create(
            media_pk="812", shortcode="OTHER812", owner_pk="82",
            owner_username="other.page", published_at=timezone.now())
        for post in (first, second, unrelated):
            PostSnapshot.objects.create(job=media, post=post, caption_hash="")
        job = ObservationJob.objects.create(
            owner=self.user, account=self.account, target="half.page",
            include_posts=False, include_reels=False, include_comments=True,
            stage_order=["comments"])

        with self.captureOnCommitCallbacks(execute=True):
            orchestrate_observation(str(job.pk))

        self.assertFalse(MediaSyncJob.objects.filter(observation=job).exists())
        self.assertEqual(
            set(job.comment_jobs.values_list("post_id", flat=True)),
            {first.pk, second.pk})
        queue_media.assert_not_called()
        queue_comments.assert_called_once()

    @patch("radar.views.queue_observation")
    def test_form_still_rejects_when_no_observation_part_is_selected(self, queue_observation):
        response = self.client.post(reverse("radar:create_media_job"), {
            "account_id": self.account.pk,
            "target": "nothing.selected",
            "mode": "full",
            "priority": "5",
            "options_present": "1",
        }, follow=True)

        self.assertContains(response, "حداقل یک بخش برای رصد انتخاب کنید.")
        self.assertFalse(ObservationJob.objects.exists())
        queue_observation.assert_not_called()

    def test_jalali_round_trip(self):
        self.assertEqual(gregorian_to_jalali(2026, 9, 16), (1405, 6, 25))
        self.assertEqual(parse_jalali("۱۴۰۵/۰۶/۲۵").isoformat(), "2026-09-16")

    def test_settings_centralizes_schedule_and_telegram_credentials(self):
        response = self.client.post(reverse("radar:save_system_settings"), {
            "max_concurrent_observations": "4", "enrichment_hour": "2",
            "enrichment_minute": "35", "enrichment_enabled": "on",
            "default_page_size": "80",
            "api_id": "12345", "api_hash": "a" * 32,
        })
        self.assertEqual(response.status_code, 302)
        config = SiteSettings.objects.get(owner=self.user)
        self.assertEqual((config.max_concurrent_observations, config.enrichment_hour,
                          config.enrichment_minute, config.default_page_size), (4, 2, 35, 80))
        self.assertTrue(TelegramSettings.objects.filter(owner=self.user, api_id=12345).exists())

    def test_archive_is_hidden_from_active_list(self):
        archived = ObservationJob.objects.create(
            owner=self.user, account=self.account, target="old", status="archived")
        active = ObservationJob.objects.create(owner=self.user, account=self.account, target="live")
        page = self.client.get(reverse("radar:observe"))
        self.assertContains(page, active.target)
        self.assertNotContains(page, f"@{archived.target}")
        archive_page = self.client.get(reverse("radar:observe") + "?archived=1")
        self.assertContains(archive_page, archived.target)

    def test_explore_and_instagram_profile_are_owner_scoped(self):
        observation = ObservationJob.objects.create(owner=self.user, account=self.account,
                                                     target="author")
        media = MediaSyncJob.objects.create(observation=observation, owner=self.user,
                                            account=self.account, target="author")
        post = Post.objects.create(media_pk="800", shortcode="STAGE8", owner_pk="9",
                                   owner_username="author", published_at=timezone.now())
        PostSnapshot.objects.create(job=media, post=post, caption_hash="")
        self.assertContains(self.client.get(reverse("radar:explore")), "author")
        self.assertContains(self.client.get(reverse("radar:instagram_profile",
                                                    args=["author"])),
                            reverse("radar:post_detail", args=[post.pk]))

    def test_account_delete_removes_session_but_keeps_history_identity(self):
        response = self.client.post(reverse("radar:delete_instagram_account",
                                            args=[self.account.pk]))
        self.assertEqual(response.status_code, 302)
        self.account.refresh_from_db()
        self.assertFalse(self.account.is_active)
        self.assertIsNone(self.account.encrypted_cookies)

    @patch("radar.observation_tasks.queue_media")
    def test_orchestrator_creates_media_first(self, queue_media):
        job = ObservationJob.objects.create(owner=self.user, account=self.account,
                                            target="orchestrated", include_posts=True)
        with self.captureOnCommitCallbacks(execute=True):
            orchestrate_observation(str(job.pk))
        job.refresh_from_db()
        self.assertEqual(job.current_stage, "posts")
        self.assertTrue(MediaSyncJob.objects.filter(observation=job).exists())
        queue_media.assert_called_once()

    def _visible_post(self):
        observation = ObservationJob.objects.create(
            owner=self.user, account=self.account, target="author")
        media = MediaSyncJob.objects.create(
            observation=observation, owner=self.user, account=self.account, target="author")
        post = Post.objects.create(
            media_pk="900", shortcode="INLINE900", owner_pk="90",
            owner_username="author", published_at=timezone.now())
        PostSnapshot.objects.create(job=media, post=post, caption_hash="")
        PostAsset.objects.create(
            post=post, position=1, media_type=2,
            preview_url="https://cdn.example/cover.jpg",
            video_url="https://cdn.example/movie.mp4")
        return post

    def _telegram(self):
        account = TelegramAccount.objects.create(
            owner=self.user, title="حساب اصلی", phone="+989120000000",
            status=TelegramAccount.Status.ACTIVE, is_active=True)
        destination = TelegramDestination.objects.create(
            account=account, peer_id=1234, access_hash=5678,
            title="کانال آزمون", kind=TelegramDestination.Kind.CHANNEL,
            is_admin=True, can_post=True)
        return account, destination

    def test_explore_plays_video_and_post_has_instant_actions(self):
        post = self._visible_post()
        explore = self.client.get(reverse("radar:explore"))
        detail = self.client.get(reverse("radar:post_detail", args=[post.pk]))
        self.assertContains(explore, "<video", html=False)
        self.assertContains(explore, "movie.mp4")
        self.assertNotContains(detail, "دانلود همین حالا")
        self.assertContains(detail, reverse("radar:instant_publish_post", args=[post.pk]))

    def test_destination_picker_and_transfer_creator_are_inside_observe(self):
        _, destination = self._telegram()
        page = self.client.get(reverse("radar:observe"))
        self.assertContains(page, destination.title)
        self.assertContains(page, reverse("radar:create_transfer_job"))

    @patch("radar.views.publish_next")
    def test_single_post_publish_is_invoked_synchronously(self, publish_next):
        post = self._visible_post()
        telegram, destination = self._telegram()
        response = self.client.post(reverse("radar:instant_publish_post", args=[post.pk]), {
            "instagram_account_id": self.account.pk,
            "telegram_account_id": telegram.pk,
            "telegram_destination_ids": [destination.pk],
        })
        self.assertEqual(response.status_code, 302)
        job = PublishJob.objects.get()
        publish_next.assert_called_once_with(str(job.pk))
        self.assertEqual(job.destination_links.get().destination, destination)
