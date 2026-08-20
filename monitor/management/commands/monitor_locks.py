import logging
import time

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from monitor import db
from monitor.models import LockAlert, RDSInstance
from monitor.notifications import notify_lock


logger = logging.getLogger(__name__)


def lock_key(row):
    query_start = row.get("blocked_query_start")
    query_start = query_start.isoformat() if query_start else "unknown-start"
    return f"{row.get('blocked_pid')}:{row.get('blocking_pid')}:{query_start}"


def process_instance(instance, now=None):
    now = now or timezone.now()
    activity, blocking = db.fetch_activity(instance)
    seen_keys = set()
    threshold = int(getattr(settings, "LOCK_ALERT_THRESHOLD_SECONDS", 120))

    for row in blocking:
        key = lock_key(row)
        seen_keys.add(key)
        alert = LockAlert.objects.filter(instance=instance, alert_key=key, resolved_at__isnull=True).first()
        if alert is None:
            alert = LockAlert.objects.create(
                instance=instance,
                alert_key=key,
                blocked_pid=row["blocked_pid"],
                blocking_pid=row["blocking_pid"],
                blocked_query=row.get("blocked_query") or "",
                blocking_query=row.get("blocking_query") or "",
                first_seen_at=now,
                last_seen_at=now,
            )
        else:
            alert.last_seen_at = now
            alert.blocked_query = row.get("blocked_query") or alert.blocked_query
            alert.blocking_query = row.get("blocking_query") or alert.blocking_query
            alert.save(update_fields=["last_seen_at", "blocked_query", "blocking_query"])

        age = (now - alert.first_seen_at).total_seconds()
        if alert.alerted_at is None and age >= threshold:
            if notify_lock(instance, alert):
                alert.alerted_at = now
                alert.save(update_fields=["alerted_at"])

    active_alerts = LockAlert.objects.filter(instance=instance, resolved_at__isnull=True)
    for alert in active_alerts:
        if alert.alert_key in seen_keys:
            continue
        alert.resolved_at = now
        alert.last_seen_at = now
        if alert.alerted_at is not None:
            notify_lock(instance, alert, resolved=True)
        alert.save(update_fields=["resolved_at", "last_seen_at"])


class Command(BaseCommand):
    help = "Monitor registered databases and notify about locks older than the threshold."

    def add_arguments(self, parser):
        parser.add_argument("--interval", type=int, default=None, help="Seconds between checks (default: 30)")
        parser.add_argument("--once", action="store_true", help="Run one check and exit")

    def handle(self, *args, **options):
        interval = options["interval"] or getattr(settings, "LOCK_MONITOR_INTERVAL_SECONDS", 30)
        while True:
            for instance in RDSInstance.objects.filter(is_active=True):
                try:
                    process_instance(instance)
                except db.ConnectionError as exc:
                    logger.warning("Could not check locks for %s: %s", instance, exc)
                except Exception:
                    logger.exception("Unexpected lock monitor failure for %s", instance)
            if options["once"]:
                return
            time.sleep(max(1, interval))
