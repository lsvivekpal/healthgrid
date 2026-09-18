from rest_framework import serializers

from .models import AuditLog, RDSInstance


class RDSInstanceSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, required=False, allow_blank=True)
    control_password = serializers.CharField(write_only=True, required=False, allow_blank=True)
    # Webhook URLs are bearer secrets. They may be written by staff but are
    # never returned in API responses, including to other staff users.
    owner_teams_webhook_url = serializers.CharField(write_only=True, required=False, allow_blank=True)

    class Meta:
        model = RDSInstance
        fields = [
            "id", "name", "db_identifier", "region", "host", "port", "db_name",
            "username", "password", "lock_control_enabled", "control_username", "control_password", "owner_teams_webhook_url", "exclude_from_global_notifications", "ssl_required", "is_active", "added_by", "created_at",
        ]
        read_only_fields = ["added_by", "created_at"]

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
        if not request or not request.user.is_superuser:
            for field in ("lock_control_enabled", "control_username", "control_password"):
                if field in validated_data:
                    raise serializers.ValidationError({field: "Only an Administrator can configure lock-control credentials."})
        password = validated_data.pop("password", None)
        control_password = validated_data.pop("control_password", None)
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
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
