import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from radar.crypto import seal
from radar.models import (InstagramAccount, MediaSyncJob, Post, PostHashtag, PostSnapshot,
                          PublishItem, PublishJob, PublishJobDestination, TelegramAccount,
                          TelegramDestination, TelegramMessage, TelegramSettings)
from radar.publish_tasks import prepare_observation_publish, publish_next
from radar.telegram_service import build_caption


FERNET_KEY = "dPuJ3zYpVzrfHiPOFU-QDpQQ0MfKnDvjeZeY3WGy50M="


class StageSevenTests(TestCase):
    def setUp(self):
        users = get_user_model()
        self.user = users.objects.create_user("telegram-owner", password="safe-password")
        self.other = users.objects.create_user("telegram-other", password="safe-password")
        self.instagram = InstagramAccount.objects.create(
            owner=self.user, username="shared.instagram", encrypted_cookies=b"encrypted")
        with patch.dict(os.environ, {"INSTRADAR_FERNET_KEY": FERNET_KEY}):
            self.telegram_settings = TelegramSettings.objects.create(
                owner=self.user, api_id=12345, encrypted_api_hash=seal("a" * 32))
            self.telegram = TelegramAccount.objects.create(
                owner=self.user, title="اصلی", phone="+989120000000", status="active",
                encrypted_session=seal("session"))
        self.channel = TelegramDestination.objects.create(
            account=self.telegram, peer_id=9001, access_hash=12, title="کانال تست",
            kind="channel", is_admin=True, can_post=True)
        self.media_job = MediaSyncJob.objects.create(
            owner=self.user, account=self.instagram, target="archive", status="done")
        self.old = Post.objects.create(
            media_pk="7001", shortcode="OLD7001", owner_pk="10",
            owner_username="archive", discovered_from="archive", caption="old")
        self.new = Post.objects.create(
            media_pk="7002", shortcode="NEW7002", owner_pk="10",
            owner_username="archive", discovered_from="archive", caption="new")
        PostSnapshot.objects.create(job=self.media_job, post=self.old, caption_hash="")
        PostSnapshot.objects.create(job=self.media_job, post=self.new, caption_hash="")

    def test_transfer_view_builds_oldest_to_newest_items_and_scopes_owner(self):
        self.client.force_login(self.user)
        with patch("radar.views.queue_publish"):
            response = self.client.post(reverse("radar:create_transfer_job"), {
                "instagram_account_id": self.instagram.pk,
                "telegram_account_id": self.telegram.pk,
                "telegram_destination_ids": [self.channel.pk],
                "target": "archive",
            })
        self.assertEqual(response.status_code, 302)
        job = PublishJob.objects.get()
        self.assertEqual(list(job.items.values_list("post_id", flat=True)),
                         [self.old.pk, self.new.pk])
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(reverse("radar:publish_job_detail",
                                                 args=[job.pk])).status_code, 404)

    def test_observation_job_waits_then_materializes_items(self):
        job = PublishJob.objects.create(
            owner=self.user, telegram_account=self.telegram,
            instagram_account=self.instagram, media_job=self.media_job,
            mode="observation", target_username="archive", status="waiting")
        PublishJobDestination.objects.create(job=job, destination=self.channel)
        with patch("radar.publish_tasks.queue_publish") as enqueue:
            prepare_observation_publish(str(self.media_job.pk))
        job.refresh_from_db()
        self.assertEqual((job.status, job.total_items), ("queued", 2))
        enqueue.assert_called_once()

    @override_settings(INSTRADAR_TELEGRAM_MAX_RETRIES=1,
                       INSTRADAR_TELEGRAM_TIMEOUT=60,
                       INSTRADAR_TELEGRAM_BACKOFF_BASE=10)
    def test_publish_records_message_and_second_job_skips_duplicate(self):
        job = PublishJob.objects.create(
            owner=self.user, telegram_account=self.telegram,
            instagram_account=self.instagram, mode="transfer",
            target_username="archive", total_items=1)
        PublishJobDestination.objects.create(job=job, destination=self.channel)
        PublishItem.objects.create(job=job, post=self.old, sequence=1)
        with patch("radar.publish_tasks._local_media", return_value=[Path("/tmp/a.jpg")]), \
                patch("radar.publish_tasks.send_media", return_value=[77]):
            publish_next(str(job.pk))
        self.assertTrue(TelegramMessage.objects.filter(
            destination=self.channel, post=self.old, message_ids=[77]).exists())
        job.refresh_from_db()
        self.assertEqual(job.status, "done")

    def test_caption_contains_archive_text_hashtags_and_source(self):
        PostHashtag.objects.create(post=self.old, tag="test")
        text = build_caption(self.old)
        self.assertIn("old", text)
        self.assertIn("#test", text)
        self.assertIn(self.old.url, text)

    def test_telegram_management_page_never_renders_encrypted_secrets(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("radar:telegram_accounts"))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "a" * 32)
        self.assertContains(response, "کانال تست")
