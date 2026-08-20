from django.contrib import admin

from .models import AuditLog, RDSInstance


@admin.register(RDSInstance)
class RDSInstanceAdmin(admin.ModelAdmin):
    list_display = ("name", "db_identifier", "owner_teams_webhook_url", "region", "host", "port", "is_active", "created_at")
    list_filter = ("region", "is_active")
    search_fields = ("name", "db_identifier", "host")


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ("instance_name", "action", "target_pid", "result", "performed_by", "performed_at")
    list_filter = ("action", "result")
    search_fields = ("instance_name", "target_query")
    readonly_fields = [f.name for f in AuditLog._meta.fields]

    def has_add_permission(self, request):
        return False
