import json
import logging
import urllib.request

from django.conf import settings


logger = logging.getLogger(__name__)


def _short_query(query, limit=600):
    query = " ".join(str(query or "").split())
    return query if len(query) <= limit else query[: limit - 1] + "…"


def _message(instance, alert, resolved=False):
    state = "CLEARED" if resolved else "BLOCKED LOCK"
    duration = alert.last_seen_at - alert.first_seen_at
    duration_seconds = max(0, int(duration.total_seconds()))
    return (
        f"RDS Dashboard: {state}\n"
        f"Database: {instance.name} ({instance.db_identifier})\n"
        f"Region: {instance.region}\n"
        f"Blocked PID: {alert.blocked_pid}\n"
        f"Blocking PID: {alert.blocking_pid}\n"
        f"Observed for: {duration_seconds}s\n"
        f"Blocked query: {_short_query(alert.blocked_query)}\n"
        f"Blocking query: {_short_query(alert.blocking_query)}"
    )


def notify_lock(instance, alert, *, resolved=False):
    """Send one lock event to the configured Teams channel and owner email."""
    message = _message(instance, alert, resolved=resolved)
    delivered = False

    webhook_urls = list(dict.fromkeys(filter(None, [
        getattr(settings, "TEAMS_LOCK_WEBHOOK_URL", ""),
        instance.owner_teams_webhook_url,
    ])))
    for webhook_url in filter(None, webhook_urls):
        try:
            payload = json.dumps({"text": message}).encode("utf-8")
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
        logger.warning("TEAMS_LOCK_WEBHOOK_URL is not configured; skipping Teams alert")

    return delivered
