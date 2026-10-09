from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from radar.account_pool import (
    acquire_comment_account, acquire_operation_account, dispatch_comment_jobs, record_account_failure,
    release_comment_account, release_operation_lease,
)
from radar.models import (
    CommentSyncJob, InstagramAccount, InstagramAccountHealthEvent,
    InstagramAccountLease, InstagramAccountPool, InstagramAccountPoolMember,
    MediaSyncJob, ObservationJob, Post, PostCommentSyncState, PostSnapshot,
)
from radar.observation_tasks import _create_comments, orchestrate_observation


class AccountPoolSafetyTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            "stage13", password="safe-test-password")
        self.accounts = [InstagramAccount.objects.create(
            owner=self.user, username=f"collector{index}",
            encrypted_cookies=b"cookies", status="active",
            min_interval_seconds=3,
        ) for index in range(3)]
        self.pool = InstagramAccountPool.objects.create(
            owner=self.user, name="safe-team", max_concurrency=2,
            hourly_request_limit=100, daily_request_limit=500)
        InstagramAccountPoolMember.objects.bulk_create([
            InstagramAccountPoolMember(pool=self.pool, account=account)
            for account in self.accounts
        ])
        self.client.force_login(self.user)

    def make_posts(self, count=6):
        archive = MediaSyncJob.objects.create(
            owner=self.user, account=self.accounts[0], target="target")
        rows = []
        for index in range(count):
            post = Post.objects.create(
                media_pk=f"13{index}", shortcode=f"SAFE13{index}",
                owner_pk="130", owner_username="target", comment_count=index + 1,
                published_at=timezone.now())
            PostSnapshot.objects.create(job=archive, post=post, caption_hash="")
            rows.append(post)
        return archive, rows

    @patch("radar.views.queue_observation")
    def test_observation_form_persists_owned_pool_and_safe_concurrency(self, queue):
        response = self.client.post(reverse("radar:create_media_job"), {
            "account_id": self.accounts[0].pk,
            "account_pool_id": self.pool.pk,
            "max_active_accounts": "2",
            "target": "target", "mode": "full", "priority": "5",
            "options_present": "1", "include_comments": "on",
        })
        self.assertEqual(response.status_code, 302)
        observation = ObservationJob.objects.get()
        self.assertEqual(observation.account_pool, self.pool)
        self.assertEqual(observation.max_active_accounts, 2)
        queue.assert_called_once_with(observation)

    def test_pool_admin_is_owner_scoped_and_members_are_saved(self):
        response = self.client.post(reverse("radar:create_account_pool"), {
            "name": "second", "max_concurrency": "2",
            "hourly_request_limit": "120", "daily_request_limit": "800",
            "account_ids": [str(item.pk) for item in self.accounts[:2]],
        })
        self.assertEqual(response.status_code, 302)
        created = InstagramAccountPool.objects.get(name="second")
        self.assertEqual(created.members.count(), 2)
        self.assertContains(self.client.get(reverse("radar:accounts")), "second")

    def test_bulk_account_registration_deduplicates_usernames(self):
        response = self.client.post(reverse("radar:accounts"), {
            "bulk_usernames": "new.one\n@new.two, new.one",
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(InstagramAccount.objects.filter(
            owner=self.user, username__in=("new.one", "new.two")).count(), 2)

    @patch("radar.observation_tasks.queue_comments")
    def test_posts_are_sharded_and_only_pool_concurrency_is_dispatched(self, queue):
        _, posts = self.make_posts()
        observation = ObservationJob.objects.create(
            owner=self.user, account=self.accounts[0], account_pool=self.pool,
            max_active_accounts=2, target="target", include_posts=False,
            include_reels=False, include_comments=True, stage_order=["comments"])
        with self.captureOnCommitCallbacks(execute=True):
            orchestrate_observation(str(observation.pk))
        jobs = list(observation.comment_jobs.order_by("created_at", "pk"))
        self.assertEqual(len(jobs), len(posts))
        self.assertEqual({item.account_id for item in jobs},
                         {item.pk for item in self.accounts})
        self.assertEqual(sum(item.scheduled_at is not None for item in jobs), 2)
        self.assertEqual(queue.call_count, 2)

        with self.captureOnCommitCallbacks(execute=True):
            dispatch_comment_jobs(observation, enqueue=queue)
        self.assertEqual(queue.call_count, 2)

    def test_account_has_exclusive_lease_and_hourly_budget(self):
        post = self.make_posts(2)[1]
        first = CommentSyncJob.objects.create(
            owner=self.user, account=self.accounts[0], post=post[0])
        second = CommentSyncJob.objects.create(
            owner=self.user, account=self.accounts[0], post=post[1])
        self.accounts[0].hourly_request_limit = 10
        self.accounts[0].save(update_fields=["hourly_request_limit"])

        lease, unavailable = acquire_comment_account(first)
        self.assertIsNotNone(lease)
        self.assertIsNone(unavailable)
        self.assertTrue(InstagramAccountLease.objects.filter(account=self.accounts[0]).exists())
        _, operation_unavailable = acquire_operation_account(
            self.accounts[0].pk, None, "media", 60, cost=1)
        self.assertIsNotNone(operation_unavailable)
        self.assertEqual(InstagramAccountLease.objects.filter(
            account=self.accounts[0]).count(), 1)
        _, unavailable = acquire_comment_account(second)
        self.assertIsNotNone(unavailable)

        release_comment_account(first)
        InstagramAccount.objects.filter(pk=self.accounts[0].pk).update(
            last_request_at=timezone.now() - timedelta(minutes=5),
            hourly_request_limit=2, requests_this_hour=2)
        _, unavailable = acquire_comment_account(second)
        self.assertIsNotNone(unavailable)
        self.assertIn("بودجه", unavailable.reason)

    def test_pool_rotates_read_only_operations_across_healthy_sessions(self):
        observation = ObservationJob.objects.create(
            owner=self.user, account=self.accounts[0], account_pool=self.pool,
            target="target", include_posts=True)
        first, unavailable = acquire_operation_account(
            self.accounts[0].pk, observation.pk, "media", 60, cost=1)
        self.assertIsNotNone(first)
        self.assertIsNone(unavailable)
        release_operation_lease(first)
        # The first account was just used; the next page should prefer the
        # least-recently-used healthy member rather than pinning the job.
        second, unavailable = acquire_operation_account(
            self.accounts[0].pk, observation.pk, "media", 60, cost=1)
        self.assertIsNotNone(second)
        self.assertIsNone(unavailable)
        self.assertEqual(second.account_id, self.accounts[1].pk)
        second.delete()

    @override_settings(INSTRADAR_FEEDBACK_COOLDOWN_SECONDS=3600)
    def test_feedback_cools_down_and_challenge_quarantines(self):
        feedback = RuntimeError("FeedbackRequired")
        category = record_account_failure(
            self.accounts[0].pk, None, feedback, "feedback")
        self.accounts[0].refresh_from_db()
        self.assertEqual(category, "feedback_cooldown")
        self.assertEqual(self.accounts[0].health_status, "cooldown")
        self.assertIsNotNone(self.accounts[0].cooldown_until)

        challenge = RuntimeError("ChallengeRequired")
        category = record_account_failure(
            self.accounts[1].pk, None, challenge, "challenge")
        self.accounts[1].refresh_from_db()
        self.assertEqual(category, "quarantined")
        self.assertEqual(self.accounts[1].health_status, "quarantined")
        self.assertEqual(self.accounts[1].status, "needs_login")
        self.assertTrue(InstagramAccountHealthEvent.objects.filter(
            account=self.accounts[1], kind="quarantined").exists())

    @override_settings(INSTRADAR_CIRCUIT_BREAKER_EVENTS=3,
                       INSTRADAR_CIRCUIT_BREAKER_WINDOW_SECONDS=900)
    def test_three_pool_safety_signals_pause_all_active_pool_observations(self):
        first = ObservationJob.objects.create(
            owner=self.user, account=self.accounts[0], account_pool=self.pool,
            target="first", status=ObservationJob.Status.RUNNING)
        second = ObservationJob.objects.create(
            owner=self.user, account=self.accounts[1], account_pool=self.pool,
            target="second", status=ObservationJob.Status.QUEUED)
        for account in self.accounts:
            record_account_failure(
                account.pk, first.pk, RuntimeError("PleaseWaitFewMinutes"),
                "rate limited")
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.status, ObservationJob.Status.PAUSED)
        self.assertEqual(second.status, ObservationJob.Status.PAUSED)
        self.assertIn("Circuit Breaker", first.error)

    @override_settings(INSTRADAR_COMMENT_INCREMENTAL_REFRESH_HOURS=168)
    def test_incremental_comments_skip_recent_unchanged_posts(self):
        media, posts = self.make_posts(1)
        PostCommentSyncState.objects.create(
            owner=self.user, post=posts[0], last_account=self.accounts[0],
            last_observed_count=posts[0].comment_count, last_status="done",
            last_scan_at=timezone.now(), last_complete_at=timezone.now())
        observation = ObservationJob.objects.create(
            owner=self.user, account=self.accounts[0], account_pool=self.pool,
            target="target", mode="incremental", include_posts=True,
            include_reels=False, include_comments=True)
        media.observation = observation
        media.save(update_fields=["observation"])

        created, source_count, skipped = _create_comments(observation, media)
        self.assertEqual((len(created), source_count, skipped), (0, 1, 1))
        observation.refresh_from_db()
        self.assertEqual(observation.comments_skipped_count, 1)
