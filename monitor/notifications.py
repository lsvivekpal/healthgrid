import csv
import io
import json
import logging
import urllib.request
import ipaddress
import socket
from datetime import timedelta
from urllib.parse import quote, urlsplit
from zoneinfo import ZoneInfo

from django.conf import settings
from django.core.signing import TimestampSigner
from django.db import models
from django.utils import timezone

from .models import LockAlert, LockReport, NotificationSettings, RDSInstance


logger = logging.getLogger(__name__)
DISPLAY_TIMEZONE = ZoneInfo("Asia/Kolkata")
REPORT_MAX_AGE_SECONDS = 7 * 24 * 60 * 60
MAX_INLINE_REPORT_BYTES = 20 * 1024


def lock_notifications_open(config, now=None):
    """Return whether lock alerts may be delivered in the configured IST window."""
    if not config.notification_schedule_enabled:
        return True
    local_time = timezone.localtime(now or timezone.now(), DISPLAY_TIMEZONE).time()
    start = config.notification_start_time
    end = config.notification_end_time
    if start == end:
        return True
    if start < end:
        return start <= local_time < end
    # Support an intentional overnight window, e.g. 21:00–09:00.
    return local_time >= start or local_time < end


def _short_query(query, limit=600):
    query = " ".join(str(query or "").split())
    return query if len(query) <= limit else query[: limit - 1] + "…"


def _human_duration(seconds):
    seconds = max(0, int(seconds or 0))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60:02d}s"
    return f"{seconds // 3600}h {(seconds % 3600) // 60:02d}m"


def _timestamp(value):
    if not value:
        return "—"
    return timezone.localtime(value, DISPLAY_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S IST")


def _pair_text(keys, limit=3200):
    pairs = [str(key).replace(":", " → ", 1) for key in keys]
    text = " · ".join(pairs)
    if len(text) <= limit:
        return text or "—"
    visible = text[: limit - 30].rsplit(" · ", 1)[0]
    return f"{visible} · … ({len(pairs)} total)"


def _summary_message(instance, alerts, event, previous_active_keys, cleared_keys, sent_at, cleared_alerts=None):
    resolved = event == "cleared"
    state = "LOCK SUMMARY CLEARED" if resolved else ("LOCK STORM" if event == "storm" else "LOCK SUMMARY")
    status = "CLEARED" if resolved else "ACTIVE"
    event_label = {"initial": "INITIAL SUMMARY", "update": "ACTIVE SUMMARY UPDATE", "cleared": "ALL LOCKS CLEARED", "storm": "LOCK STORM DETECTED"}[event]
    first_seen = min((alert.first_seen_at for alert in alerts), default=None)
    last_seen = max((alert.last_seen_at for alert in alerts), default=None)
    current_keys = [alert.alert_key for alert in alerts]
    previous_count = len(previous_active_keys)
    cleared_count = len(cleared_keys)
    active_count = len(alerts)
    active_text = _pair_text(current_keys)
    cleared_text = _pair_text(cleared_keys)
    cleared_alerts = list(cleared_alerts or [])
    manual_clears = [alert for alert in cleared_alerts if alert.clear_reason == "manual_kill"]
    clear_reason = "MANUALLY KILLED" if manual_clears else ("AUTO-CLEARED" if cleared_alerts else "—")
    cleared_by = _pair_text(sorted({alert.cleared_by for alert in manual_clears if alert.cleared_by}), limit=1200)
    killed_pids = sorted({pid for alert in manual_clears for pid in (alert.blocked_pid, alert.blocking_pid)})
    if resolved:
        if manual_clears:
            message = f"All tracked locks are cleared. Manual kill recorded by {cleared_by} for PID(s): {_pair_text(killed_pids, limit=600)}."
        else:
            message = "All tracked locks are cleared automatically."
    elif event == "storm":
        message = f"{active_count} simultaneous locks detected in one check. Action required: review the blocking sessions. This storm alert will not repeat until all locks clear."
    elif active_count == 1:
        message = "1 lock is still active. Action required: review the blocking session."
    else:
        message = f"{active_count} locks are still active. Action required: review the blocking sessions."
    return (
        f"HealthGrid: {state}\n"
        f"Status: {status}\n"
        f"Alert event: {event_label}\n"
        f"Database: {instance.name} ({instance.db_identifier})\n"
        f"Region: {instance.region}\n"
        f"Active locks: {active_count}\n"
        f"Active PID pairs: {active_text}\n"
        f"Cleared since previous card: {cleared_count}\n"
        f"Cleared PID pairs: {cleared_text}\n"
        f"Clear reason: {clear_reason}\n"
        f"Cleared by: {cleared_by}\n"
        f"Previously active: {previous_count}\n"
        f"First observed: {_timestamp(first_seen)}\n"
        f"Last observed: {_timestamp(last_seen or sent_at)}\n"
        f"Sent at: {_timestamp(sent_at)}\n"
        f"Message: {message}"
    )


def _query_detail_container(title, alerts, max_items=10):
    if not alerts:
        return None
    visible_alerts = list(alerts)[:max_items]
    items = [{"type": "TextBlock", "text": title, "weight": "Bolder", "size": "Small"}]
    for alert in visible_alerts:
        resolution = " · MANUALLY KILLED" if alert.clear_reason == "manual_kill" else (" · AUTO-CLEARED" if alert.clear_reason == "auto_clear" else "")
        cleared_by = f" by {alert.cleared_by}" if alert.clear_reason == "manual_kill" and alert.cleared_by else ""
        headline = {"type": "TextBlock", "text": f"{alert.blocked_pid} → {alert.blocking_pid} · {alert.blocked_user or '—'} → {alert.blocking_user or '—'}{resolution}{cleared_by}", "weight": "Bolder", "size": "Small", "spacing": "Small"}
        if alert.clear_reason == "manual_kill":
            headline["color"] = "Good"
        items.extend([
            headline,
            {"type": "TextBlock", "text": f"Blocked: {_short_query(alert.blocked_query, limit=420)}", "wrap": True, "fontType": "Monospace", "size": "Small", "spacing": "None"},
            {"type": "TextBlock", "text": f"Blocking: {_short_query(alert.blocking_query, limit=420)}", "wrap": True, "fontType": "Monospace", "size": "Small", "spacing": "None"},
        ])
    if len(alerts) > max_items:
        items.append({"type": "TextBlock", "text": f"Showing {max_items} of {len(alerts)} query pairs. Use the dashboard CSV export for the complete list.", "wrap": True, "size": "Small", "color": "Warning", "spacing": "Small"})
    return {"type": "Container", "style": "emphasis", "spacing": "Medium", "items": items}


def _adaptive_card(message, alerts=None, cleared_alerts=None, report=None):
    lines = message.split("\n")
    values = {}
    for line in lines:
        if ": " in line:
            key, value = line.split(": ", 1)
            values[key] = value
    state = values.get("HealthGrid", "")
    is_test = state == "TEST NOTIFICATION"
    resolved = values.get("Status") == "CLEARED"
    is_report = state == "WEEKLY LOCK REPORT"
    title = (
        "Teams notification test"
        if is_test
        else "Weekly lock report"
        if is_report
        else "All locks cleared"
        if resolved
        else "Database locks still active"
    )
    status_color = "Accent" if is_test or is_report else ("Good" if resolved else "Attention")
    message_color = "Accent" if is_test or is_report else ("Good" if resolved else "Attention")
    card_style = "emphasis" if is_test or is_report else ("good" if resolved else "attention")
    body = [
        {
            "type": "Container",
            "style": card_style,
            "bleed": True,
            "items": [
                {"type": "TextBlock", "text": "HealthGrid", "weight": "Bolder", "size": "Small", "color": status_color},
                {"type": "TextBlock", "text": title, "weight": "Bolder", "size": "Large", "color": status_color, "spacing": "None"},
            ],
        },
        {
            "type": "Container",
            "style": card_style,
            "spacing": "Medium",
            "items": [
                {"type": "TextBlock", "text": "Status message", "weight": "Bolder", "size": "Small", "color": "Good" if resolved else "Attention"},
                {"type": "TextBlock", "text": values.get("Message", "—"), "wrap": True, "weight": "Bolder", "size": "Medium", "color": message_color, "spacing": "Small"},
            ],
        },
        {
            "type": "FactSet",
            "spacing": "Medium",
            "facts": [
                {"title": "Status", "value": values.get("Status", "—")},
                {"title": "Database", "value": values.get("Database", "—")},
                {"title": "Region", "value": values.get("Region", "—")},
                {"title": "Active locks", "value": values.get("Active locks", "0")},
                {"title": "Active PID pairs", "value": values.get("Active PID pairs", "—")},
                {"title": "Cleared since previous", "value": values.get("Cleared since previous card", "0")},
                {"title": "Cleared PID pairs", "value": values.get("Cleared PID pairs", "—")},
                {"title": "Clear reason", "value": values.get("Clear reason", "—")},
                {"title": "Cleared by", "value": values.get("Cleared by", "—")},
                {"title": "Alert event", "value": values.get("Alert event", "—")},
                {"title": "First observed", "value": values.get("First observed", "—")},
                {"title": "Last observed", "value": values.get("Last observed", "—")},
                {"title": "Sent at", "value": values.get("Sent at", "—")},
                {"title": "Previously active", "value": values.get("Previously active", "0")},
            ],
        },
    ]
    if is_report:
        body[2]["facts"].extend([
            {"title": "Report rows", "value": values.get("Report rows", "0")},
            {"title": "File name", "value": values.get("File name", "—")},
            {"title": "Delivery", "value": values.get("Delivery", "See Flow instructions")},
        ])
    active_details = _query_detail_container("Active query details", alerts or [])
    cleared_details = _query_detail_container("Cleared query details", cleared_alerts or [])
    if active_details:
        body.append(active_details)
    if cleared_details:
        body.append(cleared_details)
    return {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.4",
        "body": body,
        "actions": [],
    }


def _post_webhook(webhook_url, payload):
    parts = urlsplit(webhook_url)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        raise ValueError("Webhook URL must be an HTTPS URL without embedded credentials")
    hostname = parts.hostname.lower().rstrip(".")
    allowed = settings.WEBHOOK_ALLOWED_HOSTS
    if allowed and not any(hostname == item or hostname.endswith("." + item.lstrip("*.")) for item in allowed):
        raise ValueError("Webhook hostname is not in WEBHOOK_ALLOWED_HOSTS")
    addresses = {item[4][0] for item in socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)}
    for address in addresses:
        parsed = ipaddress.ip_address(address)
        if parsed.is_private or parsed.is_loopback or parsed.is_link_local or parsed.is_reserved or parsed.is_multicast:
            raise ValueError("Webhook destination resolves to a private or reserved address")

    request = urllib.request.Request(
        webhook_url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    opener = urllib.request.build_opener(_NoRedirectHandler())
    with opener.open(request, timeout=10) as response:
        return 200 <= response.status < 300


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("Webhook redirects are not permitted")


def _weekly_alerts(now=None, instance=None):
    now = now or timezone.now()
    cutoff = now - timedelta(days=7)
    queryset = LockAlert.objects.filter(
        first_seen_at__lte=now,
    ).filter(
        # Include incidents that started, changed, or cleared in the report window.
        models.Q(first_seen_at__gte=cutoff)
        | models.Q(last_seen_at__gte=cutoff)
        | models.Q(resolved_at__gte=cutoff)
    )
    if instance is not None:
        queryset = queryset.filter(instance=instance)
    return list(queryset.select_related("instance").order_by("instance__name", "first_seen_at", "blocked_pid", "blocking_pid"))


def _csv_report(alerts, now):
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow([
        "database", "db_identifier", "region", "status", "blocked_pid", "blocked_user",
        "blocking_pid", "blocking_user", "first_observed_ist", "last_observed_ist",
        "cleared_at_ist", "clear_reason", "cleared_by", "waiting_seconds", "blocked_query", "blocking_query",
    ])
    for alert in alerts:
        writer.writerow([
            alert.instance.name,
            alert.instance.db_identifier,
            alert.instance.region,
            "ACTIVE" if alert.resolved_at is None else "CLEARED",
            alert.blocked_pid,
            alert.blocked_user,
            alert.blocking_pid,
            alert.blocking_user,
            _timestamp(alert.first_seen_at),
            _timestamp(alert.last_seen_at),
            _timestamp(alert.resolved_at),
            alert.clear_reason or "",
            alert.cleared_by or "",
            alert.waiting_seconds,
            alert.blocked_query,
            alert.blocking_query,
        ])
    return output.getvalue()


def _report_url(report, report_base_url):
    if not report_base_url:
        return ""
    token = TimestampSigner().sign(str(report.pk))
    return f"{report_base_url.rstrip('/')}/reports/weekly/{quote(token, safe='')}/"


def _weekly_report_message(*, scope, file_name, row_count, report_url):
    delivery = "Power Automate downloads the signed report URL" if report_url else "Power Automate creates the file from csv_content"
    return (
        "HealthGrid: WEEKLY LOCK REPORT\n"
        "Status: REPORT READY\n"
        f"Database: {scope}\n"
        "Region: —\n"
        f"Report rows: {row_count}\n"
        f"File name: {file_name}\n"
        f"Delivery: {delivery}\n"
        f"Generated at: {_timestamp(timezone.now())}\n"
        "Message: Weekly blocked/blocking lock history is ready for OneDrive."
    )


def _send_report_to_webhook(webhook_url, *, scope, alerts, config, now):
    if not webhook_url:
        return False
    csv_content = _csv_report(alerts, now)
    file_name = f"rds-lock-report-{now.astimezone(DISPLAY_TIMEZONE):%Y%m%d-%H%M%S}.csv"
    report = LockReport.objects.create(
        file_name=file_name,
        csv_content=csv_content,
        expires_at=now + timedelta(days=config.report_retention_days),
    )
    report_url = _report_url(report, config.report_base_url)
    content_bytes = csv_content.encode("utf-8")
    if not report_url and len(content_bytes) > MAX_INLINE_REPORT_BYTES:
        logger.error(
            "Weekly lock report for %s is %d bytes; configure the public report base URL before sending large reports",
            scope,
            len(content_bytes),
        )
        return False
    message = _weekly_report_message(
        scope=scope,
        file_name=file_name,
        row_count=len(alerts),
        report_url=report_url,
    )
    payload = {
        "event_type": "weekly_report",
        "card": _adaptive_card(message, report={"file_name": file_name, "row_count": len(alerts), "report_url": report_url}),
        "file_name": file_name,
        "csv_content": "" if report_url else csv_content,
        "report_url": report_url,
        "report_rows": len(alerts),
    }
    try:
        return _post_webhook(webhook_url, payload)
    except Exception:
        logger.exception("Could not send weekly lock report for %s", scope)
        return False


def send_weekly_reports(now=None):
    """Send the weekly report through the existing channel/owner webhooks."""
    now = now or timezone.now()
    config = NotificationSettings.load()
    LockReport.objects.filter(expires_at__lt=now).delete()
    delivered = False
    # The exclusion controls real-time global alert cards only. Weekly reports
    # intentionally retain the complete seven-day lock history.
    all_alerts = _weekly_alerts(now)
    if config.channel_webhook_url and all_alerts:
        delivered = _send_report_to_webhook(
            config.channel_webhook_url,
            scope="All monitored databases",
            alerts=all_alerts,
            config=config,
            now=now,
        ) or delivered
    for instance in RDSInstance.objects.filter(is_active=True).exclude(owner_teams_webhook_url=""):
        delivered = _send_report_to_webhook(
            instance.owner_teams_webhook_url,
            scope=str(instance),
            alerts=_weekly_alerts(now, instance=instance),
            config=config,
            now=now,
        ) or delivered
    if not config.channel_webhook_url and not RDSInstance.objects.filter(is_active=True).exclude(owner_teams_webhook_url="").exists():
        logger.warning("No Teams webhook is configured; skipping weekly lock report")
    return delivered


def send_test_notification(webhook_url, destination="Teams"):
    """Send a small connectivity test to one Teams Workflow webhook."""
    if not webhook_url:
        return False, "No Teams webhook URL is configured."
    message = (
        "HealthGrid: TEST NOTIFICATION\n"
        f"Destination: {destination}\n"
        "Status: Delivery successful"
    )
    try:
        if _post_webhook(webhook_url, {"event_type": "lock_alert", "card": _adaptive_card(message)}):
            return True, "Test notification sent."
        return False, "Teams returned a non-success HTTP status."
    except Exception as exc:
        logger.exception("Could not send Teams test notification")
        return False, str(exc)


def notify_lock_summary(instance, alerts, *, event, previous_active_keys, cleared_keys, sent_at, cleared_alerts=None):
    """Send one aggregate lock summary to the channel and owner webhook."""
    message = _summary_message(instance, alerts, event, previous_active_keys, cleared_keys, sent_at, cleared_alerts=cleared_alerts)
    delivered = False

    config = NotificationSettings.load()
    webhook_urls = list(dict.fromkeys(filter(None, [
        "" if instance.exclude_from_global_notifications else config.channel_webhook_url,
        instance.owner_teams_webhook_url,
    ])))
    for webhook_url in filter(None, webhook_urls):
        try:
            if _post_webhook(webhook_url, {
                "event_type": "lock_alert",
                "card": _adaptive_card(message, alerts=alerts, cleared_alerts=cleared_alerts),
            }):
                delivered = True
            else:
                logger.error("Teams webhook returned a non-success HTTP status")
        except Exception:
            logger.exception("Could not send Teams lock notification for %s", instance)
    if not webhook_urls:
        logger.warning("No Teams webhook is configured; skipping Teams alert")

    return delivered


def _long_query_card(instance, alerts, sent_at):
    items = [
        {
            "type": "Container",
            "style": "attention",
            "bleed": True,
            "items": [
                {"type": "TextBlock", "text": "HealthGrid", "weight": "Bolder", "size": "Small", "color": "Attention"},
                {"type": "TextBlock", "text": "Long-running manual query detected", "weight": "Bolder", "size": "Large", "color": "Attention", "spacing": "None"},
            ],
        },
        {
            "type": "TextBlock",
            "text": f"{len(alerts)} query execution(s) exceeded the configured runtime threshold. Review the user and query before terminating anything.",
            "wrap": True,
            "weight": "Bolder",
            "spacing": "Medium",
        },
        {
            "type": "FactSet",
            "facts": [
                {"title": "Database", "value": str(instance)},
                {"title": "Region", "value": instance.region},
                {"title": "Detected at", "value": _timestamp(sent_at)},
            ],
        },
    ]
    for alert in list(alerts)[:20]:
        items.append({
            "type": "Container",
            "style": "emphasis",
            "spacing": "Medium",
            "items": [
                {"type": "TextBlock", "text": f"PID {alert.pid} · user {alert.username or '—'}", "weight": "Bolder", "size": "Medium"},
                {"type": "FactSet", "facts": [
                    {"title": "Runtime", "value": _human_duration(alert.duration_seconds)},
                    {"title": "Application", "value": alert.application_name or "—"},
                    {"title": "Client", "value": alert.client_addr or "local"},
                    {"title": "Query started", "value": _timestamp(alert.query_start)},
                ]},
                {"type": "TextBlock", "text": _short_query(alert.query, limit=3000), "wrap": True, "fontType": "Monospace", "size": "Small", "spacing": "Small"},
            ],
        })
    if len(alerts) > 20:
        items.append({"type": "TextBlock", "text": f"Showing 20 of {len(alerts)} queries. Use the dashboard Sessions view for the complete list.", "wrap": True, "size": "Small", "color": "Warning"})
    return {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.4",
        "body": items,
        "actions": [],
    }


def notify_long_query(instance, alerts, *, sent_at):
    """Send one card for newly detected long manual queries to both destinations."""
    if not alerts:
        return False
    card = _long_query_card(instance, alerts, sent_at)
    delivered = False
    config = NotificationSettings.load()
    webhook_urls = list(dict.fromkeys(filter(None, [
        "" if instance.exclude_from_global_notifications else config.channel_webhook_url,
        instance.owner_teams_webhook_url,
    ])))
    for webhook_url in webhook_urls:
        try:
            if _post_webhook(webhook_url, {
                # Keep the existing Power Automate lock-alert branch compatible.
                "event_type": "lock_alert",
                "alert_type": "long_query",
                "card": card,
            }):
                delivered = True
            else:
                logger.error("Teams webhook returned a non-success HTTP status")
        except Exception:
            logger.exception("Could not send long-query notification for %s", instance)
    if not webhook_urls:
        logger.warning("No Teams webhook is configured; skipping long-query alert")
    return delivered
