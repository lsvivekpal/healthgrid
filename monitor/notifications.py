import json
import logging
import urllib.request
from zoneinfo import ZoneInfo

from django.utils import timezone

from .models import NotificationSettings


logger = logging.getLogger(__name__)
DISPLAY_TIMEZONE = ZoneInfo("Asia/Kolkata")


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


def _summary_message(instance, alerts, event, previous_active_keys, cleared_keys, sent_at):
    resolved = event == "cleared"
    state = "LOCK SUMMARY CLEARED" if resolved else "LOCK SUMMARY"
    status = "CLEARED" if resolved else "ACTIVE"
    event_label = {"initial": "INITIAL SUMMARY", "update": "ACTIVE SUMMARY UPDATE", "cleared": "ALL LOCKS CLEARED"}[event]
    first_seen = min((alert.first_seen_at for alert in alerts), default=None)
    last_seen = max((alert.last_seen_at for alert in alerts), default=None)
    current_keys = [alert.alert_key for alert in alerts]
    previous_count = len(previous_active_keys)
    cleared_count = len(cleared_keys)
    active_count = len(alerts)
    active_text = _pair_text(current_keys)
    cleared_text = _pair_text(cleared_keys)
    if resolved:
        message = "All tracked locks are cleared."
    elif active_count == 1:
        message = "1 lock is still active. Action required: review the blocking session."
    else:
        message = f"{active_count} locks are still active. Action required: review the blocking sessions."
    return (
        f"RDS Dashboard: {state}\n"
        f"Status: {status}\n"
        f"Alert event: {event_label}\n"
        f"Database: {instance.name} ({instance.db_identifier})\n"
        f"Region: {instance.region}\n"
        f"Active locks: {active_count}\n"
        f"Active PID pairs: {active_text}\n"
        f"Cleared since previous card: {cleared_count}\n"
        f"Cleared PID pairs: {cleared_text}\n"
        f"Previously active: {previous_count}\n"
        f"First observed: {_timestamp(first_seen)}\n"
        f"Last observed: {_timestamp(last_seen or sent_at)}\n"
        f"Sent at: {_timestamp(sent_at)}\n"
        f"Message: {message}"
    )


def _adaptive_card(message):
    lines = message.split("\n")
    values = {}
    for line in lines:
        if ": " in line:
            key, value = line.split(": ", 1)
            values[key] = value
    state = values.get("RDS Dashboard", "")
    is_test = state == "TEST NOTIFICATION"
    resolved = values.get("Status") == "CLEARED"
    title = "Teams notification test" if is_test else ("All locks cleared" if resolved else "Database locks still active")
    status_color = "Accent" if is_test else ("Good" if resolved else "Attention")
    return {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.4",
        "body": [
            {
                "type": "Container",
                "style": "good" if resolved else "attention",
                "bleed": True,
                "items": [
                    {"type": "TextBlock", "text": "RDS Dashboard", "weight": "Bolder", "size": "Small", "color": status_color},
                    {"type": "TextBlock", "text": title, "weight": "Bolder", "size": "Large", "color": status_color, "spacing": "None"},
                ],
            },
            {
                "type": "Container",
                "style": "good" if resolved else "attention",
                "spacing": "Medium",
                "items": [
                    {"type": "TextBlock", "text": "Status message", "weight": "Bolder", "size": "Small", "color": "Good" if resolved else "Attention"},
                    {"type": "TextBlock", "text": values.get("Message", "—"), "wrap": True, "weight": "Bolder", "size": "Medium", "color": "Good" if resolved else "Attention", "spacing": "Small"},
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
                    {"title": "Alert event", "value": values.get("Alert event", "—")},
                    {"title": "First observed", "value": values.get("First observed", "—")},
                    {"title": "Last observed", "value": values.get("Last observed", "—")},
                    {"title": "Sent at", "value": values.get("Sent at", "—")},
                    {"title": "Previously active", "value": values.get("Previously active", "0")},
                ],
            },
        ],
        "actions": [],
    }


def send_test_notification(webhook_url, destination="Teams"):
    """Send a small connectivity test to one Teams Workflow webhook."""
    if not webhook_url:
        return False, "No Teams webhook URL is configured."
    message = (
        "RDS Dashboard: TEST NOTIFICATION\n"
        f"Destination: {destination}\n"
        "Status: Delivery successful"
    )
    try:
        payload = json.dumps(_adaptive_card(message)).encode("utf-8")
        request = urllib.request.Request(
            webhook_url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            if 200 <= response.status < 300:
                return True, "Test notification sent."
            return False, f"Teams returned HTTP {response.status}."
    except Exception as exc:
        logger.exception("Could not send Teams test notification")
        return False, str(exc)


def notify_lock_summary(instance, alerts, *, event, previous_active_keys, cleared_keys, sent_at):
    """Send one aggregate lock summary to the channel and owner webhook."""
    message = _summary_message(instance, alerts, event, previous_active_keys, cleared_keys, sent_at)
    delivered = False

    webhook_urls = list(dict.fromkeys(filter(None, [
        NotificationSettings.load().channel_webhook_url,
        instance.owner_teams_webhook_url,
    ])))
    for webhook_url in filter(None, webhook_urls):
        try:
            payload = json.dumps(_adaptive_card(message)).encode("utf-8")
            request = urllib.request.Request(
                webhook_url,
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=10) as response:
                if 200 <= response.status < 300:
                    delivered = True
                else:
                    logger.error("Teams webhook returned HTTP %s", response.status)
        except Exception:
            logger.exception("Could not send Teams lock notification for %s", instance)
    if not webhook_urls:
        logger.warning("No Teams webhook is configured; skipping Teams alert")

    return delivered
