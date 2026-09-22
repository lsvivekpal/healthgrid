import logging

import psycopg2
import psycopg2.extras
import pymysql
import pymysql.cursors
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
    WHERE NOT blocked_locks.granted
      AND blocked_activity.datname = current_database()
      AND blocking_activity.datname = current_database();
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

SERVER_DATABASES_QUERY = """
    SELECT datname AS database_name
    FROM pg_database
    WHERE datallowconn AND NOT datistemplate
    ORDER BY datname;
"""

SERVER_ACTIVITY_COUNTS_QUERY = """
    SELECT datname AS database_name,
           COUNT(*) AS total_sessions,
           COUNT(*) FILTER (WHERE state = 'active') AS active_sessions,
           COUNT(*) FILTER (WHERE state IN ('idle', 'idle in transaction', 'idle in transaction (aborted)')) AS idle_sessions
    FROM pg_stat_activity
    WHERE datname IS NOT NULL AND pid <> pg_backend_pid()
    GROUP BY datname;
"""

SERVER_LOCK_COUNTS_QUERY = """
    SELECT blocked_activity.datname AS database_name, COUNT(*) AS lock_count
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
    WHERE NOT blocked_locks.granted
      AND blocked_activity.datname IS NOT NULL
      AND blocked_activity.datname = blocking_activity.datname
    GROUP BY blocked_activity.datname;
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


def _is_mysql(instance):
    return getattr(instance, "engine", "postgresql") in {"mysql", "mariadb"}


MYSQL_ACTIVITY_QUERY = """
    SELECT ID AS pid, USER AS usename, HOST AS client_addr,
           COMMAND, STATE AS wait_event, INFO AS query,
           CASE WHEN COMMAND = 'Sleep' THEN 'idle' ELSE 'active' END AS state,
           TIME AS duration_seconds,
           NULL AS application_name
    FROM information_schema.PROCESSLIST
    WHERE DB = %s AND ID <> CONNECTION_ID() AND COMMAND <> 'Sleep'
    ORDER BY TIME DESC
"""

# MariaDB and older MySQL expose InnoDB waits through these catalog views.
# MySQL 8 installations without them use the performance-schema fallback.
MYSQL_LOCK_QUERY = """
    SELECT waiting_trx.trx_mysql_thread_id AS blocked_pid,
           waiting_trx.trx_mysql_thread_id AS blocked_user_pid,
           blocking_trx.trx_mysql_thread_id AS blocking_pid,
           blocked_process.USER AS blocked_user,
           blocking_process.USER AS blocking_user,
           blocked_process.INFO AS blocked_query,
           blocking_process.INFO AS blocking_query,
           waiting_trx.trx_started AS blocked_query_start,
           TIMESTAMPDIFF(SECOND, waiting_trx.trx_started, NOW()) AS waiting_seconds
    FROM information_schema.INNODB_LOCK_WAITS waits
    JOIN information_schema.INNODB_TRX waiting_trx
      ON waiting_trx.trx_id = waits.requesting_trx_id
    JOIN information_schema.INNODB_TRX blocking_trx
      ON blocking_trx.trx_id = waits.blocking_trx_id
    LEFT JOIN information_schema.PROCESSLIST blocked_process
      ON blocked_process.ID = waiting_trx.trx_mysql_thread_id
    LEFT JOIN information_schema.PROCESSLIST blocking_process
      ON blocking_process.ID = blocking_trx.trx_mysql_thread_id
"""

MYSQL_PERFORMANCE_LOCK_QUERY = """
    SELECT requesting_thread.PROCESSLIST_ID AS blocked_pid,
           blocking_thread.PROCESSLIST_ID AS blocking_pid,
           requesting_thread.PROCESSLIST_USER AS blocked_user,
           blocking_thread.PROCESSLIST_USER AS blocking_user,
           requesting_thread.PROCESSLIST_INFO AS blocked_query,
           blocking_thread.PROCESSLIST_INFO AS blocking_query,
           requesting_thread.PROCESSLIST_TIME AS waiting_seconds
    FROM performance_schema.data_lock_waits waits
    JOIN performance_schema.data_locks requesting_lock
      ON requesting_lock.ENGINE_LOCK_ID = waits.REQUESTING_ENGINE_LOCK_ID
    JOIN performance_schema.data_locks blocking_lock
      ON blocking_lock.ENGINE_LOCK_ID = waits.BLOCKING_ENGINE_LOCK_ID
    JOIN performance_schema.threads requesting_thread
      ON requesting_thread.THREAD_ID = requesting_lock.THREAD_ID
    JOIN performance_schema.threads blocking_thread
      ON blocking_thread.THREAD_ID = blocking_lock.THREAD_ID
"""


def _mysql_activity(instance, conn):
    with conn.cursor() as cur:
        cur.execute(MYSQL_ACTIVITY_QUERY, (instance.db_name,))
        return list(cur.fetchall())


def _mysql_blocking(conn):
    with conn.cursor() as cur:
        try:
            cur.execute(MYSQL_LOCK_QUERY)
            rows = list(cur.fetchall())
        except Exception:
            try:
                cur.execute(MYSQL_PERFORMANCE_LOCK_QUERY)
                rows = list(cur.fetchall())
            except Exception:
                # Keep monitoring available when the optional lock catalog is
                # disabled or the monitoring role lacks its privileges.
                logger.warning("MySQL lock catalog is unavailable", exc_info=True)
                rows = []
    for row in rows:
        row["blocked_pid"] = row.get("blocked_pid")
        row["blocking_pid"] = row.get("blocking_pid")
        row["blocked_user"] = row.get("blocked_user") or ""
        row["blocking_user"] = row.get("blocking_user") or ""
        row["blocked_query"] = row.get("blocked_query") or ""
        row["blocking_query"] = row.get("blocking_query") or ""
    return rows


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
        if _is_mysql(instance):
            ssl = {}
            if instance.ssl_required and settings.DB_SSL_ROOT_CERT:
                ssl["ca"] = settings.DB_SSL_ROOT_CERT
            return pymysql.connect(
                host=instance.host,
                port=int(instance.port),
                database=instance.db_name,
                user=username,
                password=password,
                connect_timeout=CONNECT_TIMEOUT_SECONDS,
                read_timeout=CONNECT_TIMEOUT_SECONDS,
                write_timeout=CONNECT_TIMEOUT_SECONDS,
                autocommit=True,
                cursorclass=pymysql.cursors.DictCursor,
                ssl=ssl if instance.ssl_required else None,
                program_name="healthgrid-control",
            )
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
        if _is_mysql(instance):
            return _mysql_activity(instance, conn), _mysql_blocking(conn)
        conn.set_session(readonly=True, autocommit=True)
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(ACTIVITY_QUERY)
            activity = list(cur.fetchall())
            cur.execute(BLOCKING_QUERY)
            blocking = list(cur.fetchall())
        return activity, blocking
    finally:
        conn.close()


def fetch_server_overview(instance):
    """Return read-only database/session/lock totals for a PostgreSQL endpoint."""
    if _is_mysql(instance):
        raise ConnectionError("Server-wide database discovery is currently available for PostgreSQL endpoints only.")
    conn = get_connection(instance)
    try:
        conn.set_session(readonly=True, autocommit=True)
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(SERVER_DATABASES_QUERY)
            database_names = [row["database_name"] for row in cur.fetchall()]
            cur.execute(SERVER_ACTIVITY_COUNTS_QUERY)
            activity_counts = {row["database_name"]: row for row in cur.fetchall()}
            cur.execute(SERVER_LOCK_COUNTS_QUERY)
            lock_counts = {row["database_name"]: row["lock_count"] for row in cur.fetchall()}
        databases = []
        for name in database_names:
            counts = activity_counts.get(name, {})
            databases.append({
                "name": name,
                "total": int(counts.get("total_sessions") or 0),
                "active": int(counts.get("active_sessions") or 0),
                "idle": int(counts.get("idle_sessions") or 0),
                "locks": int(lock_counts.get(name) or 0),
            })
        return {
            "databases": databases,
            "total": sum(item["total"] for item in databases),
            "active": sum(item["active"] for item in databases),
            "idle": sum(item["idle"] for item in databases),
            "locks": sum(item["locks"] for item in databases),
        }
    finally:
        conn.close()


def fetch_activity_with_vitals(instance):
    """Return (activity_rows, blocking_rows, vitals_dict, top_tables) over one
    connection. Used by the live dashboard so everything refreshes together."""
    conn = get_connection(instance)
    try:
        if _is_mysql(instance):
            activity = _mysql_activity(instance, conn)
            blocking = _mysql_blocking(conn)
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT COUNT(*) AS total_conns,
                           SUM(COMMAND <> 'Sleep') AS active_conns,
                           SUM(COMMAND = 'Sleep') AS idle_conns,
                           0 AS idle_in_txn,
                           0 AS waiting_on_locks,
                           @@max_connections AS max_conns,
                           ROUND(SUM(data_length + index_length), 0) AS db_size,
                           COALESCE(MAX(TIME), 0) AS longest_active_seconds
                    FROM information_schema.PROCESSLIST
                    WHERE DB = %s
                """, (instance.db_name,))
                vitals = cur.fetchone()
                cur.execute("""
                    SELECT CONCAT(table_schema, '.', table_name) AS table_name,
                           data_length + index_length AS total_bytes,
                           ROUND((data_length + index_length) / 1073741824, 2) AS size_gb
                    FROM information_schema.tables
                    WHERE table_schema = %s
                    ORDER BY total_bytes DESC LIMIT 10
                """, (instance.db_name,))
                top_tables = list(cur.fetchall())
            return activity, blocking, vitals, top_tables
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
        if _is_mysql(instance):
            with conn.cursor() as cur:
                cur.execute(f"KILL CONNECTION {int(pid)}")
            return True
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
        if _is_mysql(instance):
            with conn.cursor() as cur:
                for pid in pids:
                    try:
                        cur.execute(f"KILL CONNECTION {int(pid)}")
                        results[pid] = True
                    except Exception:
                        results[pid] = False
            return results
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
    if _is_mysql(instance):
        return []
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
    if _is_mysql(instance):
        return False
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
    if _is_mysql(instance):
        return False, "Replication slots are only supported for PostgreSQL."
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


def test_connection(engine, host, port, db_name, username, password, ssl_required):
    """Try connecting with raw creds (not a saved instance) — for the Add form's
    'Test connection' button, before anything is persisted."""
    try:
        if engine in {"mysql", "mariadb"}:
            ssl = {}
            if ssl_required and settings.DB_SSL_ROOT_CERT:
                ssl["ca"] = settings.DB_SSL_ROOT_CERT
            conn = pymysql.connect(
                host=host,
                port=int(port),
                database=db_name,
                user=username,
                password=password,
                connect_timeout=CONNECT_TIMEOUT_SECONDS,
                cursorclass=pymysql.cursors.DictCursor,
                ssl=ssl if ssl_required else None,
                program_name="healthgrid-test",
            )
            conn.close()
            return True, None
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
