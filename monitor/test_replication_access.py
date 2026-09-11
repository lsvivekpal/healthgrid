from unittest.mock import MagicMock, patch

import pyotp
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from . import db
from .models import AuditLog, RDSInstance, ReplicationSlotAccess, UserMFA
from .permissions import can_manage_replication_slot


class ReplicationAccessTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        users = get_user_model()
        cls.administrator = users.objects.create_superuser("administrator", password="test-password")
        cls.operator = users.objects.create_user("operator", is_staff=True)
        cls.viewer = users.objects.create_user("viewer")
        cls.secret = pyotp.random_base32()
        for user in [cls.administrator, cls.operator]:
            profile = UserMFA(user=user, enabled=True)
            profile.set_secret(cls.secret)
            profile.save()
        cls.instance = RDSInstance.objects.create(
            name="Sample", db_identifier="sample", host="example.invalid", db_name="sample", username="reader",
        )

    def setUp(self):
        cache.clear()
        self.kill_url = reverse("instance-kill-replication-slot", args=[self.instance.pk])
        self.drop_url = reverse("instance-drop-replication-slot", args=[self.instance.pk])
        self.manage_url = reverse("user-management")

    def payload(self):
        return {"slot_name": "test_slot", "mfa_code": pyotp.TOTP(self.secret).now()}

    def test_replication_actions_are_denied_by_default(self):
        self.assertFalse(ReplicationSlotAccess.objects.exists())
        for user in [self.operator, self.viewer]:
            self.client.force_login(user)
            with patch.object(db, "terminate_replication_slot_backend") as terminate, patch.object(db, "drop_replication_slot") as drop:
                self.assertEqual(self.client.post(self.kill_url, self.payload()).status_code, 403)
                self.assertEqual(self.client.post(self.drop_url, self.payload()).status_code, 403)
                terminate.assert_not_called()
                drop.assert_not_called()

    def test_administrator_retains_both_actions_without_grants(self):
        self.client.force_login(self.administrator)
        with patch.object(db, "terminate_replication_slot_backend", return_value=True) as terminate, patch.object(db, "drop_replication_slot", return_value=(True, None)) as drop:
            self.assertEqual(self.client.post(self.kill_url, self.payload()).status_code, 302)
            self.assertEqual(self.client.post(self.drop_url, self.payload()).status_code, 302)
            terminate.assert_called_once_with(self.instance, "test_slot")
            drop.assert_called_once_with(self.instance, "test_slot")

    def test_drop_grant_does_not_grant_termination_and_still_requires_mfa(self):
        ReplicationSlotAccess.objects.create(user=self.operator, can_drop=True)
        self.client.force_login(self.operator)
        with patch.object(db, "drop_replication_slot", return_value=(True, None)) as drop, patch.object(db, "terminate_replication_slot_backend") as terminate:
            self.client.post(self.drop_url, {"slot_name": "test_slot"})
            drop.assert_not_called()
            self.client.post(self.drop_url, self.payload())
            drop.assert_called_once_with(self.instance, "test_slot")
            self.assertEqual(self.client.post(self.kill_url, self.payload()).status_code, 403)
            terminate.assert_not_called()

    def test_termination_grant_does_not_grant_drop_or_instance_removal(self):
        ReplicationSlotAccess.objects.create(user=self.operator, can_terminate=True)
        self.client.force_login(self.operator)
        with patch.object(db, "terminate_replication_slot_backend", return_value=True) as terminate, patch.object(db, "drop_replication_slot") as drop:
            self.client.post(self.kill_url, self.payload())
            terminate.assert_called_once_with(self.instance, "test_slot")
            self.assertEqual(self.client.post(self.drop_url, self.payload()).status_code, 403)
            self.assertEqual(self.client.post(reverse("instance-remove", args=[self.instance.pk]), self.payload()).status_code, 403)
            drop.assert_not_called()

    def test_administrator_can_grant_and_revoke_existing_operator_access(self):
        self.client.force_login(self.administrator)
        data = {"action": "update_slot_access", "user_id": self.operator.pk, "can_drop_slots": "1", "can_terminate_slots": "1"}
        self.assertEqual(self.client.post(self.manage_url, data).status_code, 302)
        access = ReplicationSlotAccess.objects.get(user=self.operator)
        self.assertTrue(access.can_drop and access.can_terminate)
        self.client.force_login(self.operator)
        with patch.object(db, "terminate_replication_slot_backend", return_value=True) as terminate:
            self.client.post(self.kill_url, self.payload())
            terminate.assert_called_once()
        self.client.force_login(self.administrator)
        self.client.post(self.manage_url, {"action": "update_slot_access", "user_id": self.operator.pk})
        access.refresh_from_db()
        self.assertFalse(access.can_drop or access.can_terminate)
        self.client.force_login(self.operator)
        with patch.object(db, "terminate_replication_slot_backend") as terminate, patch.object(db, "drop_replication_slot") as drop:
            self.assertEqual(self.client.post(self.kill_url, self.payload()).status_code, 403)
            self.assertEqual(self.client.post(self.drop_url, self.payload()).status_code, 403)
            terminate.assert_not_called()
            drop.assert_not_called()
        self.assertEqual(AuditLog.objects.filter(action="update_slot_permissions", performed_by=self.administrator).count(), 2)

    def test_operators_and_viewers_cannot_grant_themselves_permissions(self):
        for user in [self.operator, self.viewer]:
            self.client.force_login(user)
            response = self.client.post(self.manage_url, {"action": "update_slot_access", "user_id": user.pk, "can_drop_slots": "1", "can_terminate_slots": "1"})
            self.assertEqual(response.status_code, 403)
        self.assertFalse(ReplicationSlotAccess.objects.exists())

    def test_read_only_or_inactive_users_cannot_use_saved_grants(self):
        ReplicationSlotAccess.objects.create(user=self.viewer, can_drop=True, can_terminate=True)
        self.assertFalse(can_manage_replication_slot(self.viewer, "drop"))
        self.assertFalse(can_manage_replication_slot(self.viewer, "terminate"))
        ReplicationSlotAccess.objects.create(user=self.operator, can_drop=True, can_terminate=True)
        self.operator.is_active = False
        self.assertFalse(can_manage_replication_slot(self.operator, "drop"))
        self.assertFalse(can_manage_replication_slot(self.operator, "terminate"))

    def test_creation_defaults_off_and_supports_explicit_operator_grants(self):
        self.client.force_login(self.administrator)
        for username, grants in [("default-operator", {}), ("drop-operator", {"can_drop_slots": "1"})]:
            response = self.client.post(self.manage_url, {"username": username, "password": "Safe-Test-Password-3829", "role": "operator", **grants})
            self.assertEqual(response.status_code, 302)
            user = get_user_model().objects.get(username=username)
            self.assertEqual(can_manage_replication_slot(user, "drop"), bool(grants))
            self.assertFalse(can_manage_replication_slot(user, "terminate"))
        self.client.post(self.manage_url, {"username": "bad-viewer", "password": "Safe-Test-Password-3829", "role": "readonly", "can_drop_slots": "1"})
        self.assertFalse(get_user_model().objects.filter(username="bad-viewer").exists())

    def test_grant_form_rejects_non_operator_targets(self):
        self.client.force_login(self.administrator)
        for target in [self.administrator.pk, self.viewer.pk, "invalid"]:
            response = self.client.post(self.manage_url, {"action": "update_slot_access", "user_id": target, "can_drop_slots": "1"})
            self.assertEqual(response.status_code, 403)

    def test_slot_buttons_match_grants_even_when_slot_data_is_cached(self):
        self.client.force_login(self.operator)
        url = reverse("instance-replication-slots", args=[self.instance.pk])
        with patch.object(db, "fetch_replication_slots", return_value=[{"slot_name": "test_slot", "active": True, "active_pid": 123}]):
            response = self.client.get(url)
            self.assertNotContains(response, f'action="{self.kill_url}"')
            self.assertNotContains(response, f'action="{self.drop_url}"')
            ReplicationSlotAccess.objects.create(user=self.operator, can_terminate=True)
            response = self.client.get(url)
            self.assertContains(response, f'action="{self.kill_url}"')
            self.assertNotContains(response, f'action="{self.drop_url}"')

    def test_generic_kill_routes_only_enable_slot_termination_for_granted_users(self):
        self.client.force_login(self.operator)
        single = reverse("instance-kill", args=[self.instance.pk])
        bulk = reverse("instance-kill-chain", args=[self.instance.pk])
        payload = {"pid": [123], "mfa_code": pyotp.TOTP(self.secret).now()}
        with patch.object(db, "kill_pid", return_value=False) as kill, patch.object(db, "kill_pids", return_value={123: False}) as kills:
            self.client.post(single, payload)
            self.client.post(bulk, payload)
            kill.assert_called_once_with(self.instance, 123)
            kills.assert_called_once_with(self.instance, [123])
        ReplicationSlotAccess.objects.create(user=self.operator, can_terminate=True)
        with patch.object(db, "kill_pid", return_value=True) as kill, patch.object(db, "kill_pids", return_value={123: True}) as kills:
            self.client.post(single, payload)
            self.client.post(bulk, payload)
            kill.assert_called_once_with(self.instance, 123, allow_replication=True)
            kills.assert_called_once_with(self.instance, [123], allow_replication=True)


class GuardedTerminationTests(SimpleTestCase):
    def test_single_and_bulk_use_guarded_statement_with_default_deny(self):
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = (False,)
        with patch.object(db, "get_connection", return_value=connection):
            self.assertFalse(db.kill_pid(object(), 123))
            self.assertEqual(db.kill_pids(object(), [123, 456]), {123: False, 456: False})
        for call in cursor.execute.call_args_list:
            self.assertEqual(call.args[0], db.GUARDED_TERMINATE_QUERY)
            self.assertFalse(call.args[1][0])
        self.assertIn("pg_replication_slots WHERE active_pid", db.GUARDED_TERMINATE_QUERY)
        self.assertIn("ELSE FALSE", db.GUARDED_TERMINATE_QUERY)

    def test_explicit_permission_is_passed_to_guarded_statement(self):
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = (True,)
        with patch.object(db, "get_connection", return_value=connection):
            self.assertTrue(db.kill_pid(object(), 123, allow_replication=True))
        cursor.execute.assert_called_once_with(db.GUARDED_TERMINATE_QUERY, (True, 123, 123))
