from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("radar", "0015_stage11_unified_resume"),
    ]

    operations = [
        migrations.AddField(
            model_name="instagramaccount",
            name="browser_user_agent",
            field=models.CharField(blank=True, max_length=500),
        ),
    ]