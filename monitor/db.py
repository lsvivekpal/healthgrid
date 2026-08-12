import logging

import psycopg2
import psycopg2.extras

logger = logging.getLogger(__name__)

CONNECT_TIMEOUT_SECONDS = 5

# Active queries plus idle-in-transaction sessions (the #1 real-world lock cause) —
# plain idle connections doing nothing are excluded as noise.
ACTIVITY_QUERY = """
    SELECT pid, usename, application_name, client_addr, state,
           wait_event_type, wait_event, query, query_start,
           EXTRACT(EPOCH FROM (now() - query_start))::int AS duration_seconds
    FROM pg_stat_activity
    WHERE datname = current_database() AND pid != pg_backend_pid()
      AND state IN ('active', 'idle in transaction', 'idle in transaction (aborted)')
    ORDER BY query_start ASC NULLS LAST;
"""

# Canonical Postgres "who is blocking whom" query.
BLOCKING_QUERY = """
    SELECT
      blocked_locks.pid AS blocked_pid,
      blocked_activity.usename AS blocked_user,
      blocking_locks.pid AS blocking_pid,
      blocking_activity.usename AS blocking_user,
      blocked_activity.query AS blocked_query,
      blocking_activity.query AS blocking_query,
      blocked_activity.query_start AS blocked_query_start,
      EXTRACT(EPOCH FROM (now() - blocked_activity.query_start))::int AS waiting_seconds
    FROM pg_catalog.pg_locks blocked_locks
    JOIN pg_catalog.pg_stat_activity blocked_activity ON blocked_activity.pid = blocked_locks.pid
    JOIN pg_catalog.pg_locks blocking_locks
        ON blocking_locks.locktype = blocked_locks.locktype
        AND blocking_locks.database IS NOT DISTINCT FROM blocked_locks.database
        AND blocking_locks.relation IS NOT DISTINCT FROM blocked_locks.relation
        AND blocking_locks.page IS NOT DISTINCT FROM blocked_locks.page
        AND blocking_locks.tuple IS NOT DISTINCT FROM blocked_locks.tuple
        AND blocking_locks.virtualxid IS NOT DISTINCT FROM blocked_locks.virtualxid
        AND blocking_locks.transactionid IS NOT DISTINCT FROM blocked_locks.transactionid
        AND blocking_locks.classid IS NOT DISTINCT FROM blocked_locks.classid
        AND blocking_locks.objid IS NOT DISTINCT FROM blocked_locks.objid
        AND blocking_locks.objsubid IS NOT DISTINCT FROM blocked_locks.objsubid
        AND blocking_locks.pid != blocked_locks.pid
    JOIN pg_catalog.pg_stat_activity blocking_activity ON blocking_activity.pid = blocking_locks.pid
    WHERE NOT blocked_locks.granted;
"""


class ConnectionError(Exception):
    pass


def get_connection(instance):
    """Open a short-lived psycopg2 connection to the target RDS instance."""
    try:
        return psycopg2.connect(
            host=instance.host,
            port=instance.port,
            dbname=instance.db_name,
            user=instance.username,
            password=instance.get_password(),
            connect_timeout=CONNECT_TIMEOUT_SECONDS,
            sslmode="require" if instance.ssl_required else "prefer",
        )
    except Exception as exc:
        raise ConnectionError(str(exc)) from exc


def fetch_activity(instance):
    """Return (activity_rows, blocking_rows) for the instance."""
    conn = get_connection(instance)
    try:
        conn.set_session(readonly=True, autocommit=True)
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(ACTIVITY_QUERY)
            activity = list(cur.fetchall())
            cur.execute(BLOCKING_QUERY)
            blocking = list(cur.fetchall())
        return activity, blocking
    finally:
        conn.close()


def kill_pid(instance, pid):
    """Terminate a backend on the target instance. Returns True if it was running."""
    conn = get_connection(instance)
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SELECT pg_terminate_backend(%s);", (pid,))
            (terminated,) = cur.fetchone()
        return bool(terminated)
    finally:
        conn.close()


def kill_pids(instance, pids):
    """Terminate multiple backends over one connection. Returns {pid: terminated_bool}."""
    conn = get_connection(instance)
    results = {}
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            for pid in pids:
                cur.execute("SELECT pg_terminate_backend(%s);", (pid,))
                (terminated,) = cur.fetchone()
                results[pid] = bool(terminated)
        return results
    finally:
        conn.close()


def test_connection(host, port, db_name, username, password, ssl_required):
    """Try connecting with raw creds (not a saved instance) — for the Add form's
    'Test connection' button, before anything is persisted."""
    try:
        conn = psycopg2.connect(
            host=host,
            port=port,
            dbname=db_name,
            user=username,
            password=password,
            connect_timeout=CONNECT_TIMEOUT_SECONDS,
            sslmode="require" if ssl_required else "prefer",
        )
        conn.close()
        return True, None
    except Exception as exc:
        return False, str(exc)
