from unittest.mock import patch

import pyotp
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from django.urls import reverse
from datetime import datetime, time, timedelta, timezone as datetime_timezone

from . import db
from .models import AuditLog, LockAlert, LockReport, LongQueryAlert, NotificationSettings, RDSInstance, UserMFA
from .background import cleanup_monitor_history
from .mfa import confirm_enrollment, new_enrollment
from .management.commands.monitor_locks import lock_key, process_instance
from .notifications import _adaptive_card, _summary_message, _timestamp, lock_notifications_open, send_weekly_reports

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

    @patch("monitor.db.psycopg2.connect")
    def test_control_credentials_are_used_for_database_connections(self, connect):
        instance = make_instance(control_username="db_lock_admin")
        instance.set_control_password("controlpass")
        instance.save(update_fields=["control_password_encrypted"])

        db.get_connection(instance)

        kwargs = connect.call_args.kwargs
        self.assertEqual(kwargs["user"], "db_lock_admin")
        self.assertEqual(kwargs["password"], "controlpass")
        self.assertEqual(kwargs["application_name"], "rds-dashboard-control")
        self.assertIn("statement_timeout=8000", kwargs["options"])


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
        alert = LockAlert.objects.create(
            instance=self.instance,
            alert_key="555:777",
            blocked_pid=555,
            blocking_pid=777,
            first_seen_at=timezone.now(),
            last_seen_at=timezone.now(),
        )
        with patch.object(db, "fetch_activity", return_value=([], [{"blocked_pid": 555, "blocking_pid": 777}])), \
             patch.object(db, "kill_pid", return_value=True) as mock_kill:
            resp = self.client.post(
                reverse("instance-kill-lock", args=[self.instance.pk]),
                {"pid": 555, "query": "SELECT 1"},
            )
        mock_kill.assert_called_once_with(self.instance, 555)
        self.assertEqual(resp.status_code, 302)
        log = AuditLog.objects.get(action="kill_session")
        self.assertEqual(log.target_pid, 555)
        self.assertEqual(log.result, "success")
        self.assertEqual(log.performed_by, self.staff)
        alert.refresh_from_db()
        self.assertEqual(alert.clear_reason, "manual_kill")
        self.assertEqual(alert.cleared_by, "staffer")

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


class MFASecurityTests(TestCase):
    def setUp(self):
        self.instance = make_instance()
        self.staff = User.objects.create_user("mfa-staffer", password="pw", is_staff=True)

    def enroll(self):
        profile = UserMFA.objects.create(user=self.staff)
        secret, _codes = new_enrollment(profile)
        self.assertTrue(confirm_enrollment(profile, pyotp.TOTP(secret).now()))
        return profile, secret

    def test_unenrolled_staff_is_sent_to_setup(self):
        response = self.client.post(reverse("login"), {"username": "mfa-staffer", "password": "pw"})
        self.assertRedirects(response, reverse("mfa-setup"), fetch_redirect_response=False)
        self.assertTrue(response.wsgi_request.user.is_authenticated)
        setup = self.client.get(reverse("mfa-setup"))
        self.assertEqual(setup.status_code, 200)
        self.assertContains(setup, "Authenticator enrollment QR code")

    def test_enabled_staff_login_requires_authenticator_code(self):
        _profile, secret = self.enroll()
        response = self.client.post(reverse("login"), {"username": "mfa-staffer", "password": "pw"})
        self.assertRedirects(response, reverse("mfa-verify"), fetch_redirect_response=False)
        self.assertFalse(response.wsgi_request.user.is_authenticated)

        response = self.client.post(reverse("mfa-verify"), {"code": pyotp.TOTP(secret).now()})
        self.assertRedirects(response, reverse("instance-list"), fetch_redirect_response=False)
        self.assertTrue(response.wsgi_request.user.is_authenticated)

    def test_live_session_kill_requires_mfa(self):
        self.client.login(username="mfa-staffer", password="pw")
        with patch.object(db, "kill_pid") as mock_kill:
            response = self.client.post(reverse("instance-kill", args=[self.instance.pk]), {"pid": 321})
        self.assertRedirects(response, reverse("instance-detail", args=[self.instance.pk]), fetch_redirect_response=False)
        mock_kill.assert_not_called()
        self.assertEqual(AuditLog.objects.get(action="kill_session").result, "failed")

    def test_lock_kill_route_does_not_require_mfa(self):
        self.client.login(username="mfa-staffer", password="pw")
        with patch.object(db, "fetch_activity", return_value=([], [{"blocked_pid": 321, "blocking_pid": 654}])), \
             patch.object(db, "kill_pid", return_value=True) as mock_kill:
            response = self.client.post(reverse("instance-kill-lock", args=[self.instance.pk]), {"pid": 321})
        self.assertEqual(response.status_code, 302)
        mock_kill.assert_called_once_with(self.instance, 321)

    def test_lock_bulk_kill_requires_mfa(self):
        _profile, secret = self.enroll()
        self.client.login(username="mfa-staffer", password="pw")
        payload = {"pid": [321, 654], "mode": "bulk"}
        with patch.object(db, "kill_pids") as mock_kill:
            response = self.client.post(reverse("instance-kill-lock-chain", args=[self.instance.pk]), payload)
        self.assertEqual(response.status_code, 302)
        mock_kill.assert_not_called()

        payload["mfa_code"] = pyotp.TOTP(secret).now()
        with patch.object(db, "fetch_activity", return_value=([], [{"blocked_pid": 321, "blocking_pid": 654}])), \
             patch.object(db, "kill_pids", return_value={321: True, 654: True}) as mock_kill:
            response = self.client.post(reverse("instance-kill-lock-chain", args=[self.instance.pk]), payload)
        self.assertEqual(response.status_code, 302)
        mock_kill.assert_called_once_with(self.instance, [321, 654])


class LockMonitorTests(TestCase):
    def test_duplicate_lock_rows_create_one_incident(self):
        instance = make_instance()
        now = timezone.now()
        first_row = {
            "blocked_pid": 501,
            "blocking_pid": 601,
            "blocked_user": "app",
            "blocking_user": "worker",
            "blocked_query": "UPDATE orders",
            "blocking_query": "ALTER TABLE orders",
            "waiting_seconds": 180,
        }
        duplicate_row = {**first_row, "blocked_query": "UPDATE orders SET status = 'x'"}
        with patch("monitor.management.commands.monitor_locks.db.fetch_activity", return_value=([], [first_row, duplicate_row])), \
             patch("monitor.management.commands.monitor_locks.notify_lock_summary", return_value=True) as notify:
            process_instance(instance, now=now)

        alert = LockAlert.objects.get(instance=instance, alert_key="501:601", resolved_at__isnull=True)
        self.assertEqual(LockAlert.objects.filter(instance=instance, resolved_at__isnull=True).count(), 1)
        notify.assert_called_once()
        self.assertEqual(notify.call_args.kwargs["event"], "initial")
        self.assertEqual(len(notify.call_args.args[1]), 1)

    def test_notifies_once_while_lock_remains(self):
        instance = make_instance()
        now = timezone.now()
        row = {
            "blocked_pid": 301,
            "blocking_pid": 401,
            "blocked_user": "blocked_user",
            "blocking_user": "blocking_user",
            "blocked_query": "UPDATE orders",
            "blocking_query": "ALTER TABLE orders",
            "blocked_query_start": now - timedelta(seconds=180),
            "waiting_seconds": 180,
        }
        with patch("monitor.management.commands.monitor_locks.db.fetch_activity", return_value=([], [row])), \
             patch("monitor.management.commands.monitor_locks.notify_lock_summary", return_value=True) as notify:
            process_instance(instance, now=now)
            process_instance(instance, now=now + timedelta(seconds=30))

        self.assertEqual(notify.call_count, 1)

        with patch("monitor.management.commands.monitor_locks.db.fetch_activity", return_value=([], [row])), \
             patch("monitor.management.commands.monitor_locks.notify_lock_summary", return_value=True) as notify:
            process_instance(instance, now=now + timedelta(seconds=601))

        self.assertEqual(notify.call_count, 0)

    def test_notifies_active_update_when_lock_details_change(self):
        instance = make_instance()
        now = timezone.now()
        row = {
            "blocked_pid": 701,
            "blocking_pid": 801,
            "blocked_user": "app",
            "blocking_user": "worker",
            "blocked_query": "UPDATE orders",
            "blocking_query": "ALTER TABLE orders",
            "waiting_seconds": 180,
        }
        with patch("monitor.management.commands.monitor_locks.db.fetch_activity", return_value=([], [row])), \
             patch("monitor.management.commands.monitor_locks.notify_lock_summary", return_value=True) as notify:
            process_instance(instance, now=now)
        notify.assert_called_once()
        self.assertEqual(notify.call_args.kwargs["event"], "initial")

        changed_row = {**row, "blocked_query": "UPDATE orders SET status = 'blocked'"}
        with patch("monitor.management.commands.monitor_locks.db.fetch_activity", return_value=([], [changed_row])), \
             patch("monitor.management.commands.monitor_locks.notify_lock_summary", return_value=True) as notify:
            process_instance(instance, now=now + timedelta(seconds=30))

        notify.assert_called_once()
        self.assertEqual(notify.call_args.kwargs["event"], "update")

    def test_summarizes_partial_clears_then_sends_one_final_clear(self):
        instance = make_instance()
        now = timezone.now()
        rows = [
            {
                "blocked_pid": pid,
                "blocking_pid": 900,
                "blocked_query": f"UPDATE orders_{pid}",
                "blocking_query": "ALTER TABLE orders",
                "waiting_seconds": 180,
            }
            for pid in (901, 902, 903)
        ]
        with patch("monitor.management.commands.monitor_locks.db.fetch_activity", return_value=([], rows)), \
             patch("monitor.management.commands.monitor_locks.notify_lock_summary", return_value=True) as notify:
            process_instance(instance, now=now)
        notify.assert_called_once()
        self.assertEqual(notify.call_args.kwargs["event"], "initial")
        self.assertEqual(len(notify.call_args.args[1]), 3)

        with patch("monitor.management.commands.monitor_locks.db.fetch_activity", return_value=([], rows[:1])), \
             patch("monitor.management.commands.monitor_locks.notify_lock_summary", return_value=True) as notify:
            process_instance(instance, now=now + timedelta(seconds=30))
        notify.assert_called_once()
        self.assertEqual(notify.call_args.kwargs["event"], "update")
        self.assertEqual(set(notify.call_args.kwargs["cleared_keys"]), {"902:900", "903:900"})

        with patch("monitor.management.commands.monitor_locks.db.fetch_activity", return_value=([], [])), \
             patch("monitor.management.commands.monitor_locks.notify_lock_summary", return_value=True) as notify:
            process_instance(instance, now=now + timedelta(seconds=60))
        notify.assert_called_once()
        self.assertEqual(notify.call_args.kwargs["event"], "cleared")
        self.assertEqual(notify.call_args.kwargs["cleared_keys"], ["901:900"])

    def test_alerts_after_threshold_and_notifies_when_cleared(self):
        instance = make_instance(owner_teams_webhook_url="https://owner.example/webhook")
        now = timezone.now()
        row = {
            "blocked_pid": 101,
            "blocking_pid": 202,
            "blocked_query": "UPDATE orders SET status = 'x'",
            "blocking_query": "ALTER TABLE orders",
            "blocked_query_start": now - timedelta(seconds=121),
            "waiting_seconds": 121,
        }
        with patch("monitor.management.commands.monitor_locks.db.fetch_activity", return_value=([], [row])), \
             patch("monitor.management.commands.monitor_locks.notify_lock_summary", return_value=True) as notify:
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
        notify.assert_called_once()
        self.assertEqual(notify.call_args.kwargs["event"], "initial")

        with patch("monitor.management.commands.monitor_locks.db.fetch_activity", return_value=([], [])), \
             patch("monitor.management.commands.monitor_locks.notify_lock_summary", return_value=True) as notify:
            process_instance(instance, now=now + timedelta(seconds=30))

        alert.refresh_from_db()
        self.assertIsNotNone(alert.resolved_at)
        notify.assert_called_once()
        self.assertEqual(notify.call_args.kwargs["event"], "cleared")


class LongQueryMonitorTests(TestCase):
    def test_notifies_new_long_manual_query_once_and_excludes_application_users(self):
        instance = make_instance()
        now = timezone.now()
        NotificationSettings.objects.create(
            pk=1,
            manual_query_alert_enabled=True,
            manual_query_threshold_seconds=60,
            manual_query_excluded_users="applms,applos",
        )
        query_start = now - timedelta(seconds=75)
        row = {
            "pid": 7001,
            "usename": "dbeaver_user",
            "application_name": "DBeaver",
            "client_addr": "10.0.0.5",
            "state": "active",
            "query": "UPDATE ledger SET status = 'x'",
            "query_start": query_start,
            "duration_seconds": 75,
        }
        excluded = {**row, "pid": 7002, "usename": "applms", "duration_seconds": 120}
        with patch("monitor.management.commands.monitor_locks.db.fetch_activity", return_value=([row, excluded], [])), \
             patch("monitor.management.commands.monitor_locks.notify_long_query", return_value=True) as notify:
            process_instance(instance, now=now)
            process_instance(instance, now=now + timedelta(seconds=30))

        self.assertEqual(LongQueryAlert.objects.filter(instance=instance).count(), 1)
        self.assertEqual(notify.call_count, 1)
        self.assertEqual(notify.call_args.args[0], instance)
        self.assertEqual(notify.call_args.args[1][0].username, "dbeaver_user")

        changed = {**row, "query": "DELETE FROM ledger WHERE id = 1", "query_start": now + timedelta(seconds=30), "duration_seconds": 61}
        with patch("monitor.management.commands.monitor_locks.db.fetch_activity", return_value=([changed], [])), \
             patch("monitor.management.commands.monitor_locks.notify_long_query", return_value=True) as notify:
            process_instance(instance, now=now + timedelta(seconds=91))

        self.assertEqual(LongQueryAlert.objects.filter(instance=instance, resolved_at__isnull=True).count(), 1)
        self.assertEqual(notify.call_count, 1)


class NotificationSettingsTests(TestCase):
    def test_notification_timestamps_are_displayed_in_ist(self):
        value = datetime(2026, 9, 10, 5, 58, 50, tzinfo=datetime_timezone.utc)
        self.assertEqual(_timestamp(value), "2026-09-10 11:28:50 IST")

    def test_staff_can_save_notification_settings(self):
        staff = User.objects.create_user("settings-staff", password="pw", is_staff=True)
        self.client.login(username="settings-staff", password="pw")
        response = self.client.post(reverse("notification-settings"), {
            "channel_webhook_url": "https://teams.example/webhook",
            "threshold_seconds": "180",
            "interval_seconds": "30",
        })
        self.assertEqual(response.status_code, 302)
        config = NotificationSettings.objects.get(pk=1)
        self.assertEqual(config.channel_webhook_url, "https://teams.example/webhook")
        self.assertEqual(config.threshold_seconds, 180)

    def test_staff_can_save_long_teams_webhook_url(self):
        staff = User.objects.create_user("long-webhook-staff", password="pw", is_staff=True)
        self.client.login(username="long-webhook-staff", password="pw")
        long_url = "https://teams.example/webhook?token=" + ("a" * 500)
        response = self.client.post(reverse("notification-settings"), {
            "channel_webhook_url": long_url,
            "threshold_seconds": "120",
            "interval_seconds": "30",
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(NotificationSettings.objects.get(pk=1).channel_webhook_url, long_url)

    def test_staff_can_save_weekly_report_settings(self):
        staff = User.objects.create_user("weekly-settings-staff", password="pw", is_staff=True)
        self.client.login(username="weekly-settings-staff", password="pw")
        response = self.client.post(reverse("notification-settings"), {
            "channel_webhook_url": "https://teams.example/webhook",
            "threshold_seconds": "120",
            "interval_seconds": "30",
            "weekly_report_enabled": "1",
            "weekly_report_day": "4",
            "weekly_report_hour": "10",
            "report_base_url": "https://dashboard.example.com",
            "resolved_alert_retention_days": "45",
        })
        self.assertEqual(response.status_code, 302)
        config = NotificationSettings.objects.get(pk=1)
        self.assertTrue(config.weekly_report_enabled)
        self.assertEqual(config.weekly_report_day, 4)
        self.assertEqual(config.weekly_report_hour, 10)
        self.assertEqual(config.report_base_url, "https://dashboard.example.com")
        self.assertEqual(config.resolved_alert_retention_days, 45)

    def test_staff_can_save_custom_notification_window(self):
        staff = User.objects.create_user("schedule-staff", password="pw", is_staff=True)
        self.client.login(username="schedule-staff", password="pw")
        response = self.client.post(reverse("notification-settings"), {
            "threshold_seconds": "120",
            "interval_seconds": "30",
            "notification_schedule_enabled": "1",
            "notification_start_time": "09:00",
            "notification_end_time": "21:00",
            "resolved_alert_retention_days": "30",
        })
        self.assertEqual(response.status_code, 302)
        config = NotificationSettings.objects.get(pk=1)
        self.assertTrue(config.notification_schedule_enabled)
        self.assertEqual(config.notification_start_time.isoformat(), "09:00:00")
        self.assertEqual(config.notification_end_time.isoformat(), "21:00:00")

    def test_notification_window_uses_ist_and_supports_quiet_hours(self):
        config = NotificationSettings.objects.create(
            pk=1,
            notification_schedule_enabled=True,
            notification_start_time=time(9, 0),
            notification_end_time=time(21, 0),
        )
        morning = datetime(2026, 9, 11, 3, 30, tzinfo=datetime_timezone.utc)  # 09:00 IST
        night = datetime(2026, 9, 11, 16, 0, tzinfo=datetime_timezone.utc)  # 21:30 IST
        self.assertTrue(lock_notifications_open(config, morning))
        self.assertFalse(lock_notifications_open(config, night))


    def test_cleanup_removes_only_old_resolved_alerts_and_expired_reports(self):
        instance = make_instance()
        now = timezone.now()
        NotificationSettings.objects.create(pk=1, resolved_alert_retention_days=30)
        old_resolved = LockAlert.objects.create(
            instance=instance,
            alert_key="1:2",
            blocked_pid=1,
            blocking_pid=2,
            first_seen_at=now - timedelta(days=40),
            last_seen_at=now - timedelta(days=40),
            resolved_at=now - timedelta(days=31),
        )
        recent_resolved = LockAlert.objects.create(
            instance=instance,
            alert_key="3:4",
            blocked_pid=3,
            blocking_pid=4,
            first_seen_at=now - timedelta(days=10),
            last_seen_at=now - timedelta(days=10),
            resolved_at=now - timedelta(days=10),
        )
        active = LockAlert.objects.create(
            instance=instance,
            alert_key="5:6",
            blocked_pid=5,
            blocking_pid=6,
            first_seen_at=now - timedelta(days=100),
            last_seen_at=now,
        )
        expired_report = LockReport.objects.create(
            file_name="expired.csv",
            csv_content="header\n",
            expires_at=now - timedelta(days=1),
        )
        fresh_report = LockReport.objects.create(
            file_name="fresh.csv",
            csv_content="header\n",
            expires_at=now + timedelta(days=1),
        )

        deleted_alerts, deleted_reports = cleanup_monitor_history(now=now)

        self.assertEqual(deleted_alerts, 1)
        self.assertEqual(deleted_reports, 1)
        self.assertFalse(LockAlert.objects.filter(pk=old_resolved.pk).exists())
        self.assertTrue(LockAlert.objects.filter(pk=recent_resolved.pk).exists())
        self.assertTrue(LockAlert.objects.filter(pk=active.pk).exists())
        self.assertFalse(LockReport.objects.filter(pk=expired_report.pk).exists())
        self.assertTrue(LockReport.objects.filter(pk=fresh_report.pk).exists())

    @patch("monitor.notifications._post_webhook", return_value=True)
    def test_weekly_report_uses_flow_envelope_and_csv(self, post_webhook):
        instance = make_instance()
        now = timezone.now()
        LockAlert.objects.create(
            instance=instance,
            alert_key="11:22",
            blocked_pid=11,
            blocking_pid=22,
            blocked_user="blocked_user",
            blocking_user="blocking_user",
            blocked_query="UPDATE orders",
            blocking_query="ALTER TABLE orders",
            waiting_seconds=180,
            first_seen_at=now - timedelta(hours=2),
            last_seen_at=now - timedelta(minutes=1),
            alerted_at=now - timedelta(hours=1),
        )
        NotificationSettings.objects.create(pk=1, channel_webhook_url="https://teams.example/webhook")

        self.assertTrue(send_weekly_reports(now=now))
        payload = post_webhook.call_args.args[1]
        self.assertEqual(payload["event_type"], "weekly_report")
        self.assertEqual(payload["file_name"].endswith(".csv"), True)
        self.assertIn("blocked_query", payload["csv_content"])
        self.assertIn("UPDATE orders", payload["csv_content"])

    def test_cleared_card_highlights_manual_kill_operator(self):
        instance = make_instance()
        now = timezone.now()
        alert = LockAlert.objects.create(
            instance=instance,
            alert_key="11:22",
            blocked_pid=11,
            blocking_pid=22,
            blocked_query="UPDATE orders",
            blocking_query="ALTER TABLE orders",
            first_seen_at=now - timedelta(minutes=5),
            last_seen_at=now - timedelta(minutes=1),
            alerted_at=now - timedelta(minutes=4),
            resolved_at=now,
            clear_reason="manual_kill",
            cleared_by="staffer",
        )
        message = _summary_message(instance, [], "cleared", ["11:22"], ["11:22"], now, [alert])
        self.assertIn("MANUAL", message)
        self.assertIn("staffer", message)
        card = _adaptive_card(message, cleared_alerts=[alert])
        card_text = str(card)
        self.assertIn("MANUALLY KILLED", card_text)
        self.assertIn("staffer", card_text)

    def test_non_staff_cannot_access_notification_settings(self):
        User.objects.create_user("settings-user", password="pw", is_staff=False)
        self.client.login(username="settings-user", password="pw")
        response = self.client.get(reverse("notification-settings"))
        self.assertEqual(response.status_code, 403)

    @patch("monitor.views.send_test_notification", return_value=(True, "Test notification sent."))
    def test_staff_can_trigger_shared_channel_test(self, send_test):
        User.objects.create_user("test-staff", password="pw", is_staff=True)
        self.client.login(username="test-staff", password="pw")
        NotificationSettings.objects.create(pk=1, channel_webhook_url="https://teams.example/webhook")
        response = self.client.post(reverse("test-notification"), {
            "destination": "shared channel",
            "next": "notification-settings",
        })
        self.assertEqual(response.status_code, 302)
        send_test.assert_called_once_with("https://teams.example/webhook", "shared channel")

    def test_staff_can_update_existing_instance_owner_webhook(self):
        staff = User.objects.create_user("webhook-staff", password="pw", is_staff=True)
        instance = make_instance()
        self.client.login(username="webhook-staff", password="pw")
        response = self.client.post(reverse("instance-owner-webhook", args=[instance.pk]), {
            "owner_teams_webhook_url": "https://owner.example/webhook",
        })
        self.assertEqual(response.status_code, 302)
        instance.refresh_from_db()
        self.assertEqual(instance.owner_teams_webhook_url, "https://owner.example/webhook")

    @patch("monitor.views.send_test_notification", return_value=(True, "Test notification sent."))
    def test_staff_can_test_existing_instance_owner_webhook(self, send_test):
        User.objects.create_user("test-owner-webhook", password="pw", is_staff=True)
        instance = make_instance(owner_teams_webhook_url="https://owner.example/webhook")
        self.client.login(username="test-owner-webhook", password="pw")
        response = self.client.post(reverse("test-notification"), {
            "destination": "owner chat",
            "webhook_url": instance.owner_teams_webhook_url,
            "next": "instance-detail",
            "instance_id": instance.pk,
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("instance-detail", args=[instance.pk]))
        send_test.assert_called_once_with("https://owner.example/webhook", "owner chat")


class UserManagementTests(TestCase):
    def test_superuser_can_create_read_only_user(self):
        User.objects.create_superuser("root-admin", "root@example.com", "Strong-password-1")
        self.client.login(username="root-admin", password="Strong-password-1")
        response = self.client.post(reverse("user-management"), {
            "username": "viewer",
            "email": "viewer@example.com",
            "password": "Strong-password-2",
            "role": "readonly",
        })
        self.assertEqual(response.status_code, 302)
        viewer = User.objects.get(username="viewer")
        self.assertFalse(viewer.is_staff)
