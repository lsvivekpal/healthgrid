from django.conf import settings
from django.db import models
from django.db.models import Q

from . import crypto


class RDSInstance(models.Model):
    name = models.CharField(max_length=100, help_text="Friendly display name")
    db_identifier = models.CharField(max_length=255, help_text="RDS DBInstanceIdentifier")
    region = models.CharField(max_length=32, default="us-east-1")
    host = models.CharField(max_length=255, help_text="RDS endpoint address")
    port = models.PositiveIntegerField(default=5432)
    db_name = models.CharField(max_length=255)
    username = models.CharField(max_length=255, help_text="DB login username")
    control_username = models.CharField(
        max_length=255,
        blank=True,
        help_text="Optional dedicated monitoring/kill role; falls back to DB login username",
    )
    owner_teams_webhook_url = models.URLField(blank=True, help_text="Teams Workflow webhook for this database owner")
    password_encrypted = models.CharField(
        max_length=512, help_text="Fernet-encrypted DB login password"
    )
    control_password_encrypted = models.CharField(
        max_length=512,
        blank=True,
        help_text="Fernet-encrypted control role password",
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

    def set_control_password(self, raw_password):
        self.control_password_encrypted = crypto.encrypt_password(raw_password)

    def get_control_password(self):
        return crypto.decrypt_password(self.control_password_encrypted)


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
    blocked_user = models.CharField(max_length=255, blank=True)
    blocking_user = models.CharField(max_length=255, blank=True)
    blocked_query = models.TextField(blank=True)
    blocking_query = models.TextField(blank=True)
    waiting_seconds = models.PositiveIntegerField(default=0)
    first_seen_at = models.DateTimeField()
    last_seen_at = models.DateTimeField()
    alerted_at = models.DateTimeField(null=True, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["instance", "resolved_at"]),
            models.Index(fields=["alert_key"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["instance", "alert_key"],
                condition=Q(resolved_at__isnull=True),
                name="unique_active_lock_alert",
            ),
        ]

    def __str__(self):
        return f"{self.instance.name}: {self.blocked_pid} blocked by {self.blocking_pid}"


class LockNotificationState(models.Model):
    """Last delivered aggregate lock summary for one database."""

    instance = models.OneToOneField(RDSInstance, on_delete=models.CASCADE, related_name="lock_notification_state")
    last_fingerprint = models.CharField(max_length=64, blank=True)
    last_active_keys = models.JSONField(default=list)
    last_sent_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"Lock notification state for {self.instance.name}"


class NotificationSettings(models.Model):
    """Singleton configuration managed from the staff UI."""

    channel_webhook_url = models.URLField(blank=True, help_text="Shared Teams channel Workflow webhook")
    threshold_seconds = models.PositiveIntegerField(default=120)
    interval_seconds = models.PositiveIntegerField(default=30)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Notification settings"
        verbose_name_plural = "Notification settings"

    @classmethod
    def load(cls):
        settings, _created = cls.objects.get_or_create(pk=1)
        return settings

    def __str__(self):
        return "Notification settings"


class MonitorLease(models.Model):
    """A short-lived database lease preventing duplicate monitor loops."""

    owner_id = models.CharField(max_length=64, blank=True)
    lease_until = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return "Embedded lock monitor lease"
