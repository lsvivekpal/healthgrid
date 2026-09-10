from django.db import migrations, models


def populate_notification_fingerprints(apps, schema_editor):
    import hashlib

    LockAlert = apps.get_model("monitor", "LockAlert")
    for alert in LockAlert.objects.all().iterator():
        values = (
            alert.blocked_pid,
            alert.blocking_pid,
            alert.blocked_user or "",
            alert.blocking_user or "",
            alert.blocked_query or "",
            alert.blocking_query or "",
        )
        fingerprint = hashlib.sha256("\0".join(str(value) for value in values).encode("utf-8")).hexdigest()
        LockAlert.objects.filter(pk=alert.pk).update(last_notified_fingerprint=fingerprint)


class Migration(migrations.Migration):
    dependencies = [
        ("monitor", "0012_normalize_active_lock_alerts"),
    ]

    operations = [
        migrations.AddField(
            model_name="lockalert",
            name="last_notified_fingerprint",
            field=models.CharField(blank=True, max_length=64),
        ),
        migrations.RunPython(populate_notification_fingerprints, migrations.RunPython.noop),
        migrations.RemoveField(
            model_name="lockalert",
            name="observation_count",
        ),
    ]
