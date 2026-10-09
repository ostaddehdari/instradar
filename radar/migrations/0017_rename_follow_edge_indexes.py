from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("radar", "0016_instagramaccount_browser_user_agent"),
    ]

    operations = [
        migrations.RenameIndex(
            model_name="followedge",
            old_name="radar_followe_source_7b8a8f_idx",
            new_name="radar_follo_source__3d9a76_idx",
        ),
        migrations.RenameIndex(
            model_name="followedge",
            old_name="radar_followe_target_9b7b6c_idx",
            new_name="radar_follo_target__e6b5e4_idx",
        ),
    ]