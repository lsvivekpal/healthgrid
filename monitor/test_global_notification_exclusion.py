from datetime import datetime, timezone as dt_timezone
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from .models import LockAlert, NotificationSettings, RDSInstance
from .notifications import notify_lock_summary, send_weekly_reports


class GlobalNotificationExclusionTests(TestCase):
    def setUp(self):
        self.staff = get_user_model().objects.create_user("operator", is_staff=True)
        self.included = self.make_instance("Included DB", "included")
        self.excluded = self.make_instance("Excluded DB", "excluded", exclude_from_global_notifications=True)
        NotificationSettings.objects.create(pk=1, channel_webhook_url="https://channel.example/webhook")

    def make_instance(self, name, identifier, **overrides):
        instance = RDSInstance(
            name=name,
            db_identifier=identifier,
            region="ap-south-1",
            host="example.invalid",
            db_name="appdb",
            username="reader",
            **overrides,
        )
        instance.set_password("password")
        instance.save()
        return instance

    def make_alert(self, instance):
        now = datetime.now(dt_timezone.utc)
        return LockAlert.objects.create(
            instance=instance,
            alert_key=f"{instance.pk}:101:202",
            blocked_pid=101,
            blocking_pid=202,
            first_seen_at=now,
            last_seen_at=now,
            alerted_at=now,
        )

    @patch("monitor.notifications._post_webhook", return_value=True)
    def test_lock_summary_skips_global_webhook_but_keeps_owner_webhook(self, post):
        self.excluded.owner_teams_webhook_url = "https://owner.example/webhook"
        self.excluded.save(update_fields=["owner_teams_webhook_url"])
        alert = self.make_alert(self.excluded)
        self.assertTrue(notify_lock_summary(
            self.excluded, [alert], event="initial", previous_active_keys=[],
            cleared_keys=[], sent_at=alert.first_seen_at,
        ))
        post.assert_called_once_with("https://owner.example/webhook", post.call_args.args[1])

    @patch("monitor.notifications._post_webhook", return_value=True)
    def test_included_lock_summary_uses_global_webhook(self, post):
        alert = self.make_alert(self.included)
        self.assertTrue(notify_lock_summary(
            self.included, [alert], event="initial", previous_active_keys=[],
            cleared_keys=[], sent_at=alert.first_seen_at,
        ))
        self.assertEqual(post.call_args.args[0], "https://channel.example/webhook")

    @patch("monitor.notifications._post_webhook", return_value=True)
    def test_weekly_global_report_includes_excluded_instance_and_owner_report_remains(self, post):
        self.excluded.owner_teams_webhook_url = "https://owner.example/webhook"
        self.excluded.save(update_fields=["owner_teams_webhook_url"])
        self.make_alert(self.included)
        self.make_alert(self.excluded)
        self.assertTrue(send_weekly_reports())
        urls = [call.args[0] for call in post.call_args_list]
        self.assertEqual(urls, ["https://channel.example/webhook", "https://owner.example/webhook"])
        global_payload = post.call_args_list[0].args[1]
        owner_payload = post.call_args_list[1].args[1]
        self.assertEqual(global_payload["report_rows"], 2)
        self.assertIn("Excluded DB", str(global_payload))
        self.assertIn("Excluded DB", str(owner_payload))

    def test_owner_webhook_form_can_toggle_global_exclusion(self):
        self.client.force_login(self.staff)
        response = self.client.post(
            f"/instances/{self.excluded.pk}/owner-webhook/",
            {"owner_teams_webhook_url": "", "exclude_from_global_notifications": "1"},
        )
        self.assertEqual(response.status_code, 302)
        self.excluded.refresh_from_db()
        self.assertTrue(self.excluded.exclude_from_global_notifications)

    def test_new_instance_defaults_to_global_notifications_enabled(self):
        self.client.force_login(self.staff)
        response = self.client.post("/instances/add/", {
            "name": "New DB", "db_identifier": "new-db", "region": "ap-south-1",
            "host": "example.invalid", "port": 5432, "db_name": "appdb",
            "username": "reader", "password": "password",
        })
        self.assertEqual(response.status_code, 302)
        self.assertFalse(RDSInstance.objects.get(db_identifier="new-db").exclude_from_global_notifications)

    def test_notification_settings_can_manage_global_exclusion_list(self):
        self.client.force_login(self.staff)
        response = self.client.post("/settings/notifications/", {
            "global_scope_submitted": "1",
            "channel_webhook_url": "https://channel.example/webhook",
            "threshold_seconds": 120,
            "interval_seconds": 30,
            "notification_start_time": "09:00",
            "notification_end_time": "21:00",
            "resolved_alert_retention_days": 30,
            "manual_query_threshold_seconds": 60,
            "manual_query_excluded_users": "applms,applos",
            "weekly_report_day": 0,
            "weekly_report_hour": 9,
            "report_base_url": "",
            "excluded_instance_ids": [str(self.excluded.pk)],
        })
        self.assertEqual(response.status_code, 302)
        self.included.refresh_from_db()
        self.excluded.refresh_from_db()
        self.assertFalse(self.included.exclude_from_global_notifications)
        self.assertTrue(self.excluded.exclude_from_global_notifications)
