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
        activity, blocking, error = cached
    else:
        try:
            activity, blocking = db.fetch_activity(instance)
            error = None
        except db.ConnectionError as exc:
            activity, blocking, error = [], [], str(exc)
        cache.set(cache_key, (activity, blocking, error), timeout=settings.ACTIVITY_CACHE_TTL)

    blocking_pids = {row["blocking_pid"] for row in blocking}
    return render(
        request,
        "monitor/partials/activity_table.html",
        {
            "instance": instance,
            "activity": activity,
            "blocking": blocking,
            "blocking_pids": blocking_pids,
            "error": error,
            "is_staff": request.user.is_staff,
        },
    )


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
