import logging
import threading
import time
import uuid
from datetime import timedelta

from django.db import close_old_connections, transaction
from django.utils import timezone

from . import db
from .management.commands.monitor_locks import process_instance
from .models import LockAlert, LockReport, MonitorLease, NotificationSettings, RDSInstance
from .notifications import DISPLAY_TIMEZONE, send_weekly_reports


logger = logging.getLogger(__name__)


def acquire_monitor_lease(owner_id):
    """Return True for the one process allowed to run the current monitor cycle."""
    now = timezone.now()
    interval = NotificationSettings.load().interval_seconds
    lease_until = now + timedelta(seconds=max(60, interval * 2))
    with transaction.atomic():
        lease, _created = MonitorLease.objects.select_for_update().get_or_create(pk=1)
        if lease.owner_id and lease.owner_id != owner_id and lease.lease_until > now:
            return False
        lease.owner_id = owner_id
        lease.lease_until = lease_until
        lease.save(update_fields=["owner_id", "lease_until"])
        return True


def run_monitor_cycle():
    for instance in RDSInstance.objects.filter(is_active=True):
        try:
            process_instance(instance)
        except db.ConnectionError as exc:
            logger.warning("Could not check locks for %s: %s", instance, exc)
        except Exception:
            logger.exception("Unexpected lock monitor failure for %s", instance)


def maybe_send_weekly_report(now=None):
    now = now or timezone.now()
    config = NotificationSettings.load()
    if not config.weekly_report_enabled:
        return False

    local_now = timezone.localtime(now, DISPLAY_TIMEZONE)
    if local_now.weekday() != config.weekly_report_day or local_now.hour < config.weekly_report_hour:
        return False

    scheduled_week = local_now.isocalendar()[:2]
    if config.last_weekly_report_at:
        last_local = timezone.localtime(config.last_weekly_report_at, DISPLAY_TIMEZONE)
        if last_local.isocalendar()[:2] == scheduled_week:
            return False

    delivered = send_weekly_reports(now)
    if delivered:
        config.last_weekly_report_at = now
        config.save(update_fields=["last_weekly_report_at", "updated_at"])
    return delivered


def cleanup_monitor_history(now=None):
    """Remove only old resolved incidents and expired temporary report payloads."""
    now = now or timezone.now()
    with transaction.atomic():
        config = NotificationSettings.objects.select_for_update().get(pk=1)
        if config.last_cleanup_at and now - config.last_cleanup_at < timedelta(hours=24):
            return 0, 0

        alert_cutoff = now - timedelta(days=max(1, config.resolved_alert_retention_days))
        deleted_alerts, _details = LockAlert.objects.filter(
            resolved_at__isnull=False,
            resolved_at__lt=alert_cutoff,
        ).delete()
        deleted_reports, _details = LockReport.objects.filter(expires_at__lt=now).delete()
        config.last_cleanup_at = now
        config.save(update_fields=["last_cleanup_at", "updated_at"])
    return deleted_alerts, deleted_reports


def monitor_loop():
    owner_id = uuid.uuid4().hex
    while True:
        try:
            close_old_connections()
            if acquire_monitor_lease(owner_id):
                run_monitor_cycle()
                maybe_send_weekly_report()
                cleanup_monitor_history()
            interval = NotificationSettings.load().interval_seconds
        except Exception:
            logger.exception("Lock monitor loop failed")
            interval = 30
        finally:
            close_old_connections()
        time.sleep(max(5, interval))


def start_monitor_thread():
    thread = threading.Thread(target=monitor_loop, name="rds-lock-monitor", daemon=True)
    thread.start()
    logger.info("Started embedded RDS lock monitor")
