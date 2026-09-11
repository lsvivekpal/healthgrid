import json
from unittest.mock import patch

import pyotp
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone

from . import db
from .mfa import MFA_VERIFIED_AT_SESSION_KEY
from .models import AuditLog, RDSInstance, UserMFA


class AdministratorActionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        users = get_user_model()
        cls.administrator = users.objects.create_superuser("administrator", password="test-password")
        # A username or a staff flag alone must not grant Administrator access.
        cls.operator = users.objects.create_user("superadmin", is_staff=True)
        cls.viewer = users.objects.create_user("viewer")
        cls.secret = pyotp.random_base32()
        for user in [cls.administrator, cls.operator, cls.viewer]:
            profile = UserMFA(user=user, enabled=True)
            profile.set_secret(cls.secret)
            profile.save()
        cls.instance = RDSInstance.objects.create(
            name="Example DB", db_identifier="example", region="ap-south-1",
            host="example.invalid", db_name="example", username="reader",
        )

    def setUp(self):
        cache.clear()
        self.remove_url = reverse("instance-remove", args=[self.instance.pk])
        self.drop_url = reverse("instance-drop-replication-slot", args=[self.instance.pk])
        self.api_url = reverse("api-instance-detail", args=[self.instance.pk])

    def code(self):
        return pyotp.TOTP(self.secret).now()

    def login_recently_verified(self, user):
        self.client.force_login(user)
        session = self.client.session
        session[MFA_VERIFIED_AT_SESSION_KEY] = timezone.now().timestamp()
        session.save()
        # Signed-cookie sessions require the updated signed value in the client.
        from django.conf import settings
        self.client.cookies[settings.SESSION_COOKIE_NAME] = session.session_key

    def assert_instance_exists(self):
        self.assertTrue(RDSInstance.objects.filter(pk=self.instance.pk).exists())

    def test_operator_and_viewer_cannot_remove_or_drop_even_with_mfa(self):
        for user in [self.operator, self.viewer]:
            self.login_recently_verified(user)
            with self.subTest(user=user.username), patch.object(db, "drop_replication_slot") as drop:
                payload = {"mfa_code": self.code(), "slot_name": "test_slot"}
                self.assertEqual(self.client.post(self.remove_url, payload).status_code, 403)
                self.assertEqual(self.client.post(self.drop_url, payload).status_code, 403)
                response = self.client.delete(self.api_url, json.dumps(payload), content_type="application/json")
                self.assertEqual(response.status_code, 403)
                drop.assert_not_called()
                self.assert_instance_exists()

    def test_anonymous_requests_cannot_perform_administrator_actions(self):
        with patch.object(db, "drop_replication_slot") as drop:
            self.assertEqual(self.client.post(self.remove_url).status_code, 302)
            self.assertEqual(self.client.post(self.drop_url).status_code, 302)
            self.assertEqual(self.client.delete(self.api_url).status_code, 403)
            drop.assert_not_called()
        self.assert_instance_exists()

    def test_remove_requires_action_code_even_after_recent_login_mfa(self):
        self.login_recently_verified(self.administrator)
        for code in ["", "invalid"]:
            response = self.client.post(self.remove_url, {"mfa_code": code})
            self.assertRedirects(response, reverse("instance-detail", args=[self.instance.pk]), fetch_redirect_response=False)
            self.assert_instance_exists()
        self.assertEqual(AuditLog.objects.filter(action="remove_instance", result="failed").count(), 2)

    def test_administrator_can_remove_with_valid_action_code(self):
        self.client.force_login(self.administrator)
        response = self.client.post(self.remove_url, {"mfa_code": self.code()})
        self.assertRedirects(response, reverse("instance-list"), fetch_redirect_response=False)
        self.assertFalse(RDSInstance.objects.filter(pk=self.instance.pk).exists())
        audit = AuditLog.objects.get(action="remove_instance")
        self.assertEqual(audit.result, "success")
        self.assertEqual(audit.performed_by, self.administrator)
        self.assertEqual(audit.instance_name, "Example DB")

    def test_drop_requires_action_code_even_after_recent_login_mfa(self):
        self.login_recently_verified(self.administrator)
        with patch.object(db, "drop_replication_slot") as drop:
            for code in ["", "invalid"]:
                response = self.client.post(self.drop_url, {"slot_name": "test_slot", "mfa_code": code})
                self.assertEqual(response.status_code, 302)
            drop.assert_not_called()
        self.assertEqual(AuditLog.objects.filter(action="drop_replication_slot", result="failed").count(), 2)

    def test_administrator_can_drop_with_valid_action_code(self):
        self.client.force_login(self.administrator)
        with patch.object(db, "drop_replication_slot", return_value=(True, None)) as drop:
            response = self.client.post(self.drop_url, {"slot_name": "test_slot", "mfa_code": self.code()})
            self.assertEqual(response.status_code, 302)
            drop.assert_called_once_with(self.instance, "test_slot")
        self.assertEqual(AuditLog.objects.get(action="drop_replication_slot").performed_by, self.administrator)

    def test_api_requires_code_not_only_recent_login(self):
        self.login_recently_verified(self.administrator)
        for code in ["", "invalid", 123456]:
            response = self.client.delete(self.api_url, json.dumps({"mfa_code": code}), content_type="application/json")
            self.assertEqual(response.status_code, 403)
            self.assert_instance_exists()

    def test_api_administrator_can_delete_with_valid_code(self):
        self.client.force_login(self.administrator)
        response = self.client.delete(self.api_url, json.dumps({"mfa_code": self.code()}), content_type="application/json")
        self.assertEqual(response.status_code, 204)
        self.assertFalse(RDSInstance.objects.filter(pk=self.instance.pk).exists())
        self.assertEqual(AuditLog.objects.get(action="remove_instance").result, "success")

    def test_disabled_mfa_cannot_authorize_either_action(self):
        UserMFA.objects.filter(user=self.administrator).update(enabled=False)
        self.login_recently_verified(self.administrator)
        with patch.object(db, "drop_replication_slot") as drop:
            payload = {"slot_name": "test_slot", "mfa_code": self.code()}
            self.client.post(self.remove_url, payload)
            self.client.post(self.drop_url, payload)
            self.assertEqual(self.client.delete(self.api_url, json.dumps(payload), content_type="application/json").status_code, 403)
            drop.assert_not_called()
        self.assert_instance_exists()

    def test_get_cannot_remove_or_drop(self):
        self.client.force_login(self.administrator)
        self.assertEqual(self.client.get(self.remove_url).status_code, 405)
        self.assertEqual(self.client.get(self.drop_url).status_code, 405)
        self.assert_instance_exists()

    def test_admin_cannot_bypass_dashboard_mfa_via_delete_or_bulk_delete(self):
        self.login_recently_verified(self.administrator)
        response = self.client.post(reverse("admin:monitor_rdsinstance_delete", args=[self.instance.pk]), {"post": "yes"})
        self.assertEqual(response.status_code, 403)
        request = RequestFactory().get("/admin/monitor/rdsinstance/")
        request.user = self.administrator
        model_admin = admin.site._registry[RDSInstance]
        self.assertFalse(model_admin.has_delete_permission(request, self.instance))
        self.assertNotIn("delete_selected", model_admin.get_actions(request))
        self.assert_instance_exists()

    def test_ui_only_shows_destructive_controls_to_administrator(self):
        for user in [self.operator, self.viewer, self.administrator]:
            self.client.force_login(user)
            detail = self.client.get(reverse("instance-detail", args=[self.instance.pk]))
            with patch.object(db, "fetch_replication_slots", return_value=[{"slot_name": "test_slot", "active": False}]):
                slots = self.client.get(reverse("instance-replication-slots", args=[self.instance.pk]))
            if user.is_superuser:
                self.assertContains(detail, f'action="{self.remove_url}" data-requires-mfa="true"')
                self.assertContains(slots, f'action="{self.drop_url}" data-requires-mfa="true"')
            else:
                self.assertNotContains(detail, f'action="{self.remove_url}"')
                self.assertNotContains(slots, f'action="{self.drop_url}"')
