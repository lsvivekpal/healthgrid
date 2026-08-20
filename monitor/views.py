import csv
import logging

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.core.exceptions import PermissionDenied
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST
from rest_framework import viewsets

from . import db
from .models import AuditLog, RDSInstance
from .permissions import IsStaffOrReadOnly
from .serializers import AuditLogSerializer, RDSInstanceSerializer

logger = logging.getLogger(__name__)


def _require_staff(request):
    if not request.user.is_staff:
        raise PermissionDenied("Staff permission required for this action.")


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


# --- DRF API: instance registry CRUD ---


class RDSInstanceViewSet(viewsets.ModelViewSet):
    queryset = RDSInstance.objects.all()
    serializer_class = RDSInstanceSerializer
    permission_classes = [IsStaffOrReadOnly]

    def perform_create(self, serializer):
        instance = serializer.save(added_by=self.request.user)
        _log_audit(instance, "add_instance", self.request.user)

    def perform_destroy(self, instance):
        _log_audit(instance, "remove_instance", self.request.user, detail=instance.db_identifier)
        instance.delete()


class AuditLogViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = AuditLogSerializer

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
    return render(request, "monitor/instance_list.html", {"instances": instances})


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
        {"instance": instance, "slots": slots, "error": error, "is_staff": request.user.is_staff},
    )


@login_required
@require_POST
def kill_replication_slot(request, pk):
    """Terminate the walsender backend behind a slot — a prerequisite for dropping it."""
    instance = get_object_or_404(RDSInstance, pk=pk)
    _require_staff(request)
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
    instance = get_object_or_404(RDSInstance, pk=pk)
    _require_staff(request)
    slot_name = request.POST.get("slot_name", "")

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


@login_required
@require_POST
def kill_session(request, pk):
    instance = get_object_or_404(RDSInstance, pk=pk)
    _require_staff(request)

    pid = int(request.POST["pid"])
    query_snapshot = request.POST.get("query", "")

    try:
        terminated = db.kill_pid(instance, pid)
        _log_audit(
            instance, "kill_session", request.user,
            pid=pid, query=query_snapshot,
            result="success" if terminated else "failed",
            detail="" if terminated else "pid was not running",
        )
        if terminated:
            messages.success(request, f"Terminated backend pid {pid}.")
        else:
            messages.warning(request, f"pid {pid} was not running.")
    except db.ConnectionError as exc:
        _log_audit(instance, "kill_session", request.user, pid=pid, query=query_snapshot, result="failed", detail=str(exc))
        messages.error(request, f"Could not connect to kill pid {pid}: {exc}")

    cache.delete(f"activity:{instance.pk}")
    return redirect("instance-detail", pk=instance.pk)


@login_required
@require_POST
def kill_chain(request, pk):
    """Kill every pid in a blocking chain (blocked + blocking) in one action."""
    instance = get_object_or_404(RDSInstance, pk=pk)
    _require_staff(request)

    pids = [int(p) for p in request.POST.getlist("pid")]
    query_snapshot = request.POST.get("query", "")
    action = "kill_chain" if request.POST.get("mode") == "chain" else "bulk_kill"

    try:
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
            messages.success(request, f"Terminated {len(terminated)} pid(s): {terminated}.")
        if not_running:
            messages.warning(request, f"Already gone: {not_running}.")
    except db.ConnectionError as exc:
        _log_audit(instance, "kill_chain", request.user, query=query_snapshot, result="failed", detail=str(exc))
        messages.error(request, f"Could not connect to kill chain: {exc}")

    cache.delete(f"activity:{instance.pk}")
    return redirect("instance-detail", pk=instance.pk)


@login_required
def add_instance(request):
    _require_staff(request)
    if request.method == "POST":
        instance = RDSInstance(
            name=request.POST["name"],
            db_identifier=request.POST["db_identifier"],
            region=request.POST.get("region", "us-east-1"),
            host=request.POST["host"],
            port=request.POST.get("port") or 5432,
            db_name=request.POST["db_name"],
            username=request.POST["username"],
            ssl_required=bool(request.POST.get("ssl_required")),
            added_by=request.user,
        )
        instance.set_password(request.POST["password"])
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
def remove_instance(request, pk):
    _require_staff(request)
    instance = get_object_or_404(RDSInstance, pk=pk)
    _log_audit(instance, "remove_instance", request.user, detail=instance.db_identifier)
    instance.delete()
    messages.success(request, "Instance removed.")
    return redirect("instance-list")
