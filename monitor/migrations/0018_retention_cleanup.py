from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("monitor", "0017_weekly_lock_reports"),
    ]

    operations = [
        migrations.AddField(
            model_name="notificationsettings",
            name="last_cleanup_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="notificationsettings",
            name="resolved_alert_retention_days",
            field=models.PositiveIntegerField(
                default=30,
                help_text="Days to keep resolved lock incidents in the dashboard database",
            ),
        ),
    ]
