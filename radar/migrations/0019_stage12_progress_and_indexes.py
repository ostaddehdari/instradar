from django.db import migrations, models
from django.db.models import Count


def backfill_comment_progress(apps, schema_editor):
    Comment = apps.get_model("radar", "Comment")
    CommentSyncJob = apps.get_model("radar", "CommentSyncJob")
    counts = (Comment.objects.exclude(last_seen_job_id__isnull=True)
              .values("last_seen_job_id").annotate(total=Count("id")))
    for row in counts.iterator(chunk_size=1000):
        CommentSyncJob.objects.filter(pk=row["last_seen_job_id"]).update(
            extracted_count=row["total"])


class Migration(migrations.Migration):
    dependencies = [("radar", "0018_profile_extraction_fields")]

    operations = [
        migrations.AddField(
            model_name="commentsyncjob",
            name="extracted_count",
            field=models.PositiveBigIntegerField(default=0),
        ),
        migrations.AddIndex(
            model_name="post",
            index=models.Index(fields=["owner_username", "-published_at"],
                               name="post_owner_pub_idx"),
        ),
        migrations.AddIndex(
            model_name="post",
            index=models.Index(fields=["is_reel", "-published_at"],
                               name="post_kind_pub_idx"),
        ),
        migrations.AddIndex(
            model_name="comment",
            index=models.Index(fields=["post", "-published_at"],
                               name="comment_post_pub_idx"),
        ),
        migrations.AddIndex(
            model_name="comment",
            index=models.Index(fields=["author_username", "-published_at"],
                               name="comment_author_pub_idx"),
        ),
        migrations.RunPython(backfill_comment_progress, migrations.RunPython.noop),
    ]
