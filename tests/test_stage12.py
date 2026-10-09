import io
import uuid
import zipfile
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from radar.models import (
    AccountRelationship, Comment, CommentSyncJob, CrawlSession,
    InstagramAccount, InstagramIdentity, MediaSyncJob, ObservationJob, Post,
    PostHashtag, PostMention, PostSnapshot, PostSyncCursor,
    RelationshipScanJob, RelationshipSnapshot, RelationshipSyncState,
)
from radar.observation_progress import decorate_observation
from radar.observation_tasks import orchestrate_observation


class StageTwelveWorkflowTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("stage12", password="safe-password")
        self.account = InstagramAccount.objects.create(
            owner=self.user, username="collector", encrypted_cookies=b"cookies",
            status="active")
        self.client.force_login(self.user)

    def observation(self, **overrides):
        values = {
            "owner": self.user, "account": self.account, "target": "origin",
            "include_posts": True, "include_reels": False,
            "include_followers": True, "include_following": False,
            "include_comments": True,
            "stage_order": ["posts", "followers", "comments"],
        }
        values.update(overrides)
        return ObservationJob.objects.create(**values)

    def test_observe_forms_are_collapsed_and_recent_table_has_real_progress(self):
        self.observation()
        response = self.client.get(reverse("radar:observe"))
        self.assertContains(response, "رصد جدید")
        self.assertContains(response, "انتقال جدید")
        self.assertContains(response, 'id="new-observation-panel" class="collapse', html=False)
        self.assertContains(response, "درصد پیشرفت")

    def test_stream_counters_drive_progress(self):
        job = self.observation(include_comments=False, stage_order=["posts", "followers"])
        CrawlSession.objects.create(
            observation=job, account=self.account, target=job.target,
            profile_posts_count=200, profile_followers_count=1000)
        media = MediaSyncJob.objects.create(
            observation=job, owner=self.user, account=self.account, target=job.target)
        PostSyncCursor.objects.create(job=media, stream="timeline", seen=100, finished=True)
        PostSyncCursor.objects.create(job=media, stream="reels", finished=True)
        seed = InstagramIdentity.objects.create(instagram_pk="100", current_username="origin")
        relation = RelationshipScanJob.objects.create(
            observation=job, owner=self.user, account=self.account, target=job.target,
            seed_identity=seed, expected_followers=1000, mode="followers")
        RelationshipSyncState.objects.create(job=relation, stream="followers", seen=250)
        decorate_observation(job)
        self.assertEqual([row.percent for row in job.subtasks], [50, 25])
        self.assertEqual(job.progress_percent, 29)

    @patch("radar.observation_tasks.queue_relationships")
    def test_following_can_preempt_followers_and_media(self, queue_relationships):
        job = self.observation(
            include_followers=True, include_following=True, include_comments=False,
            stage_order=["following", "followers", "posts"])
        with self.captureOnCommitCallbacks(execute=True):
            orchestrate_observation(str(job.pk))
        relation = RelationshipScanJob.objects.get(observation=job)
        queue_relationships.assert_called_once_with(relation, stream="following")
        job.refresh_from_db()
        self.assertEqual(job.current_stage, "following")

    @patch("radar.views.queue_observation")
    def test_reorder_endpoint_keeps_stream_level_order(self, queue_observation):
        job = self.observation()
        response = self.client.post(reverse("radar:reorder_job_stages", args=[job.pk]), {
            "stages[]": ["followers", "posts", "comments"]})
        self.assertEqual(response.status_code, 200)
        job.refresh_from_db()
        self.assertEqual(job.stage_order, ["followers", "posts", "comments"])

    def _media_data(self):
        job = self.observation(include_followers=False, include_comments=False,
                               stage_order=["posts"])
        media = MediaSyncJob.objects.create(
            observation=job, owner=self.user, account=self.account, target=job.target)
        first = Post.objects.create(
            media_pk="501", shortcode="POST501", owner_pk="100",
            owner_username="origin", caption="آموزش حرفه ای #دانش @friend",
            like_count=44, view_count=800, comment_count=3,
            published_at=timezone.now())
        second = Post.objects.create(
            media_pk="502", shortcode="POST502", owner_pk="200",
            owner_username="other", caption="متن دیگر", like_count=1,
            play_count=1200,
            published_at=timezone.now())
        PostSnapshot.objects.create(job=media, post=first, caption_hash="a")
        PostSnapshot.objects.create(job=media, post=second, caption_hash="b")
        PostHashtag.objects.create(post=first, tag="دانش")
        PostMention.objects.create(post=first, username="friend")
        return first, second

    def test_post_filters_and_xlsx_cover_all_filtered_rows(self):
        first, second = self._media_data()
        response = self.client.get(reverse("radar:post_list"), {
            "profiles": ["origin"], "q": "دانش", "min_likes": "10"})
        self.assertContains(response, first.shortcode)
        self.assertNotContains(response, second.shortcode)
        export = self.client.get(reverse("radar:export_posts_xlsx"), {
            "profiles": ["origin"], "q": "دانش"})
        payload = b"".join(export.streaming_content)
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            sheet = archive.read("xl/worksheets/sheet1.xml").decode()
            workbook = archive.read("xl/workbook.xml").decode()
        self.assertIn("پست‌ها و ریلزها", workbook)
        self.assertIn("POST501", sheet)
        self.assertNotIn("POST502", sheet)

        play_only = self.client.get(reverse("radar:post_list"), {"min_views": "1000"})
        self.assertContains(play_only, second.shortcode)
        self.assertNotContains(play_only, first.shortcode)

    def test_comment_avatar_and_excel(self):
        post, _ = self._media_data()
        identity = InstagramIdentity.objects.create(
            instagram_pk="900", current_username="commenter",
            profile_pic_url="https://cdn.example/avatar.jpg")
        comment = Comment.objects.create(
            post=post, instagram_pk="990", author_pk="900",
            author_username="commenter", author_identity=identity,
            text="دیدگاه فارسی", like_count=9, published_at=timezone.now())
        response = self.client.get(reverse("radar:comment_list"), {"q": "فارسی"})
        self.assertContains(response, "avatar.jpg")
        export = self.client.get(reverse("radar:export_comments_xlsx"), {"q": "فارسی"})
        payload = b"".join(export.streaming_content)
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            sheet = archive.read("xl/worksheets/sheet1.xml").decode()
        self.assertIn(comment.text, sheet)

    def test_audience_page_distinguishes_follower_and_following(self):
        seed = InstagramIdentity.objects.create(instagram_pk="301", current_username="origin")
        follower = InstagramIdentity.objects.create(instagram_pk="302", current_username="fan")
        following = InstagramIdentity.objects.create(instagram_pk="303", current_username="idol")
        job = RelationshipScanJob.objects.create(
            owner=self.user, account=self.account, target="origin", seed_identity=seed)
        follower_edge = AccountRelationship.objects.create(source=follower, target=seed)
        following_edge = AccountRelationship.objects.create(source=seed, target=following)
        RelationshipSnapshot.objects.create(
            job=job, relationship=follower_edge, source=follower, target=seed)
        RelationshipSnapshot.objects.create(
            job=job, relationship=following_edge, source=seed, target=following)
        response = self.client.get(reverse("radar:audience_relations"))
        self.assertContains(response, "@fan")
        self.assertContains(response, "@idol")
        self.assertContains(response, "فالور")
        self.assertContains(response, "فالوینگ")
        export = self.client.get(reverse("radar:export_audience_relations_xlsx"))
        self.assertEqual(export.status_code, 200)

    def test_old_standalone_log_page_is_removed(self):
        response = self.client.get(f"/instradar/logs/{uuid.uuid4()}/")
        self.assertEqual(response.status_code, 404)

    def test_post_detail_separates_caption_hashtags_mentions_and_hides_telegram(self):
        post, _ = self._media_data()
        response = self.client.get(reverse("radar:post_detail", args=[post.pk]))
        self.assertContains(response, "مشخصات محتوا")
        self.assertContains(response, ">کپشن<", html=False)
        self.assertContains(response, ">هشتگ‌ها<", html=False)
        self.assertContains(response, ">منشن‌ها<", html=False)
        self.assertContains(response, 'id="instant-telegram-card" class="collapse', html=False)
