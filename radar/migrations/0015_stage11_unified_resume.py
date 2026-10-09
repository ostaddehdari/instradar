from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
from django.utils import timezone


class Migration(migrations.Migration):
    dependencies = [("radar", "0014_incremental_crawl_state")]

    operations = [
        migrations.CreateModel(
            name="CrawlSession",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("target", models.CharField(db_index=True, max_length=100)),
                ("instagram_pk", models.CharField(blank=True, db_index=True, max_length=64)),
                ("status", models.CharField(db_index=True, default="initialized", max_length=20)),
                ("last_crawl_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("account", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to="radar.instagramaccount")),
                ("observation", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="crawl_session", to="radar.observationjob")),
            ],
        ),
        migrations.CreateModel(
            name="CrawlCursor",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("stream", models.CharField(max_length=32)), ("cursor", models.TextField(blank=True)),
                ("last_media_id", models.CharField(blank=True, max_length=64)),
                ("last_item_id", models.CharField(blank=True, max_length=128)),
                ("pages", models.PositiveIntegerField(default=0)), ("extracted", models.PositiveBigIntegerField(default=0)),
                ("total_expected", models.PositiveBigIntegerField(blank=True, null=True)), ("finished", models.BooleanField(default=False)),
                ("issue", models.CharField(blank=True, max_length=500)), ("updated_at", models.DateTimeField(auto_now=True)),
                ("session", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="cursors", to="radar.crawlsession")),
            ],
            options={"constraints": [models.UniqueConstraint(fields=("session", "stream"), name="unique_crawl_cursor_stream")]},
        ),
        migrations.CreateModel(
            name="ExtractionProgress",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("stream", models.CharField(max_length=32)), ("expected", models.PositiveBigIntegerField(blank=True, null=True)),
                ("extracted", models.PositiveBigIntegerField(default=0)), ("pages", models.PositiveIntegerField(default=0)),
                ("status", models.CharField(default="pending", max_length=20)), ("last_crawl_at", models.DateTimeField(blank=True, null=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("session", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="progress_rows", to="radar.crawlsession")),
            ],
            options={"constraints": [models.UniqueConstraint(fields=("session", "stream"), name="unique_extraction_progress_stream")]},
        ),
        migrations.CreateModel(
            name="Contact",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("username", models.CharField(db_index=True, max_length=100)), ("first_seen_at", models.DateTimeField(default=timezone.now)),
                ("last_seen_at", models.DateTimeField(default=timezone.now)),
                ("identity", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="contacts", to="radar.instagramidentity")),
                ("owner", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to=settings.AUTH_USER_MODEL)),
            ],
            options={"constraints": [models.UniqueConstraint(fields=("owner", "identity"), name="unique_owner_contact_identity")]},
        ),
        migrations.CreateModel(
            name="FollowEdge",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("first_seen_at", models.DateTimeField(default=timezone.now)),
                ("last_seen_at", models.DateTimeField(db_index=True, default=timezone.now)),
                ("is_active", models.BooleanField(db_index=True, default=True)),
                ("last_session", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to="radar.crawlsession")),
                ("source", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="follow_edges_out", to="radar.instagramidentity")),
                ("target", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="follow_edges_in", to="radar.instagramidentity")),
            ],
            options={"constraints": [models.UniqueConstraint(fields=("source", "target"), name="unique_follow_edge")], "indexes": [models.Index(fields=["source", "is_active"], name="radar_followe_source_7b8a8f_idx"), models.Index(fields=["target", "is_active"], name="radar_followe_target_9b7b6c_idx")]},
        ),
        migrations.CreateModel(
            name="FollowerSnapshot",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("stream", models.CharField(max_length=12)), ("observed_at", models.DateTimeField(default=timezone.now)),
                ("edge", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="snapshots", to="radar.followedge")),
                ("session", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="follower_snapshots", to="radar.crawlsession")),
            ],
            options={"constraints": [models.UniqueConstraint(fields=("session", "stream", "edge"), name="unique_follower_snapshot")]},
        ),
        migrations.AddField(model_name="publishjob", name="force_resend", field=models.BooleanField(default=False)),
    ]
