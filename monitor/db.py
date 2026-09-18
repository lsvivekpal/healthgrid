import logging

import psycopg2
import psycopg2.extras
from django.conf import settings

logger = logging.getLogger(__name__)

CONNECT_TIMEOUT_SECONDS = 5
STATEMENT_TIMEOUT_MILLISECONDS = 8000
LOCK_TIMEOUT_MILLISECONDS = 2000
IDLE_TRANSACTION_TIMEOUT_MILLISECONDS = 10000

# Active queries plus idle-in-transaction sessions (the #1 real-world lock cause) —
# plain idle connections doing nothing are excluded as noise.
ACTIVITY_QUERY = """
    SELECT pid, usename, application_name, client_addr, backend_type, state,
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

# One-glance DB vitals: connection pressure, session mix, worst query age, size.
# Longest-active excludes replication/background backends (backend_type =
# 'client backend') so a long-lived walsender never shows as "the longest query".
VITALS_QUERY = """
    SELECT
      (SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()) AS total_conns,
      (SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() AND state = 'active') AS active_conns,
      (SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() AND state = 'idle') AS idle_conns,
      (SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() AND state IN ('idle in transaction', 'idle in transaction (aborted)')) AS idle_in_txn,
      (SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() AND wait_event_type = 'Lock') AS waiting_on_locks,
      (SELECT setting::int FROM pg_settings WHERE name = 'max_connections') AS max_conns,
      pg_size_pretty(pg_database_size(current_database())) AS db_size,
      COALESCE((SELECT EXTRACT(EPOCH FROM (now() - min(query_start)))::int
                FROM pg_stat_activity
                WHERE datname = current_database() AND state = 'active'
                  AND backend_type = 'client backend' AND query_start IS NOT NULL), 0) AS longest_active_seconds;
"""

# Top 10 largest tables by total size (heap + indexes + toast), reported in GB.
# System schemas excluded. Cheap catalog read; safe to refresh with the poll.
TOP_TABLES_QUERY = """
    SELECT n.nspname || '.' || c.relname AS table_name,
           pg_total_relation_size(c.oid) AS total_bytes,
           round(pg_total_relation_size(c.oid) / 1073741824.0, 2) AS size_gb
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE c.relkind IN ('r', 'p', 'm')
      AND n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
    ORDER BY pg_total_relation_size(c.oid) DESC
    LIMIT 10;
"""


# pg_current_wal_lsn() only works on a primary; guard it so this doesn't error
# out when the monitored instance is a read replica.
REPLICATION_SLOTS_QUERY = """
    SELECT slot_name, plugin, slot_type, database, active, active_pid,
           restart_lsn, confirmed_flush_lsn,
           CASE WHEN pg_is_in_recovery() THEN NULL
                ELSE pg_size_pretty(pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn))
           END AS retained_wal
    FROM pg_replication_slots
    ORDER BY slot_name;
"""


class ConnectionError(Exception):
    pass


def get_connection(instance):
    """Open an independent control connection to the target RDS instance.

    A dedicated control role can be configured for monitoring and termination.
    It prevents a blocked application role from taking the dashboard's
    emergency path down with it.
    """
    use_control_credentials = instance.lock_control_enabled and bool(instance.control_username)
    username = instance.control_username if use_control_credentials else instance.username
    password = instance.get_control_password() if use_control_credentials else instance.get_password()
    try:
        return psycopg2.connect(
            host=instance.host,
            port=instance.port,
            dbname=instance.db_name,
            user=username,
            password=password,
            connect_timeout=CONNECT_TIMEOUT_SECONDS,
            sslmode=settings.DB_SSL_MODE if instance.ssl_required else "prefer",
            **({"sslrootcert": settings.DB_SSL_ROOT_CERT} if instance.ssl_required and settings.DB_SSL_ROOT_CERT else {}),
            application_name="healthgrid-control",
            options=(
                f"-c statement_timeout={STATEMENT_TIMEOUT_MILLISECONDS} "
                f"-c lock_timeout={LOCK_TIMEOUT_MILLISECONDS} "
                f"-c idle_in_transaction_session_timeout={IDLE_TRANSACTION_TIMEOUT_MILLISECONDS}"
            ),
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


def fetch_activity_with_vitals(instance):
    """Return (activity_rows, blocking_rows, vitals_dict, top_tables) over one
    connection. Used by the live dashboard so everything refreshes together."""
    conn = get_connection(instance)
    try:
        conn.set_session(readonly=True, autocommit=True)
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(ACTIVITY_QUERY)
            activity = list(cur.fetchall())
            cur.execute(BLOCKING_QUERY)
            blocking = list(cur.fetchall())
            try:
                cur.execute(VITALS_QUERY)
                vitals = cur.fetchone()
            except Exception as exc:  # vitals are best-effort; never fail the page over them
                logger.warning("vitals query failed for %s: %s", instance, exc)
                vitals = None
            try:
                cur.execute(TOP_TABLES_QUERY)
                top_tables = list(cur.fetchall())
            except Exception as exc:
                logger.warning("top-tables query failed for %s: %s", instance, exc)
                top_tables = []
        return activity, blocking, vitals, top_tables
    finally:
        conn.close()


# Check slot membership in the same statement as termination. Generic PID and
# bulk/lock-chain routes must not bypass the operator's slot-termination grant.
GUARDED_TERMINATE_QUERY = """
SELECT CASE
    WHEN %s OR NOT EXISTS (SELECT 1 FROM pg_replication_slots WHERE active_pid = %s)
    THEN pg_terminate_backend(%s)
    ELSE FALSE
END;
"""


def kill_pid(instance, pid, *, allow_replication=False):
    """Terminate a backend; protected replication-slot PIDs return False."""
    conn = get_connection(instance)
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(GUARDED_TERMINATE_QUERY, (allow_replication, pid, pid))
            (terminated,) = cur.fetchone()
        return bool(terminated)
    finally:
        conn.close()


def kill_pids(instance, pids, *, allow_replication=False):
    """Terminate permitted backends; return False for missing/protected PIDs."""
    conn = get_connection(instance)
    results = {}
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            for pid in pids:
                cur.execute(GUARDED_TERMINATE_QUERY, (allow_replication, pid, pid))
                (terminated,) = cur.fetchone()
                results[pid] = bool(terminated)
        return results
    finally:
        conn.close()


def fetch_replication_slots(instance):
    conn = get_connection(instance)
    try:
        conn.set_session(readonly=True, autocommit=True)
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(REPLICATION_SLOTS_QUERY)
            return list(cur.fetchall())
    finally:
        conn.close()


def terminate_replication_slot_backend(instance, slot_name):
    """Terminate the walsender backend currently consuming a slot (if any),
    e.g. so it can subsequently be dropped. Returns True if a backend was killed."""
    conn = get_connection(instance)
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SELECT active_pid FROM pg_replication_slots WHERE slot_name = %s;", (slot_name,))
            row = cur.fetchone()
            if not row or not row[0]:
                return False
            cur.execute("SELECT pg_terminate_backend(%s);", (row[0],))
            (terminated,) = cur.fetchone()
            return bool(terminated)
    finally:
        conn.close()


def drop_replication_slot(instance, slot_name):
    """Drop a replication slot outright. Fails if the slot is still active —
    terminate its backend first. Returns (ok, error)."""
    conn = get_connection(instance)
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SELECT pg_drop_replication_slot(%s);", (slot_name,))
        return True, None
    except ConnectionError:
        raise
    except Exception as exc:
        return False, str(exc)
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
            sslmode=settings.DB_SSL_MODE if ssl_required else "prefer",
            **({"sslrootcert": settings.DB_SSL_ROOT_CERT} if ssl_required and settings.DB_SSL_ROOT_CERT else {}),
            application_name="healthgrid-test",
            options=(
                f"-c statement_timeout={STATEMENT_TIMEOUT_MILLISECONDS} "
                f"-c lock_timeout={LOCK_TIMEOUT_MILLISECONDS} "
                f"-c idle_in_transaction_session_timeout={IDLE_TRANSACTION_TIMEOUT_MILLISECONDS}"
            ),
        )
        conn.close()
        return True, None
    except Exception as exc:
        return False, str(exc)
