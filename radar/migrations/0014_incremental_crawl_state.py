from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ('radar', '0013_post_likers'),
    ]

    operations = [
        migrations.CreateModel(
            name='CrawlCursorState',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('target_username', models.CharField(db_index=True, max_length=100)),
                ('instagram_pk', models.CharField(blank=True, db_index=True, max_length=64)),
                ('stream', models.CharField(db_index=True, max_length=40)),
                ('cursor', models.JSONField(default=dict)),
                ('total_expected', models.BigIntegerField(default=0)),
                ('extracted_count', models.BigIntegerField(default=0)),
                ('status', models.CharField(default='ready', max_length=30)),
                ('last_seen_item', models.CharField(blank=True, max_length=128)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('owner', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='auth.user')),
            ],
            options={'unique_together': {('owner', 'target_username', 'stream')}},
        ),
        migrations.CreateModel(
            name='ProfileDiscoveryState',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('username', models.CharField(db_index=True, max_length=100)),
                ('instagram_pk', models.CharField(blank=True, max_length=64)),
                ('posts_count', models.BigIntegerField(default=0)),
                ('followers_count', models.BigIntegerField(default=0)),
                ('following_count', models.BigIntegerField(default=0)),
                ('collected_at', models.DateTimeField(auto_now=True)),
                ('owner', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='auth.user')),
            ],
            options={'ordering': ['-collected_at']},
        ),
    ]
