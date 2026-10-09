import uuid
from django.conf import settings
from django.db import models
from django.utils import timezone


class InstagramAccount(models.Model):
    class Health(models.TextChoices):
        READY = "ready", "آماده"
        COOLDOWN = "cooldown", "در حال استراحت"
        QUARANTINED = "quarantined", "قرنطینه"

    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    username = models.CharField(max_length=100, blank=True)
    profile_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    instagram_pk = models.CharField(max_length=64, blank=True)
    encrypted_cookies = models.BinaryField(blank=True, null=True)
    encrypted_aiograpi_settings = models.BinaryField(blank=True, null=True)
    browser_user_agent = models.CharField(max_length=500, blank=True)
    status = models.CharField(max_length=20, default="new")
    last_error = models.TextField(blank=True)
    verified_at = models.DateTimeField(null=True, blank=True)
    browser_captured_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    is_active = models.BooleanField(default=True, db_index=True)
    health_status = models.CharField(
        max_length=16, choices=Health.choices, default=Health.READY, db_index=True)
    health_score = models.PositiveSmallIntegerField(default=100)
    cooldown_until = models.DateTimeField(null=True, blank=True, db_index=True)
    consecutive_failures = models.PositiveSmallIntegerField(default=0)
    last_request_at = models.DateTimeField(null=True, blank=True)
    request_window_started_at = models.DateTimeField(null=True, blank=True)
    requests_this_hour = models.PositiveIntegerField(default=0)
    request_day = models.DateField(null=True, blank=True)
    requests_today = models.PositiveIntegerField(default=0)
    hourly_request_limit = models.PositiveIntegerField(default=60)
    daily_request_limit = models.PositiveIntegerField(default=300)
    min_interval_seconds = models.PositiveSmallIntegerField(default=10)

    def __str__(self):
        return self.username or str(self.profile_id)

    @property
    def status_fa(self):
        return {
            "new": "در انتظار ورود",
            "captured": "نشست مرورگر دریافت شد",
            "active": "اتصال aiograpi تأیید شد",
            "needs_login": "نیازمند ورود دوباره",
            "error": "خطای اتصال",
        }.get(self.status, "وضعیت نامشخص")

    @property
    def status_css(self):
        return {"active": "success", "captured": "info", "needs_login": "danger",
                "error": "danger", "new": "neutral"}.get(self.status, "neutral")

    @property
    def status_help_fa(self):
        return {
            "new": "Chrome را باز کنید، در Instagram وارد شوید و سپس دریافت نشست را بزنید.",
            "captured": "کوکی‌ها امن ذخیره شده‌اند؛ با اولین رصد، اعتبار نشست در aiograpi بررسی می‌شود.",
            "active": "همان نشست رمزگذاری‌شده برای رصد aiograpi و فایل موقت gallery-dl آماده است.",
            "needs_login": "نشست توسط Instagram رد یا منقضی شده است؛ ورود noVNC را تکرار کنید.",
            "error": "اتصال کامل نشده است؛ گزارش جاب و لاگ سرویس را بررسی کنید.",
        }.get(self.status, "وضعیت این اتصال هنوز شناخته نشده است.")

    @property
    def health_fa(self):
        if self.health_status == self.Health.QUARANTINED:
            return "قرنطینه؛ بررسی دستی لازم است"
        if self.health_status == self.Health.COOLDOWN:
            return "استراحت تا پایان Cooldown"
        if self.health_score <= 80:
            return "آماده با سرعت بازیابی"
        return "آمادهٔ زمان‌بندی"


class InstagramAccountPool(models.Model):
    """Owner-scoped scheduling pool with a deliberately bounded global budget."""
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, related_name="instagram_account_pools",
        on_delete=models.CASCADE)
    name = models.CharField(max_length=100)
    is_active = models.BooleanField(default=True, db_index=True)
    max_concurrency = models.PositiveSmallIntegerField(default=2)
    hourly_request_limit = models.PositiveIntegerField(default=300)
    daily_request_limit = models.PositiveIntegerField(default=2000)
    request_window_started_at = models.DateTimeField(null=True, blank=True)
    requests_this_hour = models.PositiveIntegerField(default=0)
    request_day = models.DateField(null=True, blank=True)
    requests_today = models.PositiveIntegerField(default=0)
    last_request_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(
            fields=["owner", "name"], name="unique_owner_instagram_pool")]
        ordering = ["name", "id"]

    def __str__(self):
        return self.name


class InstagramAccountPoolMember(models.Model):
    pool = models.ForeignKey(
        InstagramAccountPool, related_name="members", on_delete=models.CASCADE)
    account = models.ForeignKey(
        InstagramAccount, related_name="pool_memberships", on_delete=models.CASCADE)
    is_active = models.BooleanField(default=True, db_index=True)
    allow_comments = models.BooleanField(default=True)
    last_used_at = models.DateTimeField(null=True, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(
            fields=["pool", "account"], name="unique_instagram_pool_member")]

    def __str__(self):
        return f"{self.pool}: @{self.account.username}"


class CrawlJob(models.Model):
    class Status(models.TextChoices):
        QUEUED = "queued", "در صف"
        RUNNING = "running", "در حال اجرا"
        DONE = "done", "تمام"
        FAILED = "failed", "ناموفق"
        PAUSED = "paused", "متوقف"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    account = models.ForeignKey(InstagramAccount, on_delete=models.PROTECT)
    target = models.CharField(max_length=100)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.QUEUED)
    stage = models.CharField(max_length=40, default="profile")
    cursor = models.JSONField(default=dict)
    attempts = models.PositiveIntegerField(default=0)
    lease_until = models.DateTimeField(null=True, blank=True)
    heartbeat_at = models.DateTimeField(null=True, blank=True)
    error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class CrawlMetric(models.Model):
    job = models.ForeignKey(CrawlJob, related_name="metrics", on_delete=models.CASCADE)
    stage = models.CharField(max_length=40)
    duration_ms = models.PositiveIntegerField(default=0)
    records = models.PositiveIntegerField(default=0)
    status = models.CharField(max_length=20)
    message = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)


class ProfileSnapshot(models.Model):
    job = models.ForeignKey(CrawlJob, on_delete=models.CASCADE)
    instagram_pk = models.CharField(max_length=64)
    username = models.CharField(max_length=100)
    full_name = models.CharField(max_length=250, blank=True)
    is_private = models.BooleanField(default=False)
    is_verified = models.BooleanField(default=False)
    followers_count = models.PositiveBigIntegerField(null=True, blank=True)
    following_count = models.PositiveBigIntegerField(null=True, blank=True)
    media_count = models.PositiveBigIntegerField(null=True, blank=True)
    posts_count = models.PositiveBigIntegerField(null=True, blank=True)
    reels_count = models.PositiveBigIntegerField(null=True, blank=True)
    collected_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["job", "instagram_pk"], name="unique_profile_per_job")]


class CrawlSession(models.Model):
    """Parent checkpoint for one unified observation; survives worker restarts."""
    observation = models.OneToOneField("ObservationJob", related_name="crawl_session", on_delete=models.CASCADE)
    account = models.ForeignKey(InstagramAccount, on_delete=models.PROTECT)
    target = models.CharField(max_length=100, db_index=True)
    instagram_pk = models.CharField(max_length=64, blank=True, db_index=True)
    profile_username = models.CharField(max_length=100, blank=True)
    profile_full_name = models.CharField(max_length=250, blank=True)
    profile_is_private = models.BooleanField(null=True, blank=True)
    profile_is_verified = models.BooleanField(null=True, blank=True)
    profile_followers_count = models.PositiveBigIntegerField(null=True, blank=True)
    profile_following_count = models.PositiveBigIntegerField(null=True, blank=True)
    profile_posts_count = models.PositiveBigIntegerField(null=True, blank=True)
    profile_reels_count = models.PositiveBigIntegerField(null=True, blank=True)
    status = models.CharField(max_length=20, default="initialized", db_index=True)
    last_crawl_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class CrawlCursor(models.Model):
    """Durable per-stream cursor/anchor used by the unified crawl engine."""
    session = models.ForeignKey("CrawlSession", related_name="cursors", on_delete=models.CASCADE)
    stream = models.CharField(max_length=32)
    cursor = models.TextField(blank=True)
    last_media_id = models.CharField(max_length=64, blank=True)
    last_item_id = models.CharField(max_length=128, blank=True)
    pages = models.PositiveIntegerField(default=0)
    extracted = models.PositiveBigIntegerField(default=0)
    total_expected = models.PositiveBigIntegerField(null=True, blank=True)
    finished = models.BooleanField(default=False)
    issue = models.CharField(max_length=500, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["session", "stream"], name="unique_crawl_cursor_stream")]


class ExtractionProgress(models.Model):
    """Human-readable progress for dashboard and resumable workers."""
    session = models.ForeignKey("CrawlSession", related_name="progress_rows", on_delete=models.CASCADE)
    stream = models.CharField(max_length=32)
    expected = models.PositiveBigIntegerField(null=True, blank=True)
    extracted = models.PositiveBigIntegerField(default=0)
    pages = models.PositiveIntegerField(default=0)
    status = models.CharField(max_length=20, default="pending")
    last_crawl_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["session", "stream"], name="unique_extraction_progress_stream")]

    @property
    def percent(self):
        if not self.expected:
            return 0
        return min(100, round(self.extracted * 100 / self.expected, 1))


class Contact(models.Model):
    """Owner-scoped contact projection backed by the canonical Instagram identity."""
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    identity = models.ForeignKey("InstagramIdentity", related_name="contacts", on_delete=models.CASCADE)
    username = models.CharField(max_length=100, db_index=True)
    first_seen_at = models.DateTimeField(default=timezone.now)
    last_seen_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["owner", "identity"], name="unique_owner_contact_identity")]


class FollowEdge(models.Model):
    """Fast graph projection of a directed Instagram follow edge."""
    source = models.ForeignKey("InstagramIdentity", related_name="follow_edges_out", on_delete=models.CASCADE)
    target = models.ForeignKey("InstagramIdentity", related_name="follow_edges_in", on_delete=models.CASCADE)
    first_seen_at = models.DateTimeField(default=timezone.now)
    last_seen_at = models.DateTimeField(default=timezone.now, db_index=True)
    is_active = models.BooleanField(default=True, db_index=True)
    last_session = models.ForeignKey("CrawlSession", null=True, blank=True, on_delete=models.SET_NULL)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["source", "target"], name="unique_follow_edge")]
        indexes = [models.Index(fields=["source", "is_active"]), models.Index(fields=["target", "is_active"])]


class FollowerSnapshot(models.Model):
    """Immutable observation of a relationship during one crawl."""
    session = models.ForeignKey("CrawlSession", related_name="follower_snapshots", on_delete=models.CASCADE)
    stream = models.CharField(max_length=12)
    edge = models.ForeignKey("FollowEdge", related_name="snapshots", on_delete=models.CASCADE)
    observed_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["session", "stream", "edge"], name="unique_follower_snapshot")]



class SiteSettings(models.Model):
    """Owner-scoped operational settings editable from one central page."""
    owner = models.OneToOneField(settings.AUTH_USER_MODEL, related_name="instradar_settings",
                                 on_delete=models.CASCADE)
    max_concurrent_observations = models.PositiveSmallIntegerField(default=2)
    enrichment_hour = models.PositiveSmallIntegerField(default=3)
    enrichment_minute = models.PositiveSmallIntegerField(default=0)
    enrichment_enabled = models.BooleanField(default=True)
    default_page_size = models.PositiveSmallIntegerField(default=40)
    last_enrichment_date = models.DateField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)


class ObservationJob(models.Model):
    """Unified parent job; internal crawlers remain resumable implementation details."""
    class Status(models.TextChoices):
        QUEUED = "queued", "در صف"
        RUNNING = "running", "در حال اجرا"
        DONE = "done", "کامل"
        PARTIAL = "partial", "ناقص"
        FAILED = "failed", "ناموفق"
        PAUSED = "paused", "متوقف"
        ARCHIVED = "archived", "بایگانی‌شده"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, related_name="observation_jobs",
                              on_delete=models.CASCADE)
    account = models.ForeignKey(InstagramAccount, related_name="observation_jobs",
                                on_delete=models.PROTECT)
    account_pool = models.ForeignKey(
        InstagramAccountPool, related_name="observation_jobs", null=True, blank=True,
        on_delete=models.PROTECT)
    max_active_accounts = models.PositiveSmallIntegerField(default=1)
    target = models.CharField(max_length=100, db_index=True)
    mode = models.CharField(max_length=16, default="full")
    include_posts = models.BooleanField(default=True)
    include_reels = models.BooleanField(default=True)
    include_followers = models.BooleanField(default=False)
    include_following = models.BooleanField(default=False)
    include_comments = models.BooleanField(default=False)
    include_likes = models.BooleanField(default=True)
    publish_to_telegram = models.BooleanField(default=False)
    telegram_account = models.ForeignKey("TelegramAccount", related_name="observation_jobs",
                                         null=True, blank=True, on_delete=models.SET_NULL)
    telegram_destinations = models.ManyToManyField("TelegramDestination", blank=True,
                                                    related_name="observation_jobs")
    caption_template = models.TextField(blank=True)
    priority = models.PositiveSmallIntegerField(default=5, db_index=True)
    stage_order = models.JSONField(default=list, blank=True)
    status = models.CharField(max_length=16, choices=Status.choices,
                              default=Status.QUEUED, db_index=True)
    current_stage = models.CharField(max_length=24, default="media")
    max_pages = models.PositiveIntegerField(default=0)
    date_from = models.DateField(null=True, blank=True)
    date_to = models.DateField(null=True, blank=True)
    error = models.CharField(max_length=500, blank=True)
    archived_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    comments_scheduled_count = models.PositiveIntegerField(default=0)
    comments_skipped_count = models.PositiveIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-priority", "created_at"]

    @property
    def current_stage_fa(self):
        return {
            "media": "محتوا", "relationships": "فالورها و فالوینگ‌ها",
            "posts": "پست‌ها", "reels": "ریلزها",
            "followers": "فالورها", "following": "فالوینگ‌ها",
            "comments": "کامنت‌ها", "telegram": "انتقال تلگرام",
            "complete": "پایان",
        }.get(self.current_stage, self.current_stage)


class MediaSyncJob(models.Model):
    class Status(models.TextChoices):
        QUEUED = "queued", "در صف"
        RUNNING = "running", "در حال اجرا"
        DONE = "done", "کامل"
        PARTIAL = "partial", "ناقص"
        FAILED = "failed", "ناموفق"
        PAUSED = "paused", "متوقف"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    observation = models.OneToOneField("ObservationJob", related_name="media_job", null=True,
                                       blank=True, on_delete=models.CASCADE)
    account = models.ForeignKey(InstagramAccount, on_delete=models.PROTECT)
    target = models.CharField(max_length=100, db_index=True)
    target_pk = models.CharField(max_length=64, blank=True)
    mode = models.CharField(max_length=16, default="full")  # full / incremental
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.QUEUED, db_index=True)
    expected_media_count = models.PositiveIntegerField(null=True, blank=True)
    date_from = models.DateField(null=True, blank=True)
    date_to = models.DateField(null=True, blank=True)
    max_pages = models.PositiveIntegerField(default=0)  # 0 means no artificial cap
    attempts = models.PositiveIntegerField(default=0)
    lease_until = models.DateTimeField(null=True, blank=True)
    error = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class PostSyncCursor(models.Model):
    job = models.ForeignKey(MediaSyncJob, related_name="streams", on_delete=models.CASCADE)
    stream = models.CharField(max_length=12)  # timeline / reels
    cursor = models.TextField(blank=True)
    api = models.CharField(max_length=12, default="private")
    pages = models.PositiveIntegerField(default=0)
    seen = models.PositiveIntegerField(default=0)
    top_media_pk = models.CharField(max_length=64, blank=True)
    previous_top_pk = models.CharField(max_length=64, blank=True)
    last_shortcode = models.CharField(max_length=100, blank=True)
    finished = models.BooleanField(default=False)
    issue = models.CharField(max_length=300, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["job", "stream"], name="unique_stream_per_sync")]


class PostCrawlState(models.Model):
    account = models.ForeignKey(InstagramAccount, on_delete=models.CASCADE)
    target_pk = models.CharField(max_length=64)
    stream = models.CharField(max_length=12)
    last_top_pk = models.CharField(max_length=64, blank=True)
    last_complete_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["account", "target_pk", "stream"], name="unique_last_complete_scan")]


class Post(models.Model):
    media_pk = models.CharField(max_length=64, unique=True)
    shortcode = models.CharField(max_length=100, unique=True)
    owner_pk = models.CharField(max_length=64)
    owner_username = models.CharField(max_length=100)
    owner_identity = models.ForeignKey(
        "InstagramIdentity", related_name="posts", null=True, blank=True,
        on_delete=models.SET_NULL)
    discovered_from = models.CharField(max_length=100, db_index=True)
    media_type = models.PositiveSmallIntegerField(default=1)
    is_reel = models.BooleanField(default=False)
    caption = models.TextField(blank=True)
    caption_hash = models.CharField(max_length=64, blank=True)
    published_at = models.DateTimeField(null=True, blank=True)
    discovered_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    like_count = models.PositiveBigIntegerField(null=True, blank=True)
    view_count = models.PositiveBigIntegerField(null=True, blank=True)
    play_count = models.PositiveBigIntegerField(null=True, blank=True)
    comment_count = models.PositiveBigIntegerField(null=True, blank=True)
    detail_status = models.CharField(max_length=12, default="listing")
    availability = models.CharField(max_length=16, default="visible")

    class Meta:
        indexes = [
            models.Index(fields=["owner_username", "-published_at"],
                         name="post_owner_pub_idx"),
            models.Index(fields=["is_reel", "-published_at"],
                         name="post_kind_pub_idx"),
        ]

    @property
    def url(self):
        kind = "reel" if self.is_reel else "p"
        return f"https://www.instagram.com/{kind}/{self.shortcode}/"

    @property
    def display_view_count(self):
        """Instagram may expose the same video counter as view or play."""
        return self.view_count if self.view_count is not None else self.play_count


class PostSnapshot(models.Model):
    job = models.ForeignKey(MediaSyncJob, on_delete=models.CASCADE)
    post = models.ForeignKey(Post, on_delete=models.CASCADE)
    caption_hash = models.CharField(max_length=64)
    like_count = models.PositiveBigIntegerField(null=True, blank=True)
    view_count = models.PositiveBigIntegerField(null=True, blank=True)
    play_count = models.PositiveBigIntegerField(null=True, blank=True)
    comment_count = models.PositiveBigIntegerField(null=True, blank=True)
    collected_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["job", "post"], name="unique_post_per_sync")]


class PostMetricHistory(models.Model):
    post = models.ForeignKey(Post, related_name="metric_history", on_delete=models.CASCADE)
    job = models.ForeignKey(MediaSyncJob, on_delete=models.CASCADE)
    like_count = models.PositiveBigIntegerField(null=True, blank=True)
    view_count = models.PositiveBigIntegerField(null=True, blank=True)
    play_count = models.PositiveBigIntegerField(null=True, blank=True)
    comment_count = models.PositiveBigIntegerField(null=True, blank=True)
    recorded_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["post", "job"], name="unique_metric_change_per_sync")]


class PostAsset(models.Model):
    post = models.ForeignKey(Post, related_name="assets", on_delete=models.CASCADE)
    position = models.PositiveIntegerField()
    media_type = models.PositiveSmallIntegerField()
    preview_url = models.URLField(max_length=2000, blank=True)
    video_url = models.URLField(max_length=2000, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["post", "position"], name="unique_asset_position")]


class PostHashtag(models.Model):
    post = models.ForeignKey(Post, related_name="hashtags", on_delete=models.CASCADE)
    tag = models.CharField(max_length=150)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["post", "tag"], name="unique_post_hashtag")]


class PostMention(models.Model):
    post = models.ForeignKey(Post, related_name="mentions", on_delete=models.CASCADE)
    username = models.CharField(max_length=100)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["post", "username"], name="unique_post_mention")]


class PostCollaborator(models.Model):
    post = models.ForeignKey(Post, related_name="collaborators", on_delete=models.CASCADE)
    instagram_pk = models.CharField(max_length=64, blank=True)
    username = models.CharField(max_length=100)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["post", "username"], name="unique_post_collaborator")]


class PostLocation(models.Model):
    post = models.OneToOneField(Post, related_name="post_location", on_delete=models.CASCADE)
    instagram_pk = models.CharField(max_length=64, blank=True)
    name = models.CharField(max_length=250)
    city = models.CharField(max_length=150, blank=True)


class MediaSyncEvent(models.Model):
    job = models.ForeignKey(MediaSyncJob, related_name="events", on_delete=models.CASCADE)
    stream = models.CharField(max_length=12)
    message = models.CharField(max_length=300)
    records = models.PositiveIntegerField(default=0)
    duration_ms = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)


class CommentSyncJob(models.Model):
    class Status(models.TextChoices):
        QUEUED = "queued", "در صف"
        RUNNING = "running", "در حال اجرا"
        DONE = "done", "کامل"
        PARTIAL = "partial", "ناقص"
        FAILED = "failed", "ناموفق"
        PAUSED = "paused", "متوقف"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    observation = models.ForeignKey(ObservationJob, related_name="comment_jobs", null=True,
                                    blank=True, on_delete=models.CASCADE)
    account = models.ForeignKey(InstagramAccount, on_delete=models.PROTECT)
    post = models.ForeignKey(Post, related_name="comment_jobs", on_delete=models.PROTECT)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.QUEUED, db_index=True)
    expected_count = models.PositiveBigIntegerField(null=True, blank=True)
    scan_started_at = models.DateTimeField(default=timezone.now)
    top_cursor = models.TextField(blank=True)
    top_finished = models.BooleanField(default=False)
    top_pages = models.PositiveIntegerField(default=0)
    extracted_count = models.PositiveBigIntegerField(default=0)
    attempts = models.PositiveIntegerField(default=0)
    lease_until = models.DateTimeField(null=True, blank=True)
    scheduled_at = models.DateTimeField(null=True, blank=True, db_index=True)
    account_switches = models.PositiveSmallIntegerField(default=0)
    error = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class InstagramAccountLease(models.Model):
    """Exclusive, expiring account lease so workers cannot overlap one session."""
    account = models.OneToOneField(
        InstagramAccount, related_name="scheduler_lease", on_delete=models.CASCADE)
    observation = models.ForeignKey(
        ObservationJob, related_name="account_leases", null=True, blank=True,
        on_delete=models.CASCADE)
    comment_job = models.OneToOneField(
        CommentSyncJob, related_name="account_lease", null=True, blank=True,
        on_delete=models.CASCADE)
    purpose = models.CharField(max_length=24, default="comments")
    expires_at = models.DateTimeField(db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)


class InstagramAccountHealthEvent(models.Model):
    account = models.ForeignKey(
        InstagramAccount, related_name="health_events", on_delete=models.CASCADE)
    observation = models.ForeignKey(
        ObservationJob, related_name="account_health_events", null=True, blank=True,
        on_delete=models.SET_NULL)
    kind = models.CharField(max_length=32, db_index=True)
    message = models.CharField(max_length=300, blank=True)
    cooldown_until = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)


class PostCommentSyncState(models.Model):
    """Owner-scoped checkpoint used to avoid unchanged comment rescans."""
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    post = models.ForeignKey(Post, related_name="comment_sync_states", on_delete=models.CASCADE)
    last_account = models.ForeignKey(
        InstagramAccount, null=True, blank=True, on_delete=models.SET_NULL)
    last_observed_count = models.PositiveBigIntegerField(null=True, blank=True)
    last_status = models.CharField(max_length=12, blank=True)
    last_scan_at = models.DateTimeField(null=True, blank=True)
    last_complete_at = models.DateTimeField(null=True, blank=True, db_index=True)

    class Meta:
        constraints = [models.UniqueConstraint(
            fields=["owner", "post"], name="unique_owner_post_comment_state")]


class Comment(models.Model):
    post = models.ForeignKey(Post, related_name="comments", on_delete=models.CASCADE)
    instagram_pk = models.CharField(max_length=64, unique=True)
    parent = models.ForeignKey("self", related_name="replies", null=True, blank=True, on_delete=models.SET_NULL)
    parent_instagram_pk = models.CharField(max_length=64, blank=True)
    author_pk = models.CharField(max_length=64, blank=True, db_index=True)
    author_username = models.CharField(max_length=100, blank=True, db_index=True)
    author_identity = models.ForeignKey(
        "InstagramIdentity", related_name="comments", null=True, blank=True,
        on_delete=models.SET_NULL)
    text = models.TextField(blank=True)
    like_count = models.PositiveBigIntegerField(null=True, blank=True)
    published_at = models.DateTimeField(null=True, blank=True)
    first_seen_at = models.DateTimeField(default=timezone.now)
    last_seen_at = models.DateTimeField(default=timezone.now)
    last_seen_job = models.ForeignKey(CommentSyncJob, null=True, on_delete=models.SET_NULL)
    unavailable = models.BooleanField(default=False)

    class Meta:
        indexes = [
            models.Index(fields=["post", "-published_at"],
                         name="comment_post_pub_idx"),
            models.Index(fields=["author_username", "-published_at"],
                         name="comment_author_pub_idx"),
        ]


class CommentMention(models.Model):
    comment = models.ForeignKey(Comment, related_name="mentions", on_delete=models.CASCADE)
    username = models.CharField(max_length=100)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["comment", "username"], name="unique_comment_mention")]


class PostLike(models.Model):
    """A liker exposed by Instagram for one post; unavailable likers are never invented."""
    post = models.ForeignKey(Post, related_name="likers", on_delete=models.CASCADE)
    identity = models.ForeignKey(
        "InstagramIdentity", related_name="liked_posts", on_delete=models.CASCADE)
    source_job = models.ForeignKey(MediaSyncJob, related_name="discovered_likes",
                                   null=True, on_delete=models.SET_NULL)
    first_seen_at = models.DateTimeField(default=timezone.now)
    last_seen_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [models.UniqueConstraint(
            fields=["post", "identity"], name="unique_post_liker")]
        indexes = [models.Index(fields=["post", "last_seen_at"], name="post_liker_seen_idx")]


class ReplySyncCursor(models.Model):
    job = models.ForeignKey(CommentSyncJob, related_name="reply_cursors", on_delete=models.CASCADE)
    parent = models.ForeignKey(Comment, on_delete=models.CASCADE)
    cursor = models.TextField(blank=True)
    expected_count = models.PositiveIntegerField(null=True, blank=True)
    pages = models.PositiveIntegerField(default=0)
    finished = models.BooleanField(default=False)
    issue = models.CharField(max_length=250, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["job", "parent"], name="unique_reply_cursor_per_job")]


class CommentSyncEvent(models.Model):
    job = models.ForeignKey(CommentSyncJob, related_name="events", on_delete=models.CASCADE)
    stage = models.CharField(max_length=20)
    records = models.PositiveIntegerField(default=0)
    duration_ms = models.PositiveIntegerField(default=0)
    message = models.CharField(max_length=300)
    created_at = models.DateTimeField(auto_now_add=True)


class InstagramIdentity(models.Model):
    """Canonical Instagram person/page; usernames are mutable, Instagram PK is not."""
    instagram_pk = models.CharField(max_length=64, unique=True)
    current_username = models.CharField(max_length=100, db_index=True)
    full_name = models.CharField(max_length=250, blank=True)
    is_private = models.BooleanField(default=False)
    is_verified = models.BooleanField(default=False)
    profile_pic_url = models.TextField(blank=True)
    biography = models.TextField(blank=True)
    followers_count = models.PositiveBigIntegerField(null=True, blank=True)
    following_count = models.PositiveBigIntegerField(null=True, blank=True)
    media_count = models.PositiveBigIntegerField(null=True, blank=True)
    enriched_at = models.DateTimeField(null=True, blank=True, db_index=True)
    first_seen_at = models.DateTimeField(default=timezone.now)
    last_seen_at = models.DateTimeField(default=timezone.now, db_index=True)

    def __str__(self):
        return f"@{self.current_username}" if self.current_username else self.instagram_pk


class AccountAlias(models.Model):
    identity = models.ForeignKey(InstagramIdentity, related_name="aliases", on_delete=models.CASCADE)
    username = models.CharField(max_length=100, db_index=True)
    first_seen_at = models.DateTimeField(default=timezone.now)
    last_seen_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["identity", "username"], name="unique_identity_alias")]


class RelationshipScanJob(models.Model):
    class Status(models.TextChoices):
        QUEUED = "queued", "در صف"
        RUNNING = "running", "در حال اجرا"
        DONE = "done", "کامل"
        PARTIAL = "partial", "ناقص"
        FAILED = "failed", "ناموفق"
        PAUSED = "paused", "متوقف"

    class Mode(models.TextChoices):
        BOTH = "both", "فالورها و فالوینگ‌ها"
        FOLLOWERS = "followers", "فقط فالورها"
        FOLLOWING = "following", "فقط فالوینگ‌ها"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    observation = models.OneToOneField("ObservationJob", related_name="relationship_job", null=True,
                                       blank=True, on_delete=models.CASCADE)
    account = models.ForeignKey(InstagramAccount, on_delete=models.PROTECT)
    target = models.CharField(max_length=100, db_index=True)
    target_pk = models.CharField(max_length=64, blank=True, db_index=True)
    seed_identity = models.ForeignKey(InstagramIdentity, null=True, blank=True,
                                      related_name="seed_jobs", on_delete=models.SET_NULL)
    mode = models.CharField(max_length=16, choices=Mode.choices, default=Mode.BOTH)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.QUEUED, db_index=True)
    depth = models.PositiveSmallIntegerField(default=1)
    expected_followers = models.PositiveBigIntegerField(null=True, blank=True)
    expected_following = models.PositiveBigIntegerField(null=True, blank=True)
    max_pages = models.PositiveIntegerField(default=0)
    scan_started_at = models.DateTimeField(default=timezone.now)
    attempts = models.PositiveIntegerField(default=0)
    lease_until = models.DateTimeField(null=True, blank=True)
    error = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    completed_at = models.DateTimeField(null=True, blank=True)


class RelationshipSyncState(models.Model):
    class Stream(models.TextChoices):
        FOLLOWERS = "followers", "فالورها"
        FOLLOWING = "following", "فالوینگ‌ها"

    job = models.ForeignKey(RelationshipScanJob, related_name="streams", on_delete=models.CASCADE)
    stream = models.CharField(max_length=12, choices=Stream.choices)
    cursor = models.TextField(blank=True)
    api = models.CharField(max_length=12, default="private")
    pages = models.PositiveIntegerField(default=0)
    seen = models.PositiveBigIntegerField(default=0)
    finished = models.BooleanField(default=False)
    reconciled = models.BooleanField(default=False)
    issue = models.CharField(max_length=300, blank=True)

    @property
    def provider(self):
        return {
            "private": "aiograpi",
            "private_gql": "graphql",
            "gql": "graphql",
            "web_v1": "graphql",
            "browser": "browser",
            "auto": "aiograpi",
        }.get(self.api, self.api)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["job", "stream"], name="unique_relation_stream")]


class AccountRelationship(models.Model):
    class Type(models.TextChoices):
        FOLLOWS = "follows", "دنبال می‌کند"

    source = models.ForeignKey(InstagramIdentity, related_name="outgoing_relationships", on_delete=models.CASCADE)
    target = models.ForeignKey(InstagramIdentity, related_name="incoming_relationships", on_delete=models.CASCADE)
    relation_type = models.CharField(max_length=16, choices=Type.choices, default=Type.FOLLOWS)
    first_seen_at = models.DateTimeField(default=timezone.now)
    last_seen_at = models.DateTimeField(default=timezone.now, db_index=True)
    ended_at = models.DateTimeField(null=True, blank=True, db_index=True)
    is_active = models.BooleanField(default=True, db_index=True)
    depth = models.PositiveSmallIntegerField(default=1)
    discovered_from = models.ForeignKey(InstagramIdentity, null=True, blank=True,
                                        related_name="discovered_relationships", on_delete=models.SET_NULL)
    last_seen_job = models.ForeignKey(RelationshipScanJob, null=True, blank=True,
                                      related_name="seen_relationships", on_delete=models.SET_NULL)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["source", "target", "relation_type"], name="unique_directed_relation"),
            models.CheckConstraint(condition=~models.Q(source=models.F("target")), name="relationship_not_self"),
        ]
        indexes = [
            models.Index(fields=["source", "is_active"], name="relation_source_active_idx"),
            models.Index(fields=["target", "is_active"], name="relation_target_active_idx"),
        ]


class RelationshipSnapshot(models.Model):
    job = models.ForeignKey(RelationshipScanJob, related_name="snapshots", on_delete=models.CASCADE)
    relationship = models.ForeignKey(AccountRelationship, related_name="snapshots", on_delete=models.CASCADE)
    source = models.ForeignKey(InstagramIdentity, related_name="source_snapshots", on_delete=models.CASCADE)
    target = models.ForeignKey(InstagramIdentity, related_name="target_snapshots", on_delete=models.CASCADE)
    relation_type = models.CharField(max_length=16, default=AccountRelationship.Type.FOLLOWS)
    observed_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["job", "relationship"], name="unique_relation_per_snapshot")]

    @property
    def audience_identity(self):
        """The person related to the page that was the scan seed."""
        if self.job.seed_identity_id == self.source_id:
            return self.target
        return self.source

    @property
    def audience_kind(self):
        return "following" if self.job.seed_identity_id == self.source_id else "follower"

    @property
    def audience_kind_fa(self):
        return "فالوینگ" if self.audience_kind == "following" else "فالور"


class RelationshipChange(models.Model):
    class Type(models.TextChoices):
        FOLLOWED = "followed", "رابطه جدید"
        REACTIVATED = "reactivated", "رابطه بازگشته"
        UNFOLLOWED = "unfollowed", "لغو دنبال‌کردن"

    job = models.ForeignKey(RelationshipScanJob, related_name="changes", on_delete=models.CASCADE)
    relationship = models.ForeignKey(AccountRelationship, related_name="changes", on_delete=models.CASCADE)
    change_type = models.CharField(max_length=16, choices=Type.choices)
    happened_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["job", "relationship", "change_type"],
                                               name="unique_relation_change")]


class RelationshipScanEvent(models.Model):
    job = models.ForeignKey(RelationshipScanJob, related_name="events", on_delete=models.CASCADE)
    stream = models.CharField(max_length=16)
    records = models.PositiveIntegerField(default=0)
    duration_ms = models.PositiveIntegerField(default=0)
    message = models.CharField(max_length=400)
    created_at = models.DateTimeField(auto_now_add=True)


class GraphWorkspace(models.Model):
    """Saved, owner-scoped configuration for the Stage 5 graph builder."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, related_name="instagram_graphs",
                              on_delete=models.CASCADE)
    name = models.CharField(max_length=120)
    seed = models.ForeignKey(InstagramIdentity, related_name="graph_workspaces",
                             on_delete=models.PROTECT)
    depth = models.PositiveSmallIntegerField(default=1)
    node_limit = models.PositiveIntegerField(default=150)
    include_followers = models.BooleanField(default=True)
    include_following = models.BooleanField(default=True)
    include_mutual = models.BooleanField(default=True)
    include_comments = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=models.Q(depth__gte=1, depth__lte=3),
                                   name="graph_depth_1_3"),
            models.CheckConstraint(condition=models.Q(node_limit__gte=25, node_limit__lte=2000),
                                   name="graph_node_limit_25_2000"),
        ]
        ordering = ["-updated_at"]


class StorageVolume(models.Model):
    """A writable root used by the Stage 6 downloader."""
    name = models.CharField(max_length=80, unique=True)
    root_path = models.CharField(max_length=500, unique=True)
    max_bytes = models.PositiveBigIntegerField(default=0)
    reserved_bytes = models.PositiveBigIntegerField(default=1073741824)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name


class DownloadJob(models.Model):
    class Mode(models.TextChoices):
        SINGLE = "single", "یک پست"
        SELECTED = "selected", "پست‌های انتخاب‌شده"
        PROFILE = "profile", "همه محتوای پیج"

    class Status(models.TextChoices):
        QUEUED = "queued", "در صف"
        RUNNING = "running", "در حال دانلود"
        DONE = "done", "کامل"
        PARTIAL = "partial", "ناقص"
        FAILED = "failed", "ناموفق"
        PAUSED = "paused", "متوقف"
        CANCELLED = "cancelled", "لغوشده"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    name = models.CharField(max_length=120, blank=True)
    priority = models.PositiveSmallIntegerField(default=5, db_index=True)
    content_kind = models.CharField(max_length=12, default="all")
    archived = models.BooleanField(default=False, db_index=True)
    account = models.ForeignKey(InstagramAccount, on_delete=models.PROTECT)
    volume = models.ForeignKey(StorageVolume, null=True, blank=True, on_delete=models.PROTECT)
    mode = models.CharField(max_length=16, choices=Mode.choices)
    target_username = models.CharField(max_length=100, blank=True, db_index=True)
    status = models.CharField(max_length=16, choices=Status.choices,
                              default=Status.QUEUED, db_index=True)
    total_items = models.PositiveIntegerField(default=0)
    completed_items = models.PositiveIntegerField(default=0)
    failed_items = models.PositiveIntegerField(default=0)
    bytes_downloaded = models.PositiveBigIntegerField(default=0)
    lease_until = models.DateTimeField(null=True, blank=True)
    heartbeat_at = models.DateTimeField(null=True, blank=True)
    error = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    @property
    def progress_percent(self):
        if not self.total_items:
            return 0
        return min(100, round(100 * (self.completed_items + self.failed_items) /
                              self.total_items))


class DownloadItem(models.Model):
    class Status(models.TextChoices):
        QUEUED = "queued", "در صف"
        RUNNING = "running", "در حال دانلود"
        DONE = "done", "دانلود شد"
        FAILED = "failed", "ناموفق"
        SKIPPED = "skipped", "قبلاً موجود"

    job = models.ForeignKey(DownloadJob, related_name="items", on_delete=models.CASCADE)
    post = models.ForeignKey(Post, related_name="download_items", on_delete=models.PROTECT)
    position = models.PositiveIntegerField(default=0)
    status = models.CharField(max_length=12, choices=Status.choices,
                              default=Status.QUEUED, db_index=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    bytes_downloaded = models.PositiveBigIntegerField(default=0)
    error = models.CharField(max_length=500, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["job", "post"],
                                               name="unique_download_job_post")]
        indexes = [models.Index(fields=["job", "status"], name="download_item_status_idx")]


class MediaAsset(models.Model):
    class Kind(models.TextChoices):
        IMAGE = "image", "تصویر"
        VIDEO = "video", "ویدئو"
        OTHER = "other", "فایل"

    owner = models.ForeignKey(settings.AUTH_USER_MODEL, related_name="instagram_media_assets",
                              on_delete=models.CASCADE)
    post = models.ForeignKey(Post, related_name="downloaded_assets", on_delete=models.CASCADE)
    item = models.ForeignKey(DownloadItem, related_name="assets", null=True, blank=True,
                             on_delete=models.SET_NULL)
    position = models.PositiveIntegerField()
    kind = models.CharField(max_length=12, choices=Kind.choices, default=Kind.OTHER)
    source_url = models.TextField(blank=True)
    local_path = models.CharField(max_length=1000)
    file_name = models.CharField(max_length=300)
    mime_type = models.CharField(max_length=120, blank=True)
    size_bytes = models.PositiveBigIntegerField(default=0)
    sha256 = models.CharField(max_length=64, db_index=True)
    thumbnail_path = models.CharField(max_length=1000, blank=True)
    downloaded_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["owner", "post", "position"],
                                               name="unique_downloaded_position")]
        indexes = [models.Index(fields=["post", "sha256"], name="media_asset_hash_idx")]


class DownloadAttempt(models.Model):
    item = models.ForeignKey(DownloadItem, related_name="attempt_log", on_delete=models.CASCADE)
    attempt_number = models.PositiveSmallIntegerField()
    status = models.CharField(max_length=16)
    duration_ms = models.PositiveIntegerField(default=0)
    message = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["item", "attempt_number"],
                                               name="unique_download_attempt")]


class DownloadEvent(models.Model):
    job = models.ForeignKey(DownloadJob, related_name="events", on_delete=models.CASCADE)
    stage = models.CharField(max_length=24)
    records = models.PositiveIntegerField(default=0)
    duration_ms = models.PositiveIntegerField(default=0)
    message = models.CharField(max_length=500)
    created_at = models.DateTimeField(auto_now_add=True)


class SystemMetric(models.Model):
    captured_at = models.DateTimeField(default=timezone.now, db_index=True)
    cpu_percent = models.FloatField(default=0)
    memory_percent = models.FloatField(default=0)
    memory_used_bytes = models.PositiveBigIntegerField(default=0)
    memory_total_bytes = models.PositiveBigIntegerField(default=0)
    disk_percent = models.FloatField(default=0)
    network_rx_bytes = models.PositiveBigIntegerField(default=0)
    network_tx_bytes = models.PositiveBigIntegerField(default=0)


class JobLogCapture(models.Model):
    class Kind(models.TextChoices):
        OBSERVATION = "observation", "رصد جامع"
        PROFILE = "profile", "رصد پروفایل"
        MEDIA = "media", "رصد محتوا"
        COMMENT = "comment", "رصد کامنت"
        RELATIONSHIP = "relationship", "رصد روابط"
        DOWNLOAD = "download", "دانلود رسانه"
        TRANSFER = "transfer", "انتقال"

    class Status(models.TextChoices):
        COLLECTING = "collecting", "در حال جمع‌آوری"
        READY = "ready", "آماده"
        PARTIAL = "partial", "ناقص"
        FAILED = "failed", "ناموفق"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    job_kind = models.CharField(max_length=16, choices=Kind.choices, db_index=True)
    job_id = models.CharField(max_length=64, db_index=True)
    window_seconds = models.PositiveIntegerField(default=60)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.COLLECTING)
    started_at = models.DateTimeField(default=timezone.now)
    ends_at = models.DateTimeField()
    collected_at = models.DateTimeField(null=True, blank=True)
    journal_text = models.TextField(blank=True)
    job_events = models.JSONField(default=list)
    line_count = models.PositiveIntegerField(default=0)
    error = models.CharField(max_length=300, blank=True)

    class Meta:
        indexes = [models.Index(fields=["owner", "job_kind", "job_id", "-started_at"],
                                name="job_log_lookup_idx")]


class TelegramSettings(models.Model):
    """Owner-scoped Telegram application credentials; API hash is encrypted."""
    owner = models.OneToOneField(settings.AUTH_USER_MODEL, related_name="telegram_settings",
                                 on_delete=models.CASCADE)
    api_id = models.PositiveBigIntegerField()
    encrypted_api_hash = models.BinaryField()
    updated_at = models.DateTimeField(auto_now=True)


class TelegramAccount(models.Model):
    class Status(models.TextChoices):
        NEW = "new", "ثبت اولیه"
        CODE_SENT = "code_sent", "کد ارسال شد"
        NEEDS_PASSWORD = "needs_password", "نیازمند رمز دومرحله‌ای"
        ACTIVE = "active", "متصل"
        ERROR = "error", "خطا"

    owner = models.ForeignKey(settings.AUTH_USER_MODEL, related_name="telegram_accounts",
                              on_delete=models.CASCADE)
    title = models.CharField(max_length=100)
    phone = models.CharField(max_length=32)
    telegram_user_id = models.BigIntegerField(null=True, blank=True)
    username = models.CharField(max_length=100, blank=True)
    first_name = models.CharField(max_length=150, blank=True)
    encrypted_session = models.BinaryField(null=True, blank=True)
    encrypted_pending_session = models.BinaryField(null=True, blank=True)
    encrypted_phone_code_hash = models.BinaryField(null=True, blank=True)
    status = models.CharField(max_length=24, choices=Status.choices, default=Status.NEW,
                              db_index=True)
    last_error = models.CharField(max_length=500, blank=True)
    connected_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    is_active = models.BooleanField(default=True, db_index=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["owner", "phone"],
                                               name="unique_owner_telegram_phone")]


class TelegramDestination(models.Model):
    class Kind(models.TextChoices):
        CHANNEL = "channel", "کانال"
        GROUP = "group", "گروه"
        SUPERGROUP = "supergroup", "سوپرگروه"

    account = models.ForeignKey(TelegramAccount, related_name="destinations",
                                on_delete=models.CASCADE)
    peer_id = models.BigIntegerField()
    access_hash = models.BigIntegerField(null=True, blank=True)
    title = models.CharField(max_length=250)
    username = models.CharField(max_length=100, blank=True)
    kind = models.CharField(max_length=16, choices=Kind.choices)
    is_admin = models.BooleanField(default=False)
    can_post = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    last_synced_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["account", "peer_id"],
                                               name="unique_telegram_destination")]


class PublishJob(models.Model):
    class Mode(models.TextChoices):
        OBSERVATION = "observation", "ارسال همراه رصد"
        TRANSFER = "transfer", "انتقال آرشیو"

    class Status(models.TextChoices):
        WAITING = "waiting", "منتظر پایان رصد"
        QUEUED = "queued", "در صف"
        RUNNING = "running", "در حال ارسال"
        DONE = "done", "کامل"
        PARTIAL = "partial", "ناقص"
        FAILED = "failed", "ناموفق"
        PAUSED = "paused", "متوقف"
        CANCELLED = "cancelled", "لغوشده"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, related_name="telegram_publish_jobs",
                              on_delete=models.CASCADE)
    observation = models.OneToOneField("ObservationJob", related_name="publish_job", null=True,
                                       blank=True, on_delete=models.CASCADE)
    telegram_account = models.ForeignKey(TelegramAccount, related_name="publish_jobs",
                                         on_delete=models.PROTECT)
    instagram_account = models.ForeignKey(InstagramAccount, related_name="publish_jobs",
                                          on_delete=models.PROTECT)
    media_job = models.OneToOneField(MediaSyncJob, related_name="publish_job", null=True,
                                     blank=True, on_delete=models.SET_NULL)
    mode = models.CharField(max_length=16, choices=Mode.choices)
    target_username = models.CharField(max_length=100, db_index=True)
    caption_template = models.TextField(blank=True)
    force_resend = models.BooleanField(default=False)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.QUEUED,
                              db_index=True)
    total_items = models.PositiveIntegerField(default=0)
    completed_items = models.PositiveIntegerField(default=0)
    failed_items = models.PositiveIntegerField(default=0)
    lease_until = models.DateTimeField(null=True, blank=True)
    heartbeat_at = models.DateTimeField(null=True, blank=True)
    error = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    @property
    def progress_percent(self):
        if not self.total_items:
            return 0
        return min(100, round(100 * (self.completed_items + self.failed_items) /
                              self.total_items))


class PublishJobDestination(models.Model):
    job = models.ForeignKey(PublishJob, related_name="destination_links",
                            on_delete=models.CASCADE)
    destination = models.ForeignKey(TelegramDestination, related_name="job_links",
                                    on_delete=models.PROTECT)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["job", "destination"],
                                               name="unique_publish_job_destination")]


class PublishItem(models.Model):
    class Status(models.TextChoices):
        QUEUED = "queued", "در صف"
        RUNNING = "running", "در حال ارسال"
        DONE = "done", "ارسال شد"
        FAILED = "failed", "ناموفق"
        SKIPPED = "skipped", "قبلاً ارسال شده"

    job = models.ForeignKey(PublishJob, related_name="items", on_delete=models.CASCADE)
    post = models.ForeignKey(Post, related_name="publish_items", on_delete=models.PROTECT)
    sequence = models.PositiveIntegerField(default=0)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.QUEUED,
                              db_index=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    error = models.CharField(max_length=500, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["job", "post"],
                                               name="unique_publish_job_post")]
        ordering = ["sequence", "id"]


class PublishAttempt(models.Model):
    item = models.ForeignKey(PublishItem, related_name="attempt_log", on_delete=models.CASCADE)
    attempt_number = models.PositiveSmallIntegerField()
    status = models.CharField(max_length=16)
    duration_ms = models.PositiveIntegerField(default=0)
    message = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["item", "attempt_number"],
                                               name="unique_publish_attempt")]


class TelegramMessage(models.Model):
    job = models.ForeignKey(PublishJob, related_name="telegram_messages",
                            on_delete=models.CASCADE)
    item = models.ForeignKey(PublishItem, related_name="telegram_messages",
                             on_delete=models.CASCADE)
    destination = models.ForeignKey(TelegramDestination, related_name="messages",
                                    on_delete=models.PROTECT)
    post = models.ForeignKey(Post, related_name="telegram_messages", on_delete=models.PROTECT)
    message_ids = models.JSONField(default=list)
    sent_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["destination", "post"],
                                               name="unique_telegram_destination_post")]


class CrawlCursorState(models.Model):
    """Persistent incremental crawl state per Instagram target."""
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    target_username = models.CharField(max_length=100, db_index=True)
    instagram_pk = models.CharField(max_length=64, blank=True, db_index=True)
    stream = models.CharField(max_length=40, db_index=True)
    cursor = models.JSONField(default=dict)
    total_expected = models.BigIntegerField(default=0)
    extracted_count = models.BigIntegerField(default=0)
    status = models.CharField(max_length=30, default="ready")
    last_seen_item = models.CharField(max_length=128, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("owner", "target_username", "stream")


class ProfileDiscoveryState(models.Model):
    """Daily snapshot used for incremental decisions."""
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    username = models.CharField(max_length=100, db_index=True)
    instagram_pk = models.CharField(max_length=64, blank=True)
    full_name = models.CharField(max_length=250, blank=True)
    is_private = models.BooleanField(default=False)
    is_verified = models.BooleanField(default=False)
    posts_count = models.BigIntegerField(default=0)
    reels_count = models.BigIntegerField(null=True, blank=True)
    followers_count = models.BigIntegerField(default=0)
    following_count = models.BigIntegerField(default=0)
    collected_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-collected_at"]
