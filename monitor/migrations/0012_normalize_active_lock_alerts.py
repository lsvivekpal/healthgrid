from django.db import migrations, models
from django.db.models import Q


def normalize_active_alert_keys(apps, schema_editor):
    LockAlert = apps.get_model("monitor", "LockAlert")
    active_alerts = LockAlert.objects.filter(resolved_at__isnull=True).order_by("first_seen_at", "pk")
    canonical = {}

    for alert in active_alerts:
        key = f"{alert.blocked_pid}:{alert.blocking_pid}"
        group_key = (alert.instance_id, key)
        existing = canonical.get(group_key)
        if existing is None:
            if alert.alert_key != key:
                alert.alert_key = key
                alert.save(update_fields=["alert_key"])
            canonical[group_key] = alert
            continue

        # Keep the oldest incident and merge the latest useful details into it.
        existing.last_seen_at = max(existing.last_seen_at, alert.last_seen_at)
        existing.waiting_seconds = max(existing.waiting_seconds, alert.waiting_seconds)
        existing.observation_count = max(existing.observation_count, alert.observation_count)
        existing.alerted_at = min(
            (value for value in (existing.alerted_at, alert.alerted_at) if value is not None),
            default=None,
        )
        for field in ("blocked_user", "blocking_user", "blocked_query", "blocking_query"):
            value = getattr(alert, field)
            if value:
                setattr(existing, field, value)
        existing.save(update_fields=[
            "last_seen_at", "waiting_seconds", "observation_count", "alerted_at",
            "blocked_user", "blocking_user", "blocked_query", "blocking_query",
        ])
        alert.delete()


class Migration(migrations.Migration):
    dependencies = [
        ("monitor", "0011_single_incident_notifications"),
    ]

    operations = [
        migrations.RunPython(normalize_active_alert_keys, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="lockalert",
            constraint=models.UniqueConstraint(
                fields=("instance", "alert_key"),
                condition=Q(resolved_at__isnull=True),
                name="unique_active_lock_alert",
            ),
        ),
    ]
