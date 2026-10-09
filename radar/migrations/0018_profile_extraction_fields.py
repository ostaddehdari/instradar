from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("radar", "0017_rename_follow_edge_indexes")]

    operations = [
        migrations.AddField(model_name="profilesnapshot", name="full_name", field=models.CharField(blank=True, max_length=250)),
        migrations.AddField(model_name="profilesnapshot", name="is_private", field=models.BooleanField(default=False)),
        migrations.AddField(model_name="profilesnapshot", name="is_verified", field=models.BooleanField(default=False)),
        migrations.AddField(model_name="profilesnapshot", name="posts_count", field=models.PositiveBigIntegerField(blank=True, null=True)),
        migrations.AddField(model_name="profilesnapshot", name="reels_count", field=models.PositiveBigIntegerField(blank=True, null=True)),
        migrations.AddField(model_name="crawlsession", name="profile_username", field=models.CharField(blank=True, max_length=100)),
        migrations.AddField(model_name="crawlsession", name="profile_full_name", field=models.CharField(blank=True, max_length=250)),
        migrations.AddField(model_name="crawlsession", name="profile_is_private", field=models.BooleanField(blank=True, null=True)),
        migrations.AddField(model_name="crawlsession", name="profile_is_verified", field=models.BooleanField(blank=True, null=True)),
        migrations.AddField(model_name="crawlsession", name="profile_followers_count", field=models.PositiveBigIntegerField(blank=True, null=True)),
        migrations.AddField(model_name="crawlsession", name="profile_following_count", field=models.PositiveBigIntegerField(blank=True, null=True)),
        migrations.AddField(model_name="crawlsession", name="profile_posts_count", field=models.PositiveBigIntegerField(blank=True, null=True)),
        migrations.AddField(model_name="crawlsession", name="profile_reels_count", field=models.PositiveBigIntegerField(blank=True, null=True)),
        migrations.AddField(model_name="profilediscoverystate", name="full_name", field=models.CharField(blank=True, max_length=250)),
        migrations.AddField(model_name="profilediscoverystate", name="is_private", field=models.BooleanField(default=False)),
        migrations.AddField(model_name="profilediscoverystate", name="is_verified", field=models.BooleanField(default=False)),
        migrations.AddField(model_name="profilediscoverystate", name="reels_count", field=models.BigIntegerField(blank=True, null=True)),
    ]