from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("monitor", "0013_lock_alert_lifecycle_details"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="lockalert",
            name="last_notified_fingerprint",
        ),
        migrations.CreateModel(
            name="LockNotificationState",
            fields=[
                (
                    "id",
                    models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID"),
                ),
                ("last_fingerprint", models.CharField(blank=True, max_length=64)),
                ("last_active_keys", models.JSONField(default=list)),
                ("last_sent_at", models.DateTimeField(blank=True, null=True)),
                (
                    "instance",
                    models.OneToOneField(
                        on_delete=models.deletion.CASCADE,
                        related_name="lock_notification_state",
                        to="monitor.rdsinstance",
                    ),
                ),
            ],
        ),
    ]
