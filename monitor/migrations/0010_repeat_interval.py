from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("monitor", "0009_lockalert_details"),
    ]

    operations = [
        migrations.AddField(
            model_name="notificationsettings",
            name="repeat_interval_seconds",
            field=models.PositiveIntegerField(default=600),
        ),
    ]
