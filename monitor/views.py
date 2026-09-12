import csv
import logging
from datetime import datetime

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.core.exceptions import PermissionDenied
from django.core.signing import BadSignature, SignatureExpired, TimestampSigner
from django.core.validators import URLValidator
from django.core.exceptions import ValidationError
from django.db.models import Q
from django.db import transaction
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from urllib.parse import urlsplit
from django.views.decorators.http import require_POST, require_http_methods
from rest_framework import viewsets
from rest_framework.permissions import IsAdminUser
from rest_framework.exceptions import PermissionDenied as APIPermissionDenied

from . import db
from .mfa import require_mfa_for_action
from .models import AuditLog, DashboardLink, LockAlert, LockReport, NotificationSettings, RDSInstance, ReplicationSlotAccess
from .notifications import REPORT_MAX_AGE_SECONDS, send_test_notification, send_weekly_reports
from .permissions import IsStaffOrReadOnly, can_manage_replication_slot
from .serializers import AuditLogSerializer, RDSInstanceSerializer

logger = logging.getLogger(__name__)


def _require_staff(request):
    if not request.user.is_staff:
        raise PermissionDenied("Staff permission required for this action.")


def _require_administrator(request):
    if not (request.user.is_active and request.user.is_staff and request.user.is_superuser):
        raise PermissionDenied("Administrator permission required for this action.")


def _validate_dashboard_url(value):
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValidationError("Use an HTTP(S) URL without embedded credentials.")
    URLValidator()(value)


def _require_slot_permission(request, action):
    if not can_manage_replication_slot(request.user, action):
        raise PermissionDenied(f"Administrator access or an explicit operator grant is required to {action} replication slots.")


def _save_slot_access(request, user, can_drop, can_terminate):
    ReplicationSlotAccess.objects.update_or_create(
        user=user, defaults={"can_drop": can_drop, "can_terminate": can_terminate},
    )
    _log_audit(None, "update_slot_permissions", request.user,
               detail=f"user={user.username} (id={user.pk}); drop={can_drop}; terminate={can_terminate}")


def _fmt_duration(seconds):
    """Compact human duration: 45s, 3m 12s, 1h 04m."""
    try:
        seconds = int(seconds or 0)
    except (TypeError, ValueError):
        return "—"
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60:02d}s"
    return f"{seconds // 3600}h {(seconds % 3600) // 60:02d}m"


def _with_human_durations(rows, field):
    """Add a readable duration without losing the numeric value for sorting/color."""
    formatted = []
    for row in rows:
        item = dict(row)
        item[f"{field}_human"] = _fmt_duration(item.get(field))
        formatted.append(item)
    return formatted


def _log_audit(instance, action, user, *, pid=None, query="", result="success", detail=""):
    AuditLog.objects.create(
        instance=instance,
        instance_name=instance.name if instance else "",
        action=action,
        target_pid=pid,
        target_query=query,
        performed_by=user,
        result=result,
        detail=detail,
    )


def _mark_manual_kill(instance, pids, user):
    """Attribute active lock incidents affected by a successful manual kill."""
    pids = [int(pid) for pid in pids]
    if not pids:
        return 0
    return LockAlert.objects.filter(
        instance=instance,
        resolved_at__isnull=True,
    ).filter(
        Q(blocked_pid__in=pids) | Q(blocking_pid__in=pids)
    ).update(clear_reason="manual_kill", cleared_by=user.get_username())


def _require_action_mfa(request, instance, action, detail="", *, require_code=False):
    if require_mfa_for_action(request, request.POST.get("mfa_code", ""), require_code=require_code):
        return True
    _log_audit(
        instance,
        action,
        request.user,
        result="failed",
        detail=f"MFA verification required{': ' + detail if detail else ''}",
    )
    messages.error(request, "MFA verification is required for this action. Set up Google Authenticator from the security menu, then try again.")
    return False


# --- DRF API: instance registry CRUD ---


class RDSInstanceViewSet(viewsets.ModelViewSet):
    queryset = RDSInstance.objects.all()
    serializer_class = RDSInstanceSerializer
    permission_classes = [IsStaffOrReadOnly]

    def perform_create(self, serializer):
        instance = serializer.save(added_by=self.request.user)
        _log_audit(instance, "add_instance", self.request.user)

    def perform_destroy(self, instance):
        _require_administrator(self.request)
        if not require_mfa_for_action(self.request, self.request.data.get("mfa_code", ""), require_code=True):
            _log_audit(instance, "remove_instance", self.request.user, result="failed", detail="MFA verification required")
            raise APIPermissionDenied("A valid authenticator or recovery code is required to remove an instance.")
        _log_audit(instance, "remove_instance", self.request.user, detail=instance.db_identifier)
        instance.delete()


class AuditLogViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = AuditLogSerializer
    permission_classes = [IsAdminUser]

    def get_queryset(self):
        qs = AuditLog.objects.all()
        instance_id = self.request.query_params.get("instance")
        if instance_id:
            qs = qs.filter(instance_id=instance_id)
        return qs


# --- Template views: dashboard UI ---


@login_required
def instance_list(request):
    instances = RDSInstance.objects.filter(is_active=True)
    links = DashboardLink.objects.filter(is_active=True)
    return render(request, "monitor/instance_list.html", {"instances": instances, "dashboard_links": links})


@login_required
@require_http_methods(["GET", "POST"])
def dashboard_links(request):
    """Show approved links to other dashboards; only Administrators edit them."""
    if request.method == "POST":
        _require_administrator(request)
        action = request.POST.get("action", "save")
        if action == "delete":
            link = get_object_or_404(DashboardLink, pk=request.POST.get("link_id"))
            link.delete()
            messages.success(request, "Dashboard link removed.")
            return redirect("dashboard-links")

        name = request.POST.get("name", "").strip()
        url = request.POST.get("url", "").strip()
        description = request.POST.get("description", "").strip()
        category = request.POST.get("category", "").strip()
        try:
            sort_order = int(request.POST.get("sort_order", "0"))
        except (TypeError, ValueError):
            sort_order = -1
        if not name or len(name) > 100 or len(description) > 255 or len(category) > 80 or sort_order < 0:
            messages.error(request, "Enter a name, valid description/category lengths, and a non-negative display order.")
        else:
            try:
                _validate_dashboard_url(url)
            except ValidationError as exc:
                messages.error(request, exc.messages[0])
            else:
                link_id = request.POST.get("link_id")
                link = get_object_or_404(DashboardLink, pk=link_id) if link_id else DashboardLink(created_by=request.user)
                link.name = name
                link.url = url
                link.description = description
                link.category = category
                link.sort_order = sort_order
                link.is_active = bool(request.POST.get("is_active"))
                link.save()
                messages.success(request, "Dashboard link saved.")
                return redirect("dashboard-links")
    editing_link = None
    if request.user.is_superuser and request.GET.get("edit"):
        editing_link = get_object_or_404(DashboardLink, pk=request.GET["edit"])
    return render(request, "monitor/dashboard_links.html", {
        "dashboard_links": DashboardLink.objects.all(),
        "can_manage_links": request.user.is_superuser,
        "editing_link": editing_link,
    })


@login_required
def notification_settings(request):
    _require_staff(request)
    config = NotificationSettings.load()
    if request.method == "POST":
        webhook_url = request.POST.get("channel_webhook_url", "").strip()
        if len(webhook_url) > 2048:
            messages.error(request, "The Teams webhook URL must be 2048 characters or fewer.")
            return render(request, "monitor/notification_settings.html", {"notification_settings": config})
        if webhook_url:
            try:
                URLValidator()(webhook_url)
            except ValidationError:
                messages.error(request, "Enter a valid Teams webhook URL or leave the field blank.")
                return render(request, "monitor/notification_settings.html", {"notification_settings": config})
        report_base_url = request.POST.get("report_base_url", "").strip().rstrip("/")
        if len(report_base_url) > 2048:
            messages.error(request, "The public dashboard URL must be 2048 characters or fewer.")
            return render(request, "monitor/notification_settings.html", {"notification_settings": config})
        if report_base_url:
            try:
                URLValidator()(report_base_url)
            except ValidationError:
                messages.error(request, "Enter a valid public dashboard URL or leave the field blank.")
                return render(request, "monitor/notification_settings.html", {"notification_settings": config})
        try:
            threshold = int(request.POST.get("threshold_seconds", "120"))
            interval = int(request.POST.get("interval_seconds", "30"))
            report_day = int(request.POST.get("weekly_report_day", "0"))
            report_hour = int(request.POST.get("weekly_report_hour", "9"))
            retention_days = int(request.POST.get("resolved_alert_retention_days", "30"))
            schedule_enabled = bool(request.POST.get("notification_schedule_enabled"))
            schedule_start = datetime.strptime(request.POST.get("notification_start_time", "09:00"), "%H:%M").time()
            schedule_end = datetime.strptime(request.POST.get("notification_end_time", "21:00"), "%H:%M").time()
            manual_query_enabled = bool(request.POST.get("manual_query_alert_enabled"))
            manual_query_threshold = int(request.POST.get("manual_query_threshold_seconds", "60"))
            manual_query_excluded_users = ",".join(
                sorted({
                    username.strip()
                    for username in request.POST.get("manual_query_excluded_users", "").split(",")
                    if username.strip()
                }, key=str.casefold)
            )
        except ValueError:
            messages.error(request, "Enter valid numeric values and notification times in HH:MM format.")
        else:
            if (
                threshold < 1
                or interval < 1
                or report_day not in range(7)
                or report_hour not in range(24)
                or retention_days < 7
                or retention_days > 3650
                or (schedule_enabled and schedule_start == schedule_end)
                or manual_query_threshold < 1
                or len(manual_query_excluded_users) > 4000
            ):
                messages.error(request, "Use positive thresholds, different notification start/end times, a valid report schedule, retention from 7 to 3650 days, and a valid excluded-user list.")
            else:
                config.channel_webhook_url = webhook_url
                config.threshold_seconds = threshold
                config.interval_seconds = interval
                config.notification_schedule_enabled = schedule_enabled
                config.notification_start_time = schedule_start
                config.notification_end_time = schedule_end
                config.manual_query_alert_enabled = manual_query_enabled
                config.manual_query_threshold_seconds = manual_query_threshold
                config.manual_query_excluded_users = manual_query_excluded_users
                config.weekly_report_enabled = bool(request.POST.get("weekly_report_enabled"))
                config.weekly_report_day = report_day
                config.weekly_report_hour = report_hour
                config.report_base_url = report_base_url
                config.resolved_alert_retention_days = retention_days
                config.save()
                if request.POST.get("global_scope_submitted") == "1":
                    excluded_ids = {
                        value for value in request.POST.getlist("excluded_instance_ids")
                        if value.isdigit()
                    }
                    RDSInstance.objects.filter(is_active=True).update(exclude_from_global_notifications=False)
                    RDSInstance.objects.filter(is_active=True, pk__in=excluded_ids).update(
                        exclude_from_global_notifications=True,
                    )
                messages.success(request, "Notification settings saved.")
                return redirect("notification-settings")
    return render(request, "monitor/notification_settings.html", {
        "notification_settings": config,
        "notification_instances": RDSInstance.objects.filter(is_active=True),
    })


@login_required
@require_http_methods(["GET", "POST"])
def user_management(request):
    """Administrator-only account creation and per-operator slot grants."""
    _require_administrator(request)
    User = get_user_model()
    if request.method == "POST":
        can_drop = request.POST.get("can_drop_slots") == "1"
        can_terminate = request.POST.get("can_terminate_slots") == "1"
        action = request.POST.get("action", "create_user")
        if action == "update_slot_access":
            try:
                target_id = int(request.POST.get("user_id", ""))
            except (TypeError, ValueError):
                raise PermissionDenied("A valid operator account is required.")
            with transaction.atomic():
                target = get_object_or_404(User.objects.select_for_update(), pk=target_id)
                if target.is_superuser or not target.is_staff:
                    raise PermissionDenied("Replication-slot grants can only be changed for operators.")
                _save_slot_access(request, target, can_drop, can_terminate)
            messages.success(request, f"Updated replication-slot permissions for '{target.username}'.")
            return redirect("user-management")
        if action != "create_user":
            raise PermissionDenied("Unknown user-management action.")
        username = request.POST.get("username", "").strip()
        email = request.POST.get("email", "").strip()
        password = request.POST.get("password", "")
        role = request.POST.get("role", "readonly")
        if not username or not password or role not in {"operator", "readonly"}:
            messages.error(request, "Username, password, and a valid role are required.")
        elif role != "operator" and (can_drop or can_terminate):
            messages.error(request, "Only operators can be granted replication-slot permissions.")
        elif User.objects.filter(username=username).exists():
            messages.error(request, "That username already exists.")
        else:
            try:
                validate_password(password)
            except ValidationError as exc:
                messages.error(request, " ".join(exc.messages))
            else:
                with transaction.atomic():
                    user = User.objects.create_user(username=username, email=email, password=password, is_staff=role == "operator")
                    if role == "operator" and (can_drop or can_terminate):
                        _save_slot_access(request, user, can_drop, can_terminate)
                messages.success(request, f"Created {role} user '{username}'. They will enroll their own authenticator on first login.")
                return redirect("user-management")
    return render(request, "monitor/user_management.html", {"users": User.objects.select_related("replication_slot_access", "mfa_profile").order_by("username")})


@login_required
@require_POST
def test_notification(request):
    _require_staff(request)
    destination = request.POST.get("destination", "Teams")
    webhook_url = request.POST.get("webhook_url", "").strip()
    if destination == "shared channel":
        webhook_url = NotificationSettings.load().channel_webhook_url
    ok, detail = send_test_notification(webhook_url, destination)
    if ok:
        messages.success(request, detail)
    else:
        messages.error(request, f"Test notification failed: {detail}")
    next_page = request.POST.get("next")
    if next_page == "notification-settings":
        return redirect("notification-settings")
    if next_page == "instance-detail" and request.POST.get("instance_id"):
        return redirect("instance-detail", pk=request.POST["instance_id"])
    return redirect("instance-add")


@login_required
@require_POST
def send_weekly_report_now(request):
    _require_staff(request)
    if send_weekly_reports():
        messages.success(request, "Weekly lock report sent through the configured Teams webhook(s).")
    else:
        messages.error(request, "Weekly lock report was not sent. Configure a webhook and, for large reports, a public dashboard URL.")
    return redirect("notification-settings")


def download_weekly_report(request, token):
    """Public, signed, short-lived CSV endpoint for the Power Automate flow."""
    try:
        report_id = TimestampSigner().unsign(token, max_age=REPORT_MAX_AGE_SECONDS)
    except (BadSignature, SignatureExpired):
        raise Http404("Report link expired")
    report = get_object_or_404(LockReport, pk=report_id)
    if report.expires_at < timezone.now():
        raise Http404("Report link expired")
    response = HttpResponse(report.csv_content, content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{report.file_name}"'
    return response


@login_required
def instance_card_partial(request, pk):
    instance = get_object_or_404(RDSInstance, pk=pk)
    ctx = {"instance": instance}
    try:
        activity, blocking = db.fetch_activity(instance)
        ctx.update(reachable=True, session_count=len(activity), blocked_count=len(blocking))
    except db.ConnectionError as exc:
        ctx.update(reachable=False, error=str(exc))
    return render(request, "monitor/partials/instance_card.html", ctx)


@login_required
def instance_detail(request, pk):
    instance = get_object_or_404(RDSInstance, pk=pk)
    audit_logs = instance.audit_logs.all()[:25]
    return render(
        request,
        "monitor/instance_detail.html",
        {"instance": instance, "audit_logs": audit_logs},
    )


@login_required
def activity_table_partial(request, pk):
    instance = get_object_or_404(RDSInstance, pk=pk)
    cache_key = f"activity:{instance.pk}"
    cached = cache.get(cache_key)
    if cached is not None:
        activity, blocking, vitals, top_tables, error = cached
    else:
        try:
            activity, blocking, vitals, top_tables = db.fetch_activity_with_vitals(instance)
            error = None
        except db.ConnectionError as exc:
            activity, blocking, vitals, top_tables, error = [], [], None, [], str(exc)
        cache.set(cache_key, (activity, blocking, vitals, top_tables, error), timeout=settings.ACTIVITY_CACHE_TTL)

    activity = _with_human_durations(activity, "duration_seconds")
    blocking = _with_human_durations(blocking, "waiting_seconds")
    blocking_pids = {row["blocking_pid"] for row in blocking}
    conn_pct = None
    if vitals:
        vitals = dict(vitals)
        vitals["longest_active_human"] = _fmt_duration(vitals.get("longest_active_seconds"))
        if vitals.get("max_conns"):
            conn_pct = round(100.0 * vitals["total_conns"] / vitals["max_conns"], 1)
    return render(
        request,
        "monitor/partials/activity_table.html",
        {
            "instance": instance,
            "activity": activity,
            "blocking": blocking,
            "blocking_pids": blocking_pids,
            "vitals": vitals,
            "conn_pct": conn_pct,
            "top_tables": top_tables,
            "error": error,
            "is_staff": request.user.is_staff,
        },
    )


@login_required
def replication_slots_partial(request, pk):
    instance = get_object_or_404(RDSInstance, pk=pk)
    cache_key = f"repslots:{instance.pk}"
    cached = cache.get(cache_key)
    if cached is not None:
        slots, error = cached
    else:
        try:
            slots, error = db.fetch_replication_slots(instance), None
        except db.ConnectionError as exc:
            slots, error = [], str(exc)
        cache.set(cache_key, (slots, error), timeout=settings.ACTIVITY_CACHE_TTL)

    return render(
        request,
        "monitor/partials/replication_slots.html",
        {"instance": instance, "slots": slots, "error": error,
         "can_drop_slots": can_manage_replication_slot(request.user, "drop"),
         "can_terminate_slots": can_manage_replication_slot(request.user, "terminate")},
    )


@login_required
@require_POST
def kill_replication_slot(request, pk):
    """Terminate the walsender backend behind a slot — a prerequisite for dropping it."""
    _require_slot_permission(request, "terminate")
    instance = get_object_or_404(RDSInstance, pk=pk)
    slot_name = request.POST.get("slot_name", "")

    try:
        terminated = db.terminate_replication_slot_backend(instance, slot_name)
        _log_audit(
            instance, "kill_replication_slot", request.user,
            detail=slot_name,
            result="success" if terminated else "failed",
        )
        if terminated:
            messages.success(request, f"Terminated the active backend for slot '{slot_name}'.")
        else:
            messages.warning(request, f"Slot '{slot_name}' has no active backend right now.")
    except db.ConnectionError as exc:
        _log_audit(instance, "kill_replication_slot", request.user, detail=f"{slot_name}: {exc}", result="failed")
        messages.error(request, f"Could not connect: {exc}")

    cache.delete(f"repslots:{instance.pk}")
    return redirect("instance-detail", pk=instance.pk)


@login_required
@require_POST
def drop_replication_slot(request, pk):
    """Drop a replication slot entirely. Irreversible — any consumer attached to
    it will need to resync from scratch. Fails if the slot is still active."""
    _require_slot_permission(request, "drop")
    instance = get_object_or_404(RDSInstance, pk=pk)
    slot_name = request.POST.get("slot_name", "")
    if not _require_action_mfa(request, instance, "drop_replication_slot", detail=slot_name, require_code=True):
        return redirect("instance-detail", pk=instance.pk)

    try:
        ok, error = db.drop_replication_slot(instance, slot_name)
        _log_audit(
            instance, "drop_replication_slot", request.user,
            detail=slot_name if ok else f"{slot_name}: {error}",
            result="success" if ok else "failed",
        )
        if ok:
            messages.success(request, f"Dropped replication slot '{slot_name}'.")
        else:
            messages.error(request, f"Could not drop slot '{slot_name}': {error}")
    except db.ConnectionError as exc:
        _log_audit(instance, "drop_replication_slot", request.user, detail=f"{slot_name}: {exc}", result="failed")
        messages.error(request, f"Could not connect: {exc}")

    cache.delete(f"repslots:{instance.pk}")
    return redirect("instance-detail", pk=instance.pk)


@login_required
def download_locks(request, pk):
    """CSV of current blocked/blocking queries, full untruncated text — for
    saving a record of what's about to be killed before you kill it."""
    instance = get_object_or_404(RDSInstance, pk=pk)
    try:
        _activity, blocking = db.fetch_activity(instance)
    except db.ConnectionError as exc:
        messages.error(request, f"Could not fetch locks to download: {exc}")
        return redirect("instance-detail", pk=instance.pk)

    timestamp = timezone.now().strftime("%Y%m%d-%H%M%S")
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = f'attachment; filename="{instance.db_identifier}-locks-{timestamp}.csv"'

    writer = csv.writer(response)
    writer.writerow([
        "captured_at", "blocked_pid", "blocked_user", "blocked_query",
        "blocking_pid", "blocking_user", "blocking_query", "waiting_seconds",
    ])
    captured_at = timezone.now().isoformat()
    for row in blocking:
        writer.writerow([
            captured_at,
            row["blocked_pid"], row["blocked_user"], row["blocked_query"],
            row["blocking_pid"], row["blocking_user"], row["blocking_query"],
            row["waiting_seconds"],
        ])
    return response


@login_required
def download_sessions(request, pk):
    """CSV of current sessions (active + idle-in-transaction), full untruncated
    query text — same convenience as the locks CSV download."""
    instance = get_object_or_404(RDSInstance, pk=pk)
    try:
        activity, _blocking = db.fetch_activity(instance)
    except db.ConnectionError as exc:
        messages.error(request, f"Could not fetch sessions to download: {exc}")
        return redirect("instance-detail", pk=instance.pk)

    timestamp = timezone.now().strftime("%Y%m%d-%H%M%S")
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = f'attachment; filename="{instance.db_identifier}-sessions-{timestamp}.csv"'

    writer = csv.writer(response)
    writer.writerow(["captured_at", "pid", "usename", "state", "wait_event", "duration_seconds", "query"])
    captured_at = timezone.now().isoformat()
    for row in activity:
        writer.writerow([
            captured_at,
            row["pid"], row["usename"], row["state"], row.get("wait_event"),
            row["duration_seconds"], row["query"],
        ])
    return response


def _current_lock_pids(instance):
    """Return PIDs currently involved in a lock, for the no-MFA lock route."""
    _activity, blocking = db.fetch_activity(instance)
    return {
        pid
        for row in blocking
        for pid in (row.get("blocked_pid"), row.get("blocking_pid"))
        if pid is not None
    }


def _kill_session_impl(request, pk, *, lock_only=False):
    instance = get_object_or_404(RDSInstance, pk=pk)
    _require_staff(request)

    try:
        pid = int(request.POST["pid"])
    except (KeyError, TypeError, ValueError):
        messages.error(request, "A valid backend PID is required.")
        return redirect("instance-detail", pk=instance.pk)
    query_snapshot = request.POST.get("query", "")
    if lock_only:
        try:
            if pid not in _current_lock_pids(instance):
                messages.error(request, f"pid {pid} is no longer part of an active lock.")
                return redirect("instance-detail", pk=instance.pk)
        except db.ConnectionError as exc:
            messages.error(request, f"Could not verify the lock before killing pid {pid}: {exc}")
            return redirect("instance-detail", pk=instance.pk)
    elif not _require_action_mfa(request, instance, "kill_session", detail=f"pid={pid}"):
        return redirect("instance-detail", pk=instance.pk)

    try:
        if can_manage_replication_slot(request.user, "terminate"):
            terminated = db.kill_pid(instance, pid, allow_replication=True)
        else:
            terminated = db.kill_pid(instance, pid)
        _log_audit(
            instance, "kill_session", request.user,
            pid=pid, query=query_snapshot,
            result="success" if terminated else "failed",
            detail="" if terminated else "pid was not running or is a protected replication-slot backend",
        )
        if terminated:
            _mark_manual_kill(instance, [pid], request.user)
            messages.success(request, f"Terminated backend pid {pid}.")
        else:
            messages.warning(request, f"pid {pid} was not running or is a replication-slot backend you do not have permission to terminate.")
    except db.ConnectionError as exc:
        _log_audit(instance, "kill_session", request.user, pid=pid, query=query_snapshot, result="failed", detail=str(exc))
        messages.error(request, f"Could not connect to kill pid {pid}: {exc}")

    cache.delete(f"activity:{instance.pk}")
    return redirect("instance-detail", pk=instance.pk)


@login_required
@require_POST
def kill_session(request, pk):
    """Kill a live session; MFA is required for this route."""
    return _kill_session_impl(request, pk, lock_only=False)


@login_required
@require_POST
def kill_lock_session(request, pk):
    """Kill a PID currently involved in a lock; this is the MFA-free lock action."""
    return _kill_session_impl(request, pk, lock_only=True)


def _kill_chain_impl(request, pk, *, lock_only=False, require_mfa=False):
    """Kill selected PIDs, with an optional validated lock scope."""
    instance = get_object_or_404(RDSInstance, pk=pk)
    _require_staff(request)

    try:
        pids = [int(p) for p in request.POST.getlist("pid")]
    except (TypeError, ValueError):
        messages.error(request, "The selected backend PIDs are invalid.")
        return redirect("instance-detail", pk=instance.pk)
    query_snapshot = request.POST.get("query", "")
    action = "kill_chain" if request.POST.get("mode") == "chain" else "bulk_kill"
    if not pids:
        messages.error(request, "Select at least one backend PID first.")
        return redirect("instance-detail", pk=instance.pk)
    if require_mfa and not _require_action_mfa(request, instance, action, detail=f"pids={pids}"):
        return redirect("instance-detail", pk=instance.pk)
    if lock_only:
        try:
            lock_pids = _current_lock_pids(instance)
            if not set(pids).issubset(lock_pids):
                messages.error(request, "One or more selected PIDs are no longer part of an active lock.")
                return redirect("instance-detail", pk=instance.pk)
        except db.ConnectionError as exc:
            messages.error(request, f"Could not verify the lock before killing the selected PIDs: {exc}")
            return redirect("instance-detail", pk=instance.pk)

    try:
        if can_manage_replication_slot(request.user, "terminate"):
            results = db.kill_pids(instance, pids, allow_replication=True)
        else:
            results = db.kill_pids(instance, pids)
        terminated = [pid for pid, ok in results.items() if ok]
        not_running = [pid for pid, ok in results.items() if not ok]
        _log_audit(
            instance, action, request.user,
            query=query_snapshot,
            result="success" if terminated else "failed",
            detail=f"pids={pids} terminated={terminated} not_running={not_running}",
        )
        if terminated:
            _mark_manual_kill(instance, terminated, request.user)
            messages.success(request, f"Terminated {len(terminated)} pid(s): {terminated}.")
        if not_running:
            messages.warning(request, f"Not running or protected replication-slot backends: {not_running}.")
    except db.ConnectionError as exc:
        _log_audit(instance, "kill_chain", request.user, query=query_snapshot, result="failed", detail=str(exc))
        messages.error(request, f"Could not connect to kill chain: {exc}")

    cache.delete(f"activity:{instance.pk}")
    return redirect("instance-detail", pk=instance.pk)


@login_required
@require_POST
def kill_chain(request, pk):
    return _kill_chain_impl(request, pk, lock_only=False, require_mfa=True)


@login_required
@require_POST
def kill_lock_chain(request, pk):
    return _kill_chain_impl(request, pk, lock_only=True, require_mfa=True)


@login_required
def add_instance(request):
    _require_staff(request)
    if request.method == "POST":
        control_username = request.POST.get("control_username", "").strip()
        control_password = request.POST.get("control_password", "")
        if control_username and not control_password:
            messages.error(request, "Lock-control password is required when a lock-control username is provided.")
            return render(request, "monitor/add_instance.html", {"prefill": {
                key: request.POST.get(key, "")
                for key in ("name", "db_identifier", "region", "host", "port", "db_name", "username", "control_username", "owner_teams_webhook_url")
            }})
        instance = RDSInstance(
            name=request.POST["name"],
            db_identifier=request.POST["db_identifier"],
            region=request.POST.get("region", "us-east-1"),
            host=request.POST["host"],
            port=request.POST.get("port") or 5432,
            db_name=request.POST["db_name"],
            username=request.POST["username"],
            control_username=control_username,
            owner_teams_webhook_url=request.POST.get("owner_teams_webhook_url", "").strip(),
            exclude_from_global_notifications=bool(request.POST.get("exclude_from_global_notifications")),
            ssl_required=bool(request.POST.get("ssl_required")),
            added_by=request.user,
        )
        instance.set_password(request.POST["password"])
        if control_password:
            instance.set_control_password(control_password)
        instance.save()
        _log_audit(instance, "add_instance", request.user, detail=instance.db_identifier)
        messages.success(request, f"Added {instance.name}.")
        return redirect("instance-list")

    prefill = None
    duplicate_pk = request.GET.get("duplicate")
    if duplicate_pk:
        source = get_object_or_404(RDSInstance, pk=duplicate_pk)
        prefill = {
            "region": source.region,
            "host": source.host,
            "port": source.port,
            "username": source.username,
            "control_username": source.control_username,
            "owner_teams_webhook_url": source.owner_teams_webhook_url,
            "exclude_from_global_notifications": source.exclude_from_global_notifications,
            "password": source.get_password(),
            "ssl_required": source.ssl_required,
        }
    return render(request, "monitor/add_instance.html", {"prefill": prefill})


@login_required
@require_POST
def test_connection(request):
    """HTMX endpoint for the Add form's 'Test connection' button — tries the
    creds currently in the form, doesn't touch the database."""
    _require_staff(request)
    ok, error = db.test_connection(
        host=request.POST.get("host", ""),
        port=request.POST.get("port") or 5432,
        db_name=request.POST.get("db_name", ""),
        username=request.POST.get("username", ""),
        password=request.POST.get("password", ""),
        ssl_required=bool(request.POST.get("ssl_required")),
    )
    return render(request, "monitor/partials/test_connection_result.html", {"ok": ok, "error": error})


@login_required
@require_POST
def test_control_connection(request, pk):
    _require_staff(request)
    instance = get_object_or_404(RDSInstance, pk=pk)
    try:
        conn = db.get_connection(instance)
        conn.close()
        messages.success(request, "Lock-control connection succeeded.")
    except db.ConnectionError as exc:
        messages.error(request, f"Lock-control connection failed: {exc}")
    return redirect("instance-detail", pk=instance.pk)


@login_required
@require_POST
def rename_instance(request, pk):
    _require_staff(request)
    instance = get_object_or_404(RDSInstance, pk=pk)
    new_name = request.POST.get("name", "").strip()
    if not new_name:
        messages.error(request, "Name cannot be empty.")
        return redirect("instance-detail", pk=instance.pk)

    old_name = instance.name
    instance.name = new_name
    instance.save(update_fields=["name"])
    _log_audit(instance, "rename_instance", request.user, detail=f"{old_name} -> {new_name}")
    messages.success(request, f"Renamed to {new_name}.")
    return redirect("instance-detail", pk=instance.pk)


@login_required
@require_POST
def update_owner_webhook(request, pk):
    _require_staff(request)
    instance = get_object_or_404(RDSInstance, pk=pk)
    webhook_url = request.POST.get("owner_teams_webhook_url", "").strip()
    if len(webhook_url) > 2048:
        messages.error(request, "The Teams webhook URL must be 2048 characters or fewer.")
        return redirect("instance-detail", pk=instance.pk)
    if webhook_url:
        try:
            URLValidator()(webhook_url)
        except ValidationError:
            messages.error(request, "Enter a valid Teams webhook URL or leave the field blank.")
            return redirect("instance-detail", pk=instance.pk)
    instance.owner_teams_webhook_url = webhook_url
    instance.exclude_from_global_notifications = bool(request.POST.get("exclude_from_global_notifications"))
    instance.save(update_fields=["owner_teams_webhook_url", "exclude_from_global_notifications"])
    messages.success(request, "Owner Teams webhook and global notification setting saved.")
    return redirect("instance-detail", pk=instance.pk)


@login_required
@require_POST
def update_control_credentials(request, pk):
    _require_staff(request)
    instance = get_object_or_404(RDSInstance, pk=pk)
    control_username = request.POST.get("control_username", "").strip()
    control_password = request.POST.get("control_password", "")
    if control_username and not control_password:
        messages.error(request, "Enter the lock-control password, or clear the username to use the DB username.")
        return redirect("instance-detail", pk=instance.pk)

    instance.control_username = control_username
    if control_username:
        instance.set_control_password(control_password)
    else:
        instance.control_password_encrypted = ""
    instance.save(update_fields=["control_username", "control_password_encrypted"])
    messages.success(request, "Lock-control credentials saved. Monitoring and kill actions will use them immediately.")
    return redirect("instance-detail", pk=instance.pk)


@login_required
@require_POST
def remove_instance(request, pk):
    _require_administrator(request)
    instance = get_object_or_404(RDSInstance, pk=pk)
    if not _require_action_mfa(request, instance, "remove_instance", detail=instance.db_identifier, require_code=True):
        return redirect("instance-detail", pk=instance.pk)
    _log_audit(instance, "remove_instance", request.user, detail=instance.db_identifier)
    instance.delete()
    messages.success(request, "Instance removed.")
    return redirect("instance-list")
