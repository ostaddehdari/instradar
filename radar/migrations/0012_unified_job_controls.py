from django.db import migrations, models


def seed_stage_order(apps, schema_editor):
    ObservationJob = apps.get_model("radar", "ObservationJob")
    ObservationJob.objects.filter(stage_order=[]).update(
        stage_order=["media", "relationships", "comments", "telegram"])


class Migration(migrations.Migration):
    dependencies = [("radar", "0011_stage8_log_kind")]
    operations = [
        migrations.AddField(
            model_name="observationjob", name="include_likes",
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name="observationjob", name="stage_order",
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name="sitesettings", name="default_page_size",
            field=models.PositiveSmallIntegerField(default=40),
        ),
        migrations.RunPython(seed_stage_order, migrations.RunPython.noop),
    ]
