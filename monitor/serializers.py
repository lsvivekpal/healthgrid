from rest_framework import serializers

from .models import AuditLog, RDSInstance


class RDSInstanceSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, required=False, allow_blank=True)

    class Meta:
        model = RDSInstance
        fields = [
            "id", "name", "db_identifier", "region", "host", "port", "db_name",
            "username", "password", "ssl_required", "is_active", "added_by", "created_at",
        ]
        read_only_fields = ["added_by", "created_at"]

    def create(self, validated_data):
        password = validated_data.pop("password", "")
        instance = RDSInstance(**validated_data)
        instance.set_password(password)
        instance.save()
        return instance

    def update(self, instance, validated_data):
        password = validated_data.pop("password", None)
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        if password:
            instance.set_password(password)
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
