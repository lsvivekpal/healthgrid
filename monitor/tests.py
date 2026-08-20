from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from django.urls import reverse
from datetime import timedelta

from . import db
from .models import AuditLog, LockAlert, RDSInstance
from .management.commands.monitor_locks import lock_key, process_instance

User = get_user_model()


def make_instance(**overrides):
    password = overrides.pop("password", "apppass")
    defaults = dict(
        name="Test DB",
        db_identifier="test-db",
        region="us-east-1",
        host="localhost",
        port=5432,
        db_name="appdb",
        username="appuser",
        ssl_required=False,
    )
    defaults.update(overrides)
    instance = RDSInstance(**defaults)
    instance.set_password(password)
    instance.save()
    return instance


class BlockingQueryParseTests(TestCase):
    """fetch_activity must translate raw pg_stat_activity/pg_locks rows into the
    (activity, blocking) shape templates rely on, without dropping/mangling fields."""

    def test_fetch_activity_returns_activity_and_blocking_rows(self):
        instance = make_instance()
        activity_rows = [{"pid": 101, "usename": "app", "state": "active", "query": "UPDATE t"}]
        blocking_rows = [{"blocked_pid": 101, "blocking_pid": 202, "blocking_query": "UPDATE t"}]

        fake_cursor = _FakeCursor([activity_rows, blocking_rows])
        fake_conn = _FakeConnection(fake_cursor)

        with patch.object(db, "get_connection", return_value=fake_conn):
            activity, blocking = db.fetch_activity(instance)

        self.assertEqual(activity, activity_rows)
        self.assertEqual(blocking, blocking_rows)
        self.assertTrue(fake_conn.closed)


class _FakeCursor:
    def __init__(self, result_sets):
        self._result_sets = list(result_sets)
        self._current = None

    def execute(self, query, params=None):
        self._current = self._result_sets.pop(0)

    def fetchall(self):
        return self._current

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor
        self.closed = False
        self.autocommit = False

    def set_session(self, **kwargs):
        pass

    def cursor(self, cursor_factory=None):
        return self._cursor

    def close(self):
        self.closed = True


class KillSessionPermissionTests(TestCase):
    def setUp(self):
        self.instance = make_instance()
        self.staff = User.objects.create_user("staffer", password="pw", is_staff=True)
        self.regular = User.objects.create_user("regular", password="pw", is_staff=False)

    def test_non_staff_gets_403(self):
        self.client.login(username="regular", password="pw")
        resp = self.client.post(
            reverse("instance-kill", args=[self.instance.pk]),
            {"pid": 999, "query": "SELECT 1"},
        )
        self.assertEqual(resp.status_code, 403)
        self.assertFalse(AuditLog.objects.filter(action="kill_session").exists())

    def test_staff_kill_creates_audit_log(self):
        self.client.login(username="staffer", password="pw")
        with patch.object(db, "kill_pid", return_value=True) as mock_kill:
            resp = self.client.post(
                reverse("instance-kill", args=[self.instance.pk]),
                {"pid": 555, "query": "SELECT 1"},
            )
        mock_kill.assert_called_once_with(self.instance, 555)
        self.assertEqual(resp.status_code, 302)
        log = AuditLog.objects.get(action="kill_session")
        self.assertEqual(log.target_pid, 555)
        self.assertEqual(log.result, "success")
        self.assertEqual(log.performed_by, self.staff)

    def test_anonymous_redirected_to_login(self):
        resp = self.client.post(
            reverse("instance-kill", args=[self.instance.pk]),
            {"pid": 555, "query": "SELECT 1"},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login/", resp.url)


class AddInstanceAuditTests(TestCase):
    def test_add_instance_writes_audit_log(self):
        staff = User.objects.create_user("staffer2", password="pw", is_staff=True)
        self.client.login(username="staffer2", password="pw")
        resp = self.client.post(reverse("instance-add"), {
            "name": "New DB",
            "db_identifier": "new-db",
            "region": "us-east-1",
            "host": "localhost",
            "port": 5432,
            "db_name": "appdb",
            "username": "appuser",
            "password": "apppass",
        })
        self.assertEqual(resp.status_code, 302)
        instance = RDSInstance.objects.get(db_identifier="new-db")
        log = AuditLog.objects.get(action="add_instance", instance=instance)
        self.assertEqual(log.performed_by, staff)


class LockMonitorTests(TestCase):
    def test_alerts_after_threshold_and_notifies_when_cleared(self):
        instance = make_instance(owner_teams_webhook_url="https://owner.example/webhook")
        now = timezone.now()
        row = {
            "blocked_pid": 101,
            "blocking_pid": 202,
            "blocked_query": "UPDATE orders SET status = 'x'",
            "blocking_query": "ALTER TABLE orders",
            "blocked_query_start": now - timedelta(seconds=121),
        }
        with patch("monitor.management.commands.monitor_locks.db.fetch_activity", return_value=([], [row])), \
             patch("monitor.management.commands.monitor_locks.notify_lock", return_value=True) as notify:
            alert = LockAlert.objects.create(
                instance=instance,
                alert_key=lock_key(row),
                blocked_pid=row["blocked_pid"],
                blocking_pid=row["blocking_pid"],
                blocked_query=row["blocked_query"],
                blocking_query=row["blocking_query"],
                first_seen_at=now - timedelta(seconds=121),
                last_seen_at=now - timedelta(seconds=30),
            )
            process_instance(instance, now=now)

        alert.refresh_from_db()
        self.assertIsNotNone(alert.alerted_at)
        notify.assert_called_once_with(instance, alert)

        with patch("monitor.management.commands.monitor_locks.db.fetch_activity", return_value=([], [])), \
             patch("monitor.management.commands.monitor_locks.notify_lock", return_value=True) as notify:
            process_instance(instance, now=now + timedelta(seconds=30))

        alert.refresh_from_db()
        self.assertIsNotNone(alert.resolved_at)
        notify.assert_called_once_with(instance, alert, resolved=True)
