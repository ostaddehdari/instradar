import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("radar", "0019_stage12_progress_and_indexes"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="commentsyncjob", name="account_switches",
            field=models.PositiveSmallIntegerField(default=0)),
        migrations.AddField(
            model_name="commentsyncjob", name="scheduled_at",
            field=models.DateTimeField(blank=True, db_index=True, null=True)),
        migrations.AddField(
            model_name="instagramaccount", name="consecutive_failures",
            field=models.PositiveSmallIntegerField(default=0)),
        migrations.AddField(
            model_name="instagramaccount", name="cooldown_until",
            field=models.DateTimeField(blank=True, db_index=True, null=True)),
        migrations.AddField(
            model_name="instagramaccount", name="daily_request_limit",
            field=models.PositiveIntegerField(default=300)),
        migrations.AddField(
            model_name="instagramaccount", name="health_score",
            field=models.PositiveSmallIntegerField(default=100)),
        migrations.AddField(
            model_name="instagramaccount", name="health_status",
            field=models.CharField(
                choices=[("ready", "آماده"), ("cooldown", "در حال استراحت"),
                         ("quarantined", "قرنطینه")],
                db_index=True, default="ready", max_length=16)),
        migrations.AddField(
            model_name="instagramaccount", name="hourly_request_limit",
            field=models.PositiveIntegerField(default=60)),
        migrations.AddField(
            model_name="instagramaccount", name="last_request_at",
            field=models.DateTimeField(blank=True, null=True)),
        migrations.AddField(
            model_name="instagramaccount", name="min_interval_seconds",
            field=models.PositiveSmallIntegerField(default=10)),
        migrations.AddField(
            model_name="instagramaccount", name="request_day",
            field=models.DateField(blank=True, null=True)),
        migrations.AddField(
            model_name="instagramaccount", name="request_window_started_at",
            field=models.DateTimeField(blank=True, null=True)),
        migrations.AddField(
            model_name="instagramaccount", name="requests_this_hour",
            field=models.PositiveIntegerField(default=0)),
        migrations.AddField(
            model_name="instagramaccount", name="requests_today",
            field=models.PositiveIntegerField(default=0)),
        migrations.AddField(
            model_name="observationjob", name="comments_scheduled_count",
            field=models.PositiveIntegerField(default=0)),
        migrations.AddField(
            model_name="observationjob", name="comments_skipped_count",
            field=models.PositiveIntegerField(default=0)),
        migrations.AddField(
            model_name="observationjob", name="max_active_accounts",
            field=models.PositiveSmallIntegerField(default=1)),
        migrations.CreateModel(
            name="InstagramAccountPool",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True,
                                           serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=100)),
                ("is_active", models.BooleanField(db_index=True, default=True)),
                ("max_concurrency", models.PositiveSmallIntegerField(default=2)),
                ("hourly_request_limit", models.PositiveIntegerField(default=300)),
                ("daily_request_limit", models.PositiveIntegerField(default=2000)),
                ("request_window_started_at", models.DateTimeField(blank=True, null=True)),
                ("requests_this_hour", models.PositiveIntegerField(default=0)),
                ("request_day", models.DateField(blank=True, null=True)),
                ("requests_today", models.PositiveIntegerField(default=0)),
                ("last_request_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("owner", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="instagram_account_pools", to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ["name", "id"]}),
        migrations.AddField(
            model_name="observationjob", name="account_pool",
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                related_name="observation_jobs", to="radar.instagramaccountpool")),
        migrations.CreateModel(
            name="InstagramAccountPoolMember",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True,
                                           serialize=False, verbose_name="ID")),
                ("is_active", models.BooleanField(db_index=True, default=True)),
                ("allow_comments", models.BooleanField(default=True)),
                ("last_used_at", models.DateTimeField(blank=True, db_index=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("account", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="pool_memberships", to="radar.instagramaccount")),
                ("pool", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="members", to="radar.instagramaccountpool")),
            ]),
        migrations.CreateModel(
            name="InstagramAccountHealthEvent",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True,
                                           serialize=False, verbose_name="ID")),
                ("kind", models.CharField(db_index=True, max_length=32)),
                ("message", models.CharField(blank=True, max_length=300)),
                ("cooldown_until", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("account", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="health_events", to="radar.instagramaccount")),
                ("observation", models.ForeignKey(
                    blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                    related_name="account_health_events", to="radar.observationjob")),
            ]),
        migrations.CreateModel(
            name="InstagramAccountLease",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True,
                                           serialize=False, verbose_name="ID")),
                ("purpose", models.CharField(default="comments", max_length=24)),
                ("expires_at", models.DateTimeField(db_index=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("account", models.OneToOneField(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="scheduler_lease", to="radar.instagramaccount")),
                ("comment_job", models.OneToOneField(
                    blank=True, null=True, on_delete=django.db.models.deletion.CASCADE,
                    related_name="account_lease", to="radar.commentsyncjob")),
                ("observation", models.ForeignKey(
                    blank=True, null=True, on_delete=django.db.models.deletion.CASCADE,
                    related_name="account_leases", to="radar.observationjob")),
            ]),
        migrations.CreateModel(
            name="PostCommentSyncState",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True,
                                           serialize=False, verbose_name="ID")),
                ("last_observed_count", models.PositiveBigIntegerField(blank=True, null=True)),
                ("last_status", models.CharField(blank=True, max_length=12)),
                ("last_scan_at", models.DateTimeField(blank=True, null=True)),
                ("last_complete_at", models.DateTimeField(blank=True, db_index=True, null=True)),
                ("last_account", models.ForeignKey(
                    blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                    to="radar.instagramaccount")),
                ("owner", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    to=settings.AUTH_USER_MODEL)),
                ("post", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="comment_sync_states", to="radar.post")),
            ]),
        migrations.AddConstraint(
            model_name="instagramaccountpool",
            constraint=models.UniqueConstraint(
                fields=("owner", "name"), name="unique_owner_instagram_pool")),
        migrations.AddConstraint(
            model_name="instagramaccountpoolmember",
            constraint=models.UniqueConstraint(
                fields=("pool", "account"), name="unique_instagram_pool_member")),
        migrations.AddConstraint(
            model_name="postcommentsyncstate",
            constraint=models.UniqueConstraint(
                fields=("owner", "post"), name="unique_owner_post_comment_state")),
    ]
