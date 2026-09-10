from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("monitor", "0010_repeat_interval"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="notificationsettings",
            name="repeat_interval_seconds",
        ),
        migrations.RemoveField(
            model_name="lockalert",
            name="last_notified_at",
        ),
        migrations.AddField(
            model_name="lockalert",
            name="observation_count",
            field=models.PositiveIntegerField(default=1),
        ),
    ]
