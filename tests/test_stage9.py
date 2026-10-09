import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from radar.models import (InstagramAccount, MediaSyncJob, ObservationJob,
                          RelationshipScanJob, RelationshipSyncState)
from radar.observation_tasks import orchestrate_observation
from radar.views import _decorate_observation_progress


class UnifiedJobCenterTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            "stage9", password="safe-password")
        self.account = InstagramAccount.objects.create(
            owner=self.user, username="runner", encrypted_cookies=b"cookies",
            status="captured")
        self.client.force_login(self.user)

    def test_drag_order_updates_queue_priority(self):
        first = ObservationJob.objects.create(
            owner=self.user, account=self.account, target="first")
        second = ObservationJob.objects.create(
            owner=self.user, account=self.account, target="second")
        response = self.client.post(reverse("radar:reorder_jobs"), {
            "job_ids[]": [str(second.pk), str(first.pk)]})
        self.assertEqual(response.status_code, 200)
        first.refresh_from_db(); second.refresh_from_db()
        self.assertGreater(second.priority, first.priority)

    def test_stage_order_is_persisted_at_stream_level(self):
        job = ObservationJob.objects.create(
            owner=self.user, account=self.account, target="target",
            include_posts=True, include_followers=True, include_comments=True)
        response = self.client.post(
            reverse("radar:reorder_job_stages", args=[job.pk]),
            {"stages[]": ["relationships", "comments", "media"]})
        self.assertEqual(response.status_code, 200)
        job.refresh_from_db()
        self.assertEqual(job.stage_order[:3], ["followers", "comments", "posts"])

    @patch("radar.observation_tasks.queue_relationships")
    def test_worker_honors_relationship_stage_before_independent_media(self, queue_relationships):
        job = ObservationJob.objects.create(
            owner=self.user, account=self.account, target="target",
            include_posts=True, include_followers=True,
            stage_order=["relationships", "media"])
        with self.captureOnCommitCallbacks(execute=True):
            orchestrate_observation(str(job.pk))
        job.refresh_from_db()
        self.assertEqual(job.current_stage, "followers")
        self.assertTrue(RelationshipScanJob.objects.filter(observation=job).exists())
        self.assertFalse(MediaSyncJob.objects.filter(observation=job).exists())
        queue_relationships.assert_called_once()

    def test_jobs_page_exposes_progress_and_stage_sort_endpoints(self):
        job = ObservationJob.objects.create(
            owner=self.user, account=self.account, target="target",
            include_posts=True, include_followers=True,
            stage_order=["media", "relationships"])
        response = self.client.get(reverse("radar:jobs"))
        self.assertContains(response, 'class="progress job-progress"')
        self.assertContains(response, reverse("radar:reorder_job_stages", args=[job.pk]))
        self.assertContains(response, "همه فالورها")

    @patch("radar.observation_tasks.queue_relationships")
    def test_relationship_only_observation_starts_relationship_stage(self, queue_relationships):
        job = ObservationJob.objects.create(
            owner=self.user, account=self.account, target="target",
            include_posts=False, include_reels=False,
            include_followers=True, include_following=True,
            stage_order=["relationships"])
        with self.captureOnCommitCallbacks(execute=True):
            orchestrate_observation(str(job.pk))
        job.refresh_from_db()
        self.assertEqual(job.status, "running")
        self.assertEqual(job.current_stage, "followers")
        relation = RelationshipScanJob.objects.get(observation=job)
        self.assertEqual(relation.status, "queued")
        queue_relationships.assert_called_once_with(relation)

    def test_relationship_only_progress_reflects_stream_state(self):
        job = ObservationJob.objects.create(
            owner=self.user, account=self.account, target="target",
            include_posts=False, include_reels=False,
            include_followers=True, include_following=True,
            current_stage="relationships", status="running",
            stage_order=["relationships"])
        relation = RelationshipScanJob.objects.create(
            observation=job, owner=self.user, account=self.account,
            target="target", mode="both", status="running")
        RelationshipSyncState.objects.create(job=relation, stream="followers")
        RelationshipSyncState.objects.create(job=relation, stream="following")
        _decorate_observation_progress(job)
        self.assertEqual(job.progress_percent, 0)
        self.assertEqual(job.relationship_provider, "aiograpi")

    def test_relationship_progress_reports_failing_stream_provider(self):
        job = ObservationJob.objects.create(
            owner=self.user, account=self.account, target="target",
            include_posts=False, include_reels=False,
            include_followers=True, include_following=True,
            current_stage="relationships", status="partial",
            stage_order=["relationships"])
        relation = RelationshipScanJob.objects.create(
            observation=job, owner=self.user, account=self.account,
            target="target", mode="both", status="partial")
        RelationshipSyncState.objects.create(
            job=relation, stream="following", api="private")
        RelationshipSyncState.objects.create(
            job=relation, stream="followers", api="gql",
            issue="پاسخ API خالی بود؛ رصد رابطه‌ها ناقص است")
        _decorate_observation_progress(job)
        self.assertEqual(job.relationship_provider, "graphql")
        self.assertIn("خالی", job.relationship_issue)
