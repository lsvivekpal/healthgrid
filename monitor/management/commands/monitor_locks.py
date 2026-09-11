import logging
import hashlib
import time

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from monitor import db
from monitor.models import LockAlert, LockNotificationState, NotificationSettings, RDSInstance
from monitor.notifications import notify_lock_summary


logger = logging.getLogger(__name__)


def lock_key(row):
    # A lock can produce multiple catalog rows and query_start can change
    # while the same backend pair remains blocked. Treat the PID pair as one
    # alert so Teams does not receive duplicate cards for one incident.
    return f"{row.get('blocked_pid')}:{row.get('blocking_pid')}"


def lock_fingerprint(row):
    """Identify meaningful lock-detail changes, excluding elapsed time."""
    values = (
        row.get("blocked_pid"),
        row.get("blocking_pid"),
        row.get("blocked_user") or "",
        row.get("blocking_user") or "",
        row.get("blocked_query") or "",
        row.get("blocking_query") or "",
    )
    return hashlib.sha256("\0".join(str(value) for value in values).encode("utf-8")).hexdigest()


def summary_fingerprint(alerts):
    values = sorted(
        f"{alert.alert_key}:{lock_fingerprint({
            'blocked_pid': alert.blocked_pid,
            'blocking_pid': alert.blocking_pid,
            'blocked_user': alert.blocked_user,
            'blocking_user': alert.blocking_user,
            'blocked_query': alert.blocked_query,
            'blocking_query': alert.blocking_query,
        })}"
        for alert in alerts
    )
    return hashlib.sha256("\0".join(values).encode("utf-8")).hexdigest()


def process_instance(instance, now=None):
    now = now or timezone.now()
    activity, blocking = db.fetch_activity(instance)
    seen_keys = set()
    config = NotificationSettings.load()
    threshold = config.threshold_seconds

    # PostgreSQL can return several rows for one backend pair. Collapse those
    # rows before updating state so one real incident produces one alert.
    unique_blocking = {}
    for row in blocking:
        key = lock_key(row)
        current = unique_blocking.get(key)
        if current is None or int(row.get("waiting_seconds") or 0) > int(current.get("waiting_seconds") or 0):
            unique_blocking[key] = row

    for key, row in unique_blocking.items():
        seen_keys.add(key)
        with transaction.atomic():
            # Lock the incident row while deciding whether to notify. This
            # keeps a manually-run monitor command from racing the embedded
            # monitor and sending the same event twice.
            alert, created = LockAlert.objects.select_for_update().get_or_create(
                instance=instance,
                alert_key=key,
                resolved_at=None,
                defaults={
                    "blocked_pid": row["blocked_pid"],
                    "blocking_pid": row["blocking_pid"],
                    "blocked_user": row.get("blocked_user") or "",
                    "blocking_user": row.get("blocking_user") or "",
                    "blocked_query": row.get("blocked_query") or "",
                    "blocking_query": row.get("blocking_query") or "",
                    "waiting_seconds": max(0, int(row.get("waiting_seconds") or 0)),
                    "first_seen_at": now,
                    "last_seen_at": now,
                },
            )
            if not created:
                alert.last_seen_at = now
                alert.blocked_user = row.get("blocked_user") or alert.blocked_user
                alert.blocking_user = row.get("blocking_user") or alert.blocking_user
                alert.blocked_query = row.get("blocked_query") or alert.blocked_query
                alert.blocking_query = row.get("blocking_query") or alert.blocking_query
                alert.waiting_seconds = max(0, int(row.get("waiting_seconds") or alert.waiting_seconds))
                alert.save(update_fields=["last_seen_at", "blocked_user", "blocking_user", "blocked_query", "blocking_query", "waiting_seconds"])

            wait_age = alert.waiting_seconds
            if wait_age >= threshold and alert.alerted_at is None:
                # Mark the incident as eligible for the aggregate summary.
                # Delivery is handled once below for the whole database.
                alert.alerted_at = now
                alert.save(update_fields=["alerted_at"])

    active_alerts = LockAlert.objects.filter(instance=instance, resolved_at__isnull=True)
    cleared_alerts = []
    for alert in active_alerts:
        if alert.alert_key in seen_keys:
            continue
        alert.resolved_at = now
        if alert.alerted_at is not None:
            cleared_alerts.append(alert)
        alert.save(update_fields=["resolved_at"])

    eligible_alerts = list(
        LockAlert.objects.filter(
            instance=instance,
            resolved_at__isnull=True,
            alerted_at__isnull=False,
        ).order_by("blocked_pid", "blocking_pid")
    )
    current_keys = sorted(alert.alert_key for alert in eligible_alerts)

    with transaction.atomic():
        state, _created = LockNotificationState.objects.select_for_update().get_or_create(instance=instance)
        previous_keys = sorted(state.last_active_keys or [])
        cleared_keys = sorted(set(previous_keys) - set(current_keys))
        current_fingerprint = summary_fingerprint(eligible_alerts)

        if current_fingerprint == state.last_fingerprint:
            return

        # Do not send a zero-lock summary on the first monitor cycle.
        if not previous_keys and not current_keys:
            state.last_fingerprint = current_fingerprint
            state.last_active_keys = []
            state.save(update_fields=["last_fingerprint", "last_active_keys"])
            return

        if not previous_keys and current_keys:
            event = "initial"
        elif previous_keys and not current_keys:
            event = "cleared"
        else:
            event = "update"

        if notify_lock_summary(
            instance,
            eligible_alerts,
            event=event,
            previous_active_keys=previous_keys,
            cleared_keys=cleared_keys,
            sent_at=now,
            cleared_alerts=cleared_alerts,
        ):
            state.last_fingerprint = current_fingerprint
            state.last_active_keys = current_keys
            state.last_sent_at = now
            state.save(update_fields=["last_fingerprint", "last_active_keys", "last_sent_at"])


class Command(BaseCommand):
    help = "Monitor registered databases and notify about locks older than the threshold."

    def add_arguments(self, parser):
        parser.add_argument("--interval", type=int, default=None, help="Seconds between checks (default: 30)")
        parser.add_argument("--once", action="store_true", help="Run one check and exit")

    def handle(self, *args, **options):
        interval = options["interval"]
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
            if interval is None:
                interval = NotificationSettings.load().interval_seconds
            time.sleep(max(1, interval))
