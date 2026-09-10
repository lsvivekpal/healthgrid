import logging
import threading
import time
import uuid
from datetime import timedelta

from django.db import close_old_connections, transaction
from django.utils import timezone

from . import db
from .management.commands.monitor_locks import process_instance
from .models import MonitorLease, NotificationSettings, RDSInstance


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


def monitor_loop():
    owner_id = uuid.uuid4().hex
    while True:
        try:
            close_old_connections()
            if acquire_monitor_lease(owner_id):
                run_monitor_cycle()
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
