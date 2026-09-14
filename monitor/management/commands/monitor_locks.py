import logging
import hashlib
import time

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from monitor import db
from monitor.models import LockAlert, LockNotificationState, LongQueryAlert, NotificationSettings, RDSInstance
from monitor.notifications import lock_notifications_open, notify_lock_summary, notify_long_query


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


def long_query_key(row):
    values = (
        row.get("pid"),
        row.get("usename") or "",
        row.get("query_start") or "",
        row.get("query") or "",
    )
    fingerprint = hashlib.sha256("\0".join(str(value) for value in values).encode("utf-8")).hexdigest()
    return f"{row.get('pid')}:{fingerprint}"


def process_long_queries(instance, activity, config, now):
    """Track and notify once for long active queries from non-application users."""
    if not config.manual_query_alert_enabled:
        LongQueryAlert.objects.filter(instance=instance, resolved_at__isnull=True).update(resolved_at=now)
        return

    excluded_users = {
        username.strip().casefold()
        for username in (config.manual_query_excluded_users or "").split(",")
        if username.strip()
    }
    candidates = {}
    for row in activity:
        username = str(row.get("usename") or "").strip()
        application_name = str(row.get("application_name") or "").strip()
        if row.get("state") != "active" or not username or username.casefold() in excluded_users:
            continue
        if application_name.casefold() == "healthgrid-control":
            continue
        if int(row.get("duration_seconds") or 0) < config.manual_query_threshold_seconds:
            continue
        candidates[long_query_key(row)] = row

    seen_keys = set(candidates)
    new_alerts = []
    for key, row in candidates.items():
        with transaction.atomic():
            alert, created = LongQueryAlert.objects.select_for_update().get_or_create(
                instance=instance,
                alert_key=key,
                resolved_at=None,
                defaults={
                    "pid": row["pid"],
                    "username": row.get("usename") or "",
                    "application_name": row.get("application_name") or "",
                    "client_addr": str(row.get("client_addr") or ""),
                    "query": row.get("query") or "",
                    "query_start": row.get("query_start"),
                    "duration_seconds": max(0, int(row.get("duration_seconds") or 0)),
                    "first_seen_at": now,
                    "last_seen_at": now,
                },
            )
            if not created:
                alert.last_seen_at = now
                alert.duration_seconds = max(0, int(row.get("duration_seconds") or alert.duration_seconds))
                alert.save(update_fields=["last_seen_at", "duration_seconds"])
            if alert.alerted_at is None:
                new_alerts.append(alert)

    LongQueryAlert.objects.filter(
        instance=instance,
        resolved_at__isnull=True,
    ).exclude(alert_key__in=seen_keys).update(resolved_at=now)

    if new_alerts and lock_notifications_open(config, now):
        if notify_long_query(instance, new_alerts, sent_at=now):
            LongQueryAlert.objects.filter(pk__in=[alert.pk for alert in new_alerts]).update(alerted_at=now)


def _upsert_lock_alerts(instance, unique_blocking, threshold, now):
    """Persist one database snapshot in one transaction.

    A lock storm can contain hundreds of PID pairs. Keeping one transaction
    for the whole snapshot avoids hundreds of individual commit/fsync cycles,
    which used to make the embedded web process appear unhealthy under load.
    """
    seen_keys = set()
    with transaction.atomic():
        for key, row in unique_blocking.items():
            seen_keys.add(key)
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

            if alert.waiting_seconds >= threshold and alert.alerted_at is None:
                alert.alerted_at = now
                alert.save(update_fields=["alerted_at"])
    return seen_keys


def process_instance(instance, now=None):
    now = now or timezone.now()
    activity, blocking = db.fetch_activity(instance)
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

    seen_keys = _upsert_lock_alerts(instance, unique_blocking, threshold, now)

    active_alerts = LockAlert.objects.filter(instance=instance, resolved_at__isnull=True)
    cleared_alerts = []
    to_clear = []
    for alert in active_alerts:
        if alert.alert_key in seen_keys:
            continue
        alert.resolved_at = now
        if not alert.clear_reason:
            alert.clear_reason = "auto_clear"
        if alert.alerted_at is not None:
            cleared_alerts.append(alert)
        to_clear.append(alert)
    if to_clear:
        clear_ids = [alert.pk for alert in to_clear]
        LockAlert.objects.filter(pk__in=clear_ids).update(resolved_at=now)
        LockAlert.objects.filter(pk__in=clear_ids, clear_reason="").update(clear_reason="auto_clear")

    eligible_alerts = list(
        LockAlert.objects.filter(
            instance=instance,
            resolved_at__isnull=True,
            alerted_at__isnull=False,
        ).order_by("blocked_pid", "blocking_pid")
    )
    current_keys = sorted(alert.alert_key for alert in eligible_alerts)

    process_long_queries(instance, activity, config, now)

    with transaction.atomic():
        state, _created = LockNotificationState.objects.select_for_update().get_or_create(instance=instance)
        previous_keys = sorted(state.last_active_keys or [])
        cleared_keys = sorted(set(previous_keys) - set(current_keys))
        current_fingerprint = summary_fingerprint(eligible_alerts)
        all_active_alerts = list(
            LockAlert.objects.filter(instance=instance, resolved_at__isnull=True)
            .order_by("blocked_pid", "blocking_pid")
        )

        if not lock_notifications_open(config, now):
            # Keep the current set for the next daytime comparison, but clear
            # the fingerprint so a lock that survives the quiet window gets a
            # fresh daytime summary instead of being treated as unchanged.
            state.last_fingerprint = ""
            state.last_active_keys = current_keys
            state.save(update_fields=["last_fingerprint", "last_active_keys"])
            return

        storm_threshold = config.lock_storm_threshold
        if not all_active_alerts:
            if state.storm_active:
                state.storm_active = False
                state.save(update_fields=["storm_active"])
        elif storm_threshold and len(all_active_alerts) >= storm_threshold:
            if not state.storm_active:
                if notify_lock_summary(
                    instance,
                    all_active_alerts,
                    event="storm",
                    previous_active_keys=previous_keys,
                    cleared_keys=[],
                    sent_at=now,
                ):
                    state.storm_active = True
                    state.last_fingerprint = current_fingerprint
                    state.last_active_keys = current_keys
                    state.last_sent_at = now
                    state.save(update_fields=["storm_active", "last_fingerprint", "last_active_keys", "last_sent_at"])
            return
        elif state.storm_active:
            # Suppress ordinary change cards while a previously reported storm
            # is still active. The dashboard and weekly CSV retain all detail.
            return

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
