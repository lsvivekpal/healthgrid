from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .auth_views import mfa_setup
from . import views

router = DefaultRouter()
router.register("instances", views.RDSInstanceViewSet, basename="api-instance")
router.register("audit-logs", views.AuditLogViewSet, basename="api-auditlog")

urlpatterns = [
    path("api/", include(router.urls)),
    path("", views.instance_list, name="instance-list"),
    path("instances/add/", views.add_instance, name="instance-add"),
    path("instances/test-connection/", views.test_connection, name="instance-test-connection"),
    path("instances/<int:pk>/test-control-connection/", views.test_control_connection, name="instance-test-control-connection"),
    path("settings/notifications/", views.notification_settings, name="notification-settings"),
    path("settings/notifications/test/", views.test_notification, name="test-notification"),
    path("settings/notifications/weekly-report/", views.send_weekly_report_now, name="send-weekly-report-now"),
    path("settings/users/", views.user_management, name="user-management"),
    path("settings/dashboards/", views.dashboard_links, name="dashboard-links"),
    path("settings/security/mfa/", mfa_setup, name="mfa-setup"),
    path("reports/weekly/<str:token>/", views.download_weekly_report, name="download-weekly-report"),
    path("instances/<int:pk>/", views.instance_detail, name="instance-detail"),
    path("instances/<int:pk>/card/", views.instance_card_partial, name="instance-card"),
    path("instances/group/<uuid:group_id>/card/", views.instance_group_card_partial, name="instance-group-card"),
    path("instances/<int:pk>/activity/", views.activity_table_partial, name="instance-activity"),
    path("instances/<int:pk>/replication-slots/", views.replication_slots_partial, name="instance-replication-slots"),
    path("instances/<int:pk>/replication-slots/kill/", views.kill_replication_slot, name="instance-kill-replication-slot"),
    path("instances/<int:pk>/replication-slots/drop/", views.drop_replication_slot, name="instance-drop-replication-slot"),
    path("instances/<int:pk>/locks/download/", views.download_locks, name="instance-locks-download"),
    path("instances/<int:pk>/sessions/download/", views.download_sessions, name="instance-sessions-download"),
    path("instances/<int:pk>/locks/kill/", views.kill_lock_session, name="instance-kill-lock"),
    path("instances/<int:pk>/locks/kill-chain/", views.kill_lock_chain, name="instance-kill-lock-chain"),
    path("instances/<int:pk>/kill/", views.kill_session, name="instance-kill"),
    path("instances/<int:pk>/kill-chain/", views.kill_chain, name="instance-kill-chain"),
    path("instances/<int:pk>/rename/", views.rename_instance, name="instance-rename"),
    path("instances/<int:pk>/owner-webhook/", views.update_owner_webhook, name="instance-owner-webhook"),
    path("instances/<int:pk>/control-credentials/", views.update_control_credentials, name="instance-control-credentials"),
    path("instances/<int:pk>/remove/", views.remove_instance, name="instance-remove"),
    path("instances/group/<uuid:group_id>/remove/", views.remove_instance_group, name="instance-group-remove"),
]
