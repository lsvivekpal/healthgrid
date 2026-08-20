from django.conf import settings
from django.db import models

from . import crypto


class RDSInstance(models.Model):
    name = models.CharField(max_length=100, help_text="Friendly display name")
    db_identifier = models.CharField(max_length=255, help_text="RDS DBInstanceIdentifier")
    region = models.CharField(max_length=32, default="us-east-1")
    host = models.CharField(max_length=255, help_text="RDS endpoint address")
    port = models.PositiveIntegerField(default=5432)
    db_name = models.CharField(max_length=255)
    username = models.CharField(max_length=255, help_text="DB login username")
    owner_teams_webhook_url = models.URLField(blank=True, help_text="Teams Workflow webhook for this database owner")
    password_encrypted = models.CharField(
        max_length=512, help_text="Fernet-encrypted DB login password"
    )
    ssl_required = models.BooleanField(
        default=True, help_text="Uncheck for local/dev Postgres without SSL"
    )
    is_active = models.BooleanField(default=True)
    added_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} ({self.db_identifier})"

    def set_password(self, raw_password):
        self.password_encrypted = crypto.encrypt_password(raw_password)

    def get_password(self):
        return crypto.decrypt_password(self.password_encrypted)


class AuditLog(models.Model):
    ACTION_CHOICES = [
        ("kill_session", "Kill session"),
        ("kill_chain", "Kill blocking chain"),
        ("bulk_kill", "Bulk kill selected"),
        ("kill_replication_slot", "Kill replication slot backend"),
        ("drop_replication_slot", "Drop replication slot"),
        ("add_instance", "Add instance"),
        ("remove_instance", "Remove instance"),
        ("rename_instance", "Rename instance"),
    ]

    instance = models.ForeignKey(
        RDSInstance, on_delete=models.SET_NULL, null=True, blank=True, related_name="audit_logs"
    )
    instance_name = models.CharField(max_length=100, blank=True)
    action = models.CharField(max_length=32, choices=ACTION_CHOICES)
    target_pid = models.IntegerField(null=True, blank=True)
    target_query = models.TextField(blank=True)
    performed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True
    )
    performed_at = models.DateTimeField(auto_now_add=True)
    result = models.CharField(max_length=16, choices=[("success", "Success"), ("failed", "Failed")])
    detail = models.TextField(blank=True)

    class Meta:
        ordering = ["-performed_at"]

    def __str__(self):
        return f"{self.action} on {self.instance_name} @ {self.performed_at:%Y-%m-%d %H:%M:%S}"


class LockAlert(models.Model):
    """Persistent state for a lock observed by the background monitor."""

    instance = models.ForeignKey(RDSInstance, on_delete=models.CASCADE, related_name="lock_alerts")
    alert_key = models.CharField(max_length=255)
    blocked_pid = models.IntegerField()
    blocking_pid = models.IntegerField()
    blocked_query = models.TextField(blank=True)
    blocking_query = models.TextField(blank=True)
    first_seen_at = models.DateTimeField()
    last_seen_at = models.DateTimeField()
    alerted_at = models.DateTimeField(null=True, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["instance", "resolved_at"]),
            models.Index(fields=["alert_key"]),
        ]

    def __str__(self):
        return f"{self.instance.name}: {self.blocked_pid} blocked by {self.blocking_pid}"
