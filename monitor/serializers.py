from rest_framework import serializers

from .models import AuditLog, RDSInstance
from .permissions import can_manage_instance_notifications
from .security import mask_endpoint

# Fields a non-superuser must never be able to change on an existing instance.
# Repointing host/port/engine/username would exfiltrate stored credentials to
# an attacker-controlled server; the status/exclusion flags are Administrator-
# gated per the permission matrix.
_ADMIN_ONLY_UPDATE_FIELDS = (
    "host", "port", "engine", "username", "db_name",
    "ssl_required", "is_active", "exclude_from_global_notifications",
)
# Changing any of these invalidates the stored lock-control credentials, which
# were issued for the original endpoint/role.
_CONNECTION_FIELDS = ("host", "port", "engine", "username")


class RDSInstanceSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, required=False, allow_blank=True)
    control_password = serializers.CharField(write_only=True, required=False, allow_blank=True)
    # Webhook URLs are bearer secrets. They may be written by staff but are
    # never returned in API responses, including to other staff users.
    owner_teams_webhook_url = serializers.CharField(write_only=True, required=False, allow_blank=True)

    class Meta:
        model = RDSInstance
        fields = [
            "id", "name", "engine", "db_identifier", "region", "host", "port", "db_name",
            "username", "password", "lock_control_enabled", "control_username", "control_password", "owner_teams_webhook_url", "exclude_from_global_notifications", "ssl_required", "is_active", "added_by", "created_at",
        ]
        read_only_fields = ["added_by", "created_at"]

    def to_representation(self, instance):
        data = super().to_representation(instance)
        request = self.context.get("request")
        if not request or not request.user.is_superuser:
            data["host"] = mask_endpoint(instance.host)
        return data

    def create(self, validated_data):
        request = self.context.get("request")
        if not request or not request.user.is_superuser:
            validated_data.pop("lock_control_enabled", None)
            validated_data.pop("control_username", None)
            validated_data.pop("control_password", None)
        if not validated_data.get("lock_control_enabled"):
            validated_data["control_username"] = ""
            validated_data.pop("control_password", None)
        password = validated_data.pop("password", "")
        control_password = validated_data.pop("control_password", "")
        instance = RDSInstance(**validated_data)
        if instance.control_username and not control_password:
            raise serializers.ValidationError({"control_password": "Required when control_username is set."})
        instance.set_password(password)
        if control_password:
            instance.set_control_password(control_password)
        instance.save()
        return instance

    def update(self, instance, validated_data):
        request = self.context.get("request")
        is_superuser = bool(request and request.user.is_superuser)
        if not is_superuser:
            for field in ("lock_control_enabled", "control_username", "control_password"):
                if field in validated_data:
                    raise serializers.ValidationError({field: "Only an Administrator can configure lock-control credentials."})
            for field in _ADMIN_ONLY_UPDATE_FIELDS:
                if field in validated_data:
                    raise serializers.ValidationError({field: "Only an Administrator can change this field."})
            # The Owner webhook is a bearer secret gated by its own per-instance
            # permission; block it otherwise so Operators cannot redirect alerts.
            if "owner_teams_webhook_url" in validated_data and not can_manage_instance_notifications(
                request.user if request else None, instance
            ):
                raise serializers.ValidationError(
                    {"owner_teams_webhook_url": "Notification-management permission is required for this instance."}
                )
        # A superuser repointing the connection must re-establish lock control;
        # the stored credentials were issued for the previous endpoint/role.
        connection_changed = any(
            field in validated_data and validated_data[field] != getattr(instance, field)
            for field in _CONNECTION_FIELDS
        )
        password = validated_data.pop("password", None)
        control_password = validated_data.pop("control_password", None)
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        if connection_changed and "control_password" not in validated_data:
            instance.lock_control_enabled = False
            instance.control_username = ""
            instance.control_password_encrypted = ""
        if password:
            instance.set_password(password)
        if not instance.lock_control_enabled:
            instance.control_username = ""
            instance.control_password_encrypted = ""
        elif "control_username" in validated_data:
            if instance.control_username and not control_password:
                raise serializers.ValidationError({"control_password": "Required when control_username is set."})
            if not instance.control_username:
                instance.control_password_encrypted = ""
        if control_password:
            instance.set_control_password(control_password)
        instance.save()
        return instance


class AuditLogSerializer(serializers.ModelSerializer):
    class Meta:
        model = AuditLog
        fields = [
            "id", "instance", "instance_name", "action", "target_pid",
            "target_query", "performed_by", "performed_at", "result", "detail",
        ]
        read_only_fields = fields
