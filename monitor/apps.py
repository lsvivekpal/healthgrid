import os
import sys

from django.apps import AppConfig


class MonitorConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "monitor"

    def ready(self):
        # Avoid background threads during migrations/tests and the parent
        # process of Django's autoreloader. Gunicorn workers still start the
        # thread; the database lease ensures only one worker runs checks.
        ignored_commands = {"check", "collectstatic", "makemigrations", "migrate", "monitor_locks", "showmigrations", "shell", "test"}
        if os.environ.get("DISABLE_EMBEDDED_LOCK_MONITOR") == "1":
            return
        if len(sys.argv) > 1 and sys.argv[1] in ignored_commands:
            return
        if sys.argv[1:2] == ["runserver"] and os.environ.get("RUN_MAIN") != "true":
            return
        from .background import start_monitor_thread
        start_monitor_thread()
