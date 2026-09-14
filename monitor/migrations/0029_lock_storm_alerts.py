from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("monitor", "0028_report_retention_days"),
    ]

    operations = [
        migrations.AddField(
            model_name="notificationsettings",
            name="lock_storm_threshold",
            field=models.PositiveIntegerField(
                default=100,
                help_text="Send one compact alert when this many active PID pairs are detected; 0 disables it",
            ),
        ),
        migrations.AddField(
            model_name="locknotificationstate",
            name="storm_active",
            field=models.BooleanField(default=False),
        ),
    ]
