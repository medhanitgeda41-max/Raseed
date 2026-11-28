"""Unified database connection factory supporting local SQLite and cloud Postgres (Neon).

Switches automatically via DATABASE_URL environment variable:
- If DATABASE_URL is set: connects to Neon Postgres via psycopg3 (pooled connections).
- Otherwise: connects to local SQLite with WAL mode enabled.

The rest of the code base writes SQLite-style SQL (``?`` placeholders,
``INSERT OR IGNORE``, ``datetime('now')``). The Postgres wrapper below
translates those statements and normalises row values (timestamps -> text,
NUMERIC -> float) so both engines behave the same for callers.
"""
from datetime import datetime, date
from decimal import Decimal
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import threading
from typing import Optional, Tuple, Any, List, Dict, Union

from config.settings import settings
from database.request_cache import note_statement

SCHEMA_PATH = Path(__file__).parent / "schema.sql"
_log = logging.getLogger(__name__)

# Test hook: when set, every connection (even with an explicit db_path) goes to
# this Postgres URL so the whole test-suite can be run against a real Postgres.
TEST_PG_ENV = "RASEED_TEST_DATABASE_URL"


def _forced_test_pg_url() -> Optional[str]:
    url = os.environ.get(TEST_PG_ENV)
    return _normalise_url(url) if url else None


def _normalise_url(url: str) -> str:
    # Neon/Render sometimes pass postgres:// which psycopg3 prefers as postgresql://
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    return url


def is_postgres_configured() -> bool:
    """Return True if a PostgreSQL DATABASE_URL is configured."""
    if _forced_test_pg_url():
        return True
    url = os.environ.get("DATABASE_URL") or settings.database_url
    return bool(url and ("postgres://" in url or "postgresql://" in url))


def get_database_url() -> Optional[str]:
    """Get clean Postgres connection string (handling postgres:// to postgresql:// if needed)."""
    forced = _forced_test_pg_url()
    if forced:
        return forced
    url = os.environ.get("DATABASE_URL") or settings.database_url
    if not url:
        return None
    return _normalise_url(url)


def _pg_url_for(db_path: Optional[Path]) -> Optional[str]:
    """Return the Postgres URL to use for this call, or None for SQLite."""
    forced = _forced_test_pg_url()
    if forced:
        return forced
    if db_path is None and "PYTEST_CURRENT_TEST" not in os.environ and is_postgres_configured():
        return get_database_url()
    return None


# ------------------------------------------------------------------------------
# Value normalisation (make Postgres rows look like SQLite rows)
# ------------------------------------------------------------------------------

def _norm_value(v: Any) -> Any:
    if isinstance(v, datetime):
        if v.tzinfo is not None:
            from datetime import timezone
            v = v.astimezone(timezone.utc).replace(tzinfo=None)
        return v.isoformat(sep=" ")
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, memoryview):
        return v.tobytes()
    return v


class RowAdapter(dict):
    """Row adapter that allows dictionary key access (row['id']) and index access (row[0])."""

    def __init__(self, data: Dict[str, Any]):
        super().__init__(data)
        self._values = list(data.values())

    def __getitem__(self, key: Union[str, int]) -> Any:
        if isinstance(key, int):
            return self._values[key]
        return super().__getitem__(key)

    def get(self, key: str, default: Any = None) -> Any:
        return super().get(key, default)


_INSERT_OR_IGNORE = re.compile(r"\bINSERT\s+OR\s+IGNORE\s+INTO\b", re.IGNORECASE)
_INSERT_OR_REPLACE = re.compile(r"\bINSERT\s+OR\s+REPLACE\s+INTO\b", re.IGNORECASE)
_DATETIME_NOW = re.compile(r"\bdatetime\(\s*'now'\s*\)", re.IGNORECASE)
_DATE_NOW = re.compile(r"\bdate\(\s*'now'\s*\)", re.IGNORECASE)
_IFNULL = re.compile(r"\bIFNULL\s*\(", re.IGNORECASE)
_GROUP_CONCAT = re.compile(r"\bGROUP_CONCAT\s*\(\s*([^,()]+?)\s*\)", re.IGNORECASE)
_TRAILING_SEMI = re.compile(r";\s*$")
_PARAM_IS_NULL = re.compile(r"%s(\s+IS\s+(?:NOT\s+)?NULL)", re.IGNORECASE)
_LIKE = re.compile(r"\bLIKE\b", re.IGNORECASE)


def convert_sql_for_postgres(sql: str, has_params: bool) -> str:
    """Translate one SQLite-flavoured statement into Postgres SQL."""
    clean = sql
    if has_params:
        # psycopg treats % as a placeholder marker when params are passed,
        # so literal % (e.g. LIKE '%credit%') must be escaped first.
        clean = clean.replace("%", "%%")
        clean = clean.replace("?", "%s")
        # "? IS NULL" gives Postgres no type for the parameter: make it explicit.
        clean = _PARAM_IS_NULL.sub(r"%s::text\1", clean)
    # SQLite LIKE is case-insensitive for ASCII; Postgres LIKE is not.
    clean = _LIKE.sub("ILIKE", clean)
    if _INSERT_OR_IGNORE.search(clean):
        clean = _INSERT_OR_IGNORE.sub("INSERT INTO", clean)
        if "ON CONFLICT" not in clean.upper():
            clean = _TRAILING_SEMI.sub("", clean).strip() + " ON CONFLICT DO NOTHING;"
    clean = _DATETIME_NOW.sub("CURRENT_TIMESTAMP", clean)
    clean = _DATE_NOW.sub("CURRENT_DATE", clean)
    clean = _IFNULL.sub("COALESCE(", clean)
    clean = _GROUP_CONCAT.sub(r"STRING_AGG(CAST(\1 AS TEXT), ',')", clean)
    return clean


class PgCursorWrapper:
    """Cursor wrapper for psycopg3: ? -> %s, RETURNING id for inserts, rollback on error."""

    def __init__(self, raw_cursor, conn_wrapper=None):
        self._cur = raw_cursor
        self._conn_wrapper = conn_wrapper
        self.lastrowid = None
        self._prefetched: Optional[List[Any]] = None

    def _convert_sql(self, sql: str, has_params: bool = True) -> str:
        return convert_sql_for_postgres(sql, has_params)

    def execute(self, sql: str, params: Optional[Union[tuple, list, dict]] = None):
        has_params = params is not None and len(params) > 0
        clean_converted = self._convert_sql(sql, has_params).strip()
        if clean_converted.upper().startswith("PRAGMA"):
            return self

        upper = clean_converted.upper()
        is_insert = upper.startswith("INSERT INTO")
        has_returning = "RETURNING" in upper

        sql_to_run = clean_converted
        added_returning = False
        if is_insert and not has_returning and "ON CONFLICT DO NOTHING" not in upper:
            sql_to_run = _TRAILING_SEMI.sub("", clean_converted).strip() + " RETURNING id;"
            added_returning = True

        self.lastrowid = None
        self._prefetched = None
        note_statement(sql_to_run)
        try:
            if has_params:
                self._cur.execute(sql_to_run, params)
            else:
                self._cur.execute(sql_to_run)

            if added_returning and self._cur.description:
                row = self._cur.fetchone()
                if row is not None:
                    self.lastrowid = row.get("id") if isinstance(row, dict) else row[0]
            return self
        except Exception as exc:
            # A failed statement poisons the Postgres transaction; roll back so the
            # connection stays usable, and log the real cause (with PII masked).
            if self._conn_wrapper:
                try:
                    self._conn_wrapper.rollback()
                except Exception:
                    pass
            try:
                from guards.privacy import mask_log_message
                safe_sql = mask_log_message(str(sql))
            except Exception:
                safe_sql = "<sql>"
            _log.error("Database error (%s: %s) executing SQL: %s", type(exc).__name__, exc, safe_sql)
            raise

    @property
    def rowcount(self) -> int:
        return self._cur.rowcount if self._cur is not None else 0

    @property
    def description(self):
        return self._cur.description if self._cur is not None else None

    def executemany(self, sql: str, seq_of_params):
        clean = self._convert_sql(sql, True)
        note_statement(clean)
        return self._cur.executemany(clean, seq_of_params)

    def __iter__(self):
        while True:
            row = self.fetchone()
            if row is None:
                break
            yield row

    def _adapt(self, row: Any) -> RowAdapter:
        if isinstance(row, dict):
            data = row
        else:
            cols = [desc[0] for desc in self._cur.description] if self._cur.description else []
            data = dict(zip(cols, row))
        return RowAdapter({k: _norm_value(v) for k, v in data.items()})

    def fetchone(self) -> Optional[RowAdapter]:
        if self._cur.description is None:
            return None
        row = self._cur.fetchone()
        if row is None:
            return None
        adapter = self._adapt(row)
        if "id" in adapter and self.lastrowid is None:
            self.lastrowid = adapter["id"]
        return adapter

    def fetchall(self) -> List[RowAdapter]:
        if self._cur.description is None:
            return []
        rows = self._cur.fetchall()
        return [self._adapt(r) for r in rows] if rows else []

    def close(self):
        try:
            self._cur.close()
        except Exception:
            pass


class PgConnectionWrapper:
    """Connection wrapper for PostgreSQL (psycopg3). close() returns pooled connections."""

    def __init__(self, raw_conn, pool=None):
        self._conn = raw_conn
        self._pool = pool
        self._closed = False

    def cursor(self) -> PgCursorWrapper:
        return PgCursorWrapper(self._conn.cursor(), conn_wrapper=self)

    def execute(self, sql: str, params: Optional[Union[tuple, list, dict]] = None) -> PgCursorWrapper:
        cur = self.cursor()
        cur.execute(sql, params)
        return cur

    def executescript(self, script: str) -> None:
        for stmt in script.split(";"):
            if stmt.strip():
                self.execute(stmt)

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        if self._closed:
            return
        self._closed = True
        if self._pool is not None:
            try:
                # Never hand a connection with an open transaction back to the pool.
                self._conn.rollback()
            except Exception:
                pass
            try:
                self._pool.putconn(self._conn)
                return
            except Exception:
                pass
        try:
            self._conn.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is not None:
            self.rollback()
        else:
            self.commit()


# ------------------------------------------------------------------------------
# Connection pool (re-using connections is the biggest speed win on Neon:
# every fresh TLS connection costs 100-400 ms).
# ------------------------------------------------------------------------------
_POOLS: Dict[str, Any] = {}
_POOL_LOCK = threading.Lock()
_PG_DOWN_UNTIL: Dict[str, float] = {}


def _get_pool(url: str):
    pool = _POOLS.get(url)
    if pool is not None:
        return pool
    with _POOL_LOCK:
        pool = _POOLS.get(url)
        if pool is not None:
            return pool
        try:
            from psycopg_pool import ConnectionPool
            from psycopg.rows import dict_row
        except Exception:
            return None
        pool = ConnectionPool(
            url,
            min_size=1,
            max_size=int(os.environ.get("DB_POOL_MAX", "6")),
            max_idle=240,           # Neon closes idle connections; recycle before that
            kwargs={"row_factory": dict_row, "connect_timeout": 5, "autocommit": False},
            check=ConnectionPool.check_connection,
            open=True,
            name="raseed",
        )
        _POOLS[url] = pool
        return pool


def close_all_pools() -> None:
    with _POOL_LOCK:
        for pool in _POOLS.values():
            try:
                pool.close()
            except Exception:
                pass
        _POOLS.clear()


def _connect_postgres(url: str) -> PgConnectionWrapper:
    import psycopg
    from psycopg.rows import dict_row

    pool = _get_pool(url)
    if pool is not None:
        raw = pool.getconn(timeout=10)
        return PgConnectionWrapper(raw, pool=pool)
    raw = psycopg.connect(url, row_factory=dict_row, connect_timeout=5)
    return PgConnectionWrapper(raw)


def get_db_connection(
    db_path: Optional[Path] = None, readonly: bool = False
) -> Any:
    """Create a configured database connection.

    If DATABASE_URL is set and no explicit local db_path is passed, uses Postgres (Neon).
    If Postgres is unreachable, gracefully falls back to local SQLite (and does not
    retry Postgres for 30 s, so the UI does not hang on every query).
    """
    import time

    url = _pg_url_for(db_path)
    if url and _PG_DOWN_UNTIL.get(url, 0) < time.monotonic():
        try:
            return _connect_postgres(url)
        except Exception as e:
            if _forced_test_pg_url():
                raise
            _PG_DOWN_UNTIL[url] = time.monotonic() + 30
            _log.warning("Postgres connection failed (%s); falling back to local SQLite.", e)

    # SQLite Local Connection
    path = db_path or settings.database_path
    path = Path(path).resolve()

    if readonly:
        if not path.exists():
            raise FileNotFoundError(f"Database file does not exist for read-only access: {path}")
        uri = f"file:{path.as_posix()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=10.0)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), timeout=10.0)
        conn.execute("PRAGMA journal_mode = WAL;")

    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA busy_timeout = 5000;")
    conn.row_factory = sqlite3.Row
    if not readonly:
        # Invalidate the per-page read cache whenever this connection writes.
        conn.set_trace_callback(note_statement)
    return conn


def convert_sqlite_schema_to_postgres(sqlite_sql: Optional[str] = None) -> str:
    """Convert SQLite schema DDL statements to PostgreSQL compatible DDL."""
    if sqlite_sql is None:
        with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
            sqlite_sql = f.read()

    lines = []
    for line in sqlite_sql.splitlines():
        clean = line.strip()
        if clean.upper().startswith("PRAGMA"):
            continue
        lines.append(line)
    text = "\n".join(lines)
    text = re.sub(r"\bINTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT\b", "SERIAL PRIMARY KEY", text, flags=re.IGNORECASE)
    text = re.sub(r"\bJSON\b", "JSONB", text, flags=re.IGNORECASE)
    text = re.sub(r"\bTIMESTAMP\b", "TIMESTAMPTZ", text, flags=re.IGNORECASE)
    # SQLite REAL is 8-byte; Postgres REAL is only 4-byte (loses cents on large AED amounts).
    text = re.sub(r"\bREAL\b", "DOUBLE PRECISION", text, flags=re.IGNORECASE)
    return text


TENANT_TABLES = [
    "users",
    "business_profile",
    "uploaded_documents",
    "invoices",
    "transactions",
    "credit_ledger_entries",
    "ledger_entries",
    "vat_returns",
    "credit_ledger",
    "refund_records",
    "credit_allocations",
    "document_requests",
    "agent_tasks",
    "staged_actions",
    "audit_events",
    "scheduler_runs",
    "scheduler_messages",
    "chat_messages",
    "memory_notes",
    "cached_column_mappings",
    "parked_rows",
]

_MIGRATION_COLUMNS = [
    ("chat_messages", "timestamp TEXT"),
    ("uploaded_documents", "extracted_text TEXT"),
    ("users", "email TEXT"),
    ("users", "full_name TEXT"),
    ("users", "password_hash TEXT"),
    ("users", "failed_login_attempts INTEGER DEFAULT 0"),
    ("users", "locked_until TIMESTAMP"),
    ("users", "is_verified INTEGER DEFAULT 1"),
    ("users", "totp_secret TEXT"),
    ("users", "is_totp_enabled INTEGER DEFAULT 0"),
    ("users", "backup_codes_json TEXT"),
    ("business_profile", "local_ai_only INTEGER NOT NULL DEFAULT 0"),
    ("uploaded_documents", "included_in_analysis INTEGER NOT NULL DEFAULT 1"),
    ("uploaded_documents", "reader_version INTEGER NOT NULL DEFAULT 0"),
]

_INIT_DONE: set = set()
_TEST_RESET_DONE: set = set()
_INIT_LOCK = threading.Lock()


def _init_postgres(url: str, schema_sql: str, reset: bool = False) -> None:
    """Create tables, migrations, RLS policies. Runs in autocommit so one failing
    statement can never undo the others."""
    import psycopg

    pg_sql = convert_sqlite_schema_to_postgres(schema_sql)
    with psycopg.connect(url, autocommit=True, connect_timeout=10) as raw:
        def run(stmt: str) -> bool:
            try:
                raw.execute(stmt)
                return True
            except Exception as exc:
                _log.debug("init_db statement skipped (%s): %s", exc, stmt[:80])
                return False

        if reset:
            close_all_pools()
            run(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = current_database() AND pid <> pg_backend_pid();"
            )
            run("DROP SCHEMA IF EXISTS public CASCADE;")
            run("CREATE SCHEMA public;")

        # 1. Tables and indexes
        for stmt in pg_sql.split(";"):
            if stmt.strip():
                run(stmt)

        # 2. Missing columns
        for tbl, col in _MIGRATION_COLUMNS:
            col_pg = re.sub(r"\bTIMESTAMP\b", "TIMESTAMPTZ", col)
            run(f"ALTER TABLE {tbl} ADD COLUMN IF NOT EXISTS {col_pg};")

        # 2b. Allow the 'evidence' document type on databases created by older versions
        try:
            run("ALTER TABLE uploaded_documents DROP CONSTRAINT IF EXISTS uploaded_documents_document_type_check;")
            run("ALTER TABLE uploaded_documents ADD CONSTRAINT uploaded_documents_document_type_check CHECK "
                "(document_type IN ('invoice', 'register', 'vat_return', 'ledger', 'fta_guide', 'evidence', 'other'));")
        except Exception:
            pass

        # 3. Upgrade any 4-byte REAL money columns created by older versions
        try:
            cols = raw.execute(
                "SELECT table_name, column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND data_type = 'real';"
            ).fetchall()
            for tbl, col in cols:
                run(f'ALTER TABLE "{tbl}" ALTER COLUMN "{col}" TYPE DOUBLE PRECISION;')
        except Exception:
            pass

        # 4. Row-Level Security policies (tenant isolation)
        tenant_check = (
            "current_setting('app.business_id', true) IS NULL "
            "OR current_setting('app.business_id', true) = '' "
            "OR {col}::text = current_setting('app.business_id', true)"
        )
        run("ALTER TABLE businesses ENABLE ROW LEVEL SECURITY;")
        run("DROP POLICY IF EXISTS businesses_tenant_policy ON businesses;")
        chk = tenant_check.format(col="id")
        run(f"CREATE POLICY businesses_tenant_policy ON businesses FOR ALL USING ({chk}) WITH CHECK ({chk});")
        for tbl in TENANT_TABLES:
            chk = tenant_check.format(col="business_id")
            run(f"ALTER TABLE {tbl} ENABLE ROW LEVEL SECURITY;")
            run(f"DROP POLICY IF EXISTS tenant_isolation_policy ON {tbl};")
            run(f"CREATE POLICY tenant_isolation_policy ON {tbl} FOR ALL USING ({chk}) WITH CHECK ({chk});")

        # 5. Default business root + one-time verification migration
        run("INSERT INTO businesses (id, name, trn) VALUES (1, 'Default Business', '100000000000003') ON CONFLICT DO NOTHING;")
        run("UPDATE users SET is_verified = 1 WHERE is_verified = 0;")

        # 6. Keep SERIAL sequences ahead of manually inserted ids
        for tbl in ["businesses"] + TENANT_TABLES + ["verification_codes", "rate_limits"]:
            run(
                f"SELECT setval(pg_get_serial_sequence('{tbl}', 'id'), "
                f"GREATEST(coalesce((SELECT max(id) FROM {tbl}), 1), 1));"
            )


def _sqlite_allow_evidence_type(conn) -> None:
    """Older SQLite databases have a CHECK list without 'evidence': rebuild that one table in place."""
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='uploaded_documents';").fetchone()
    sql = (row[0] if row else "") or ""
    if not sql or "'evidence'" in sql:
        return
    new_sql = sql.replace("'fta_guide', 'other'", "'fta_guide', 'evidence', 'other'")
    if new_sql == sql:
        return
    conn.execute("PRAGMA foreign_keys = OFF;")
    conn.execute("ALTER TABLE uploaded_documents RENAME TO uploaded_documents_old;")
    conn.execute(new_sql)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(uploaded_documents_old);").fetchall()]
    new_cols = {r[1] for r in conn.execute("PRAGMA table_info(uploaded_documents);").fetchall()}
    keep = ", ".join(c for c in cols if c in new_cols)
    conn.execute(f"INSERT INTO uploaded_documents ({keep}) SELECT {keep} FROM uploaded_documents_old;")
    conn.execute("DROP TABLE uploaded_documents_old;")
    conn.execute("PRAGMA foreign_keys = ON;")


def init_db(db_path: Optional[Path] = None, force: bool = False) -> None:
    """Initialize database tables, indexes, and RLS policies from schema.sql. Idempotent.

    On Postgres this runs once per process (subsequent calls are free), because the
    Streamlit script re-executes on every click.
    """
    with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
        schema_sql = f.read()

    url = _pg_url_for(db_path)
    if url:
        forced_test = _forced_test_pg_url() is not None
        key = url
        if not forced_test and not force and key in _INIT_DONE:
            return
        with _INIT_LOCK:
            if not forced_test and not force and key in _INIT_DONE:
                return
            try:
                # In forced test mode each fresh test database gets a clean schema.
                reset = False
                if forced_test and db_path is not None:
                    marker = str(Path(db_path).resolve())
                    reset = marker not in _TEST_RESET_DONE
                    _TEST_RESET_DONE.add(marker)
                _init_postgres(url, schema_sql, reset=reset)
                _INIT_DONE.add(key)
                return
            except Exception as exc:
                if forced_test:
                    raise
                _log.warning("Postgres init failed (%s); using local SQLite.", exc)

    # SQLite Initialization
    path = db_path or settings.database_path
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)

    conn = get_db_connection(db_path=path, readonly=False)
    if not isinstance(conn, sqlite3.Connection):
        conn.close()
        return
    try:
        with conn:
            for tbl, col in _MIGRATION_COLUMNS:
                try:
                    conn.execute(f"ALTER TABLE {tbl} ADD COLUMN {col};")
                except Exception:
                    pass

            conn.executescript(schema_sql)
            _sqlite_allow_evidence_type(conn)
            conn.execute(
                "INSERT INTO businesses (id, name, trn) VALUES (1, 'Default Business', '100000000000003') ON CONFLICT(id) DO NOTHING;"
            )
            try:
                conn.execute("UPDATE users SET is_verified = 1 WHERE is_verified = 0;")
            except Exception:
                pass
    finally:
        conn.close()


def set_tenant_context(conn: Any, business_id: Optional[int]) -> None:
    """Set PostgreSQL Row-Level Security tenant context session variable if on Postgres."""
    if business_id is None:
        return
    try:
        if isinstance(conn, PgConnectionWrapper) or hasattr(conn, "_conn"):
            conn.execute(f"SET LOCAL app.business_id = '{int(business_id)}';")
    except Exception:
        pass


def create_business(
    name: str,
    trn: str,
    db_path: Optional[Path] = None,
) -> int:
    """Create a new isolated business tenant."""
    conn = get_db_connection(db_path=db_path, readonly=False)
    try:
        with conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO businesses (name, trn) VALUES (?, ?) ON CONFLICT(trn) DO UPDATE SET name = excluded.name RETURNING id;",
                (name, trn),
            )
            row = cursor.fetchone()
            biz_id = row["id"] if row else cursor.lastrowid
            if not biz_id:
                cursor.execute("SELECT id FROM businesses WHERE trn = ?;", (trn,))
                row2 = cursor.fetchone()
                biz_id = row2["id"] if row2 else 1
            set_tenant_context(conn, biz_id)
            return biz_id
    finally:
        conn.close()


def create_user(
    business_id: int,
    username: str,
    role: str,
    db_path: Optional[Path] = None,
) -> int:
    """Create a user with role ('OWNER', 'ACCOUNTANT', 'APPROVER') for a specific business."""
    if role not in ("OWNER", "ACCOUNTANT", "APPROVER"):
        raise ValueError(f"Invalid user role: {role}. Must be OWNER, ACCOUNTANT, or APPROVER.")

    conn = get_db_connection(db_path=db_path, readonly=False)
    try:
        with conn:
            set_tenant_context(conn, business_id)
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO users (business_id, username, role, is_verified) VALUES (?, ?, ?, 1)
                ON CONFLICT(business_id, username) DO UPDATE SET role = excluded.role RETURNING id;
                """,
                (business_id, username, role),
            )
            row = cursor.fetchone()
            if row:
                return row["id"]
            if cursor.lastrowid:
                return cursor.lastrowid
            cursor.execute("SELECT id FROM users WHERE business_id = ? AND username = ?;", (business_id, username))
            return cursor.fetchone()["id"]
    finally:
        conn.close()


def list_businesses(
    username: Optional[str] = None,
    db_path: Optional[Path] = None,
) -> list[dict]:
    """Return the businesses accessible to the user (or all businesses if username is None)."""
    from guards.privacy import mask_trn

    conn = get_db_connection(db_path=db_path, readonly=False)
    try:
        cursor = conn.cursor()
        if username:
            cursor.execute(
                """
                SELECT 
                    b.id, 
                    b.name, 
                    b.trn, 
                    b.created_at,
                    bp.legal_name,
                    u.role
                FROM businesses b
                JOIN users u ON b.id = u.business_id
                LEFT JOIN business_profile bp ON b.id = bp.business_id
                WHERE u.username = ?
                ORDER BY b.id ASC;
                """,
                (username,),
            )
        else:
            cursor.execute(
                """
                SELECT 
                    b.id, 
                    b.name, 
                    b.trn, 
                    b.created_at,
                    bp.legal_name,
                    NULL as role
                FROM businesses b
                LEFT JOIN business_profile bp ON b.id = bp.business_id
                ORDER BY b.id ASC;
                """
            )
        rows = cursor.fetchall()
        results = []
        for r in rows:
            raw_trn = r["trn"] or ""
            masked = mask_trn(raw_trn) if raw_trn else "[MASKED_TRN]"
            created_at_val = str(r["created_at"]) if r["created_at"] else ""
            created_date_val = created_at_val.split()[0] if created_at_val else ""
            legal_name_val = r["legal_name"] or r["name"]
            results.append({
                "id": r["id"],
                "name": r["name"],
                "legal_name": legal_name_val,
                "trn": masked,
                "masked_trn": masked,
                "raw_trn": raw_trn,
                "created_at": created_at_val,
                "created_date": created_date_val,
                "role": r["role"],
            })
        return results
    finally:
        conn.close()


def get_or_create_default_business(db_path: Optional[Path] = None) -> int:
    """Ensure at least one business exists for single-tenant backward compatibility."""
    conn = get_db_connection(db_path=db_path, readonly=False)
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM businesses LIMIT 1;")
        row = cursor.fetchone()
        if row:
            return row["id"]
        with conn:
            cursor.execute(
                "INSERT INTO businesses (name, trn) VALUES ('Default Entity', '100000000000003');"
            )
            return cursor.lastrowid
    finally:
        conn.close()


# ------------------------------------------------------------------------------
# Persistent Agent Memory: Chat History
# ------------------------------------------------------------------------------

# The web app turns this off: chat is a temporary workspace that is not kept after logout.
CHAT_PERSISTENCE = True


def save_chat_message(
    business_id: int,
    role: str,
    content: str,
    how_i_got_this: Optional[Any] = None,
    approval_card: Optional[Any] = None,
    staged_action_id: Optional[int] = None,
    timestamp: Optional[str] = None,
    db_path: Optional[Path] = None,
    how_i_got_this_json: Optional[str] = None,
    approval_card_json: Optional[str] = None,
) -> int:
    """Persist a chat message for a business conversation."""
    if not CHAT_PERSISTENCE and db_path is None:
        return 0
    conn = get_db_connection(db_path=db_path, readonly=False)
    ts = timestamp or datetime.now().strftime("%H:%M")
    h_json = how_i_got_this_json if how_i_got_this_json is not None else (json.dumps(how_i_got_this) if how_i_got_this else None)
    a_json = approval_card_json if approval_card_json is not None else (json.dumps(approval_card) if approval_card else None)
    try:
        with conn:
            set_tenant_context(conn, business_id)
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO chat_messages (
                    business_id, role, content, how_i_got_this_json, approval_card_json,
                    staged_action_id, timestamp
                ) VALUES (?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    business_id,
                    role,
                    content,
                    h_json,
                    a_json,
                    staged_action_id,
                    ts,
                ),
            )
            return cursor.lastrowid or 0
    finally:
        conn.close()


def get_chat_history(
    business_id: int,
    limit: int = 20,
    db_path: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """Retrieve the recent chat history for a business, ordered oldest to newest."""
    conn = get_db_connection(db_path=db_path, readonly=True)
    try:
        set_tenant_context(conn, business_id)
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT * FROM (
                SELECT *
                FROM chat_messages
                WHERE business_id = ?
                ORDER BY id DESC
                LIMIT ?
            ) sub ORDER BY id ASC;
            """,
            (business_id, limit),
        )
        rows = cursor.fetchall()
        messages = []
        for r in rows:
            keys = r.keys() if hasattr(r, "keys") else []
            ts = r["timestamp"] if "timestamp" in keys and r["timestamp"] else (
                str(r["created_at"])[11:16] if "created_at" in keys and r["created_at"] else datetime.now().strftime("%H:%M")
            )
            msg = {
                "id": r["id"],
                "role": r["role"],
                "content": r["content"],
                "timestamp": ts,
            }
            if "how_i_got_this_json" in keys and r["how_i_got_this_json"]:
                try:
                    msg["how_i_got_this"] = json.loads(r["how_i_got_this_json"])
                except Exception:
                    pass
            if "approval_card_json" in keys and r["approval_card_json"]:
                try:
                    msg["approval_card"] = json.loads(r["approval_card_json"])
                except Exception:
                    pass
            if "staged_action_id" in keys and r["staged_action_id"]:
                msg["staged_action_id"] = r["staged_action_id"]
            messages.append(msg)
        return messages
    finally:
        conn.close()


# ------------------------------------------------------------------------------
# Persistent Agent Memory: Confirmed Memory Notes
# ------------------------------------------------------------------------------

def add_memory_note(
    business_id: int,
    fact: Optional[str] = None,
    source: str = "chat",
    confirmed_date: Optional[str] = None,
    db_path: Optional[Path] = None,
    note: Optional[str] = None,
) -> int:
    """Save an important fact confirmed by the owner in the chat."""
    conn = get_db_connection(db_path=db_path, readonly=False)
    the_fact = (fact or note or "").strip()
    cd = confirmed_date or date.today().isoformat()
    try:
        with conn:
            set_tenant_context(conn, business_id)
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO memory_notes (business_id, fact, source, confirmed_date)
                VALUES (?, ?, ?, ?);
                """,
                (business_id, the_fact, source.strip(), cd),
            )
            return cursor.lastrowid or 0
    finally:
        conn.close()


def get_memory_notes(
    business_id: int,
    db_path: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """Retrieve all active confirmed memory notes for a business."""
    conn = get_db_connection(db_path=db_path, readonly=True)
    try:
        set_tenant_context(conn, business_id)
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id, business_id, fact, source, confirmed_date, created_at
            FROM memory_notes
            WHERE business_id = ?
            ORDER BY id ASC;
            """,
            (business_id,),
        )
        rows = cursor.fetchall()
        results = []
        for r in rows:
            d = dict(r)
            d["note"] = d["fact"]
            results.append(d)
        return results
    finally:
        conn.close()


def delete_memory_note(
    note_id: int,
    business_id: int,
    db_path: Optional[Path] = None,
) -> bool:
    """Delete a confirmed memory note belonging to a business."""
    conn = get_db_connection(db_path=db_path, readonly=False)
    try:
        with conn:
            set_tenant_context(conn, business_id)
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM memory_notes WHERE id = ? AND business_id = ?;",
                (note_id, business_id),
            )
            return True
    finally:
        conn.close()


def reset_db(db_path: Optional[Path] = None, confirm: bool = False) -> None:
    """Wipe all business data, records, and uploaded document references from database.
    
    Requires explicit confirm=True.
    """
    if not confirm:
        raise ValueError("reset_db requires explicit confirm=True parameter.")

    tables = [
        "chat_messages",
        "memory_notes",
        "audit_events",
        "staged_actions",
        "agent_tasks",
        "document_requests",
        "credit_allocations",
        "credit_ledger_entries",
        "transactions",
        "refund_records",
        "credit_ledger",
        "vat_returns",
        "ledger_entries",
        "invoices",
        "parked_rows",
        "uploaded_documents",
        "business_profile",
        "user_roles",
        "users",
        "businesses",
    ]

    conn = get_db_connection(db_path=db_path, readonly=False)
    try:
        if isinstance(conn, PgConnectionWrapper):
            existing = {
                r["table_name"]
                for r in conn.execute(
                    "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public';"
                ).fetchall()
            }
            to_clear = [t for t in tables if t in existing]
            with conn:
                if to_clear:
                    conn.execute("TRUNCATE " + ", ".join(to_clear) + " RESTART IDENTITY CASCADE;")
                conn.execute(
                    "INSERT INTO businesses (id, name, trn) VALUES (1, 'Default Business', '100000000000003') ON CONFLICT DO NOTHING;"
                )
                conn.execute("SELECT setval(pg_get_serial_sequence('businesses', 'id'), 1);")
            return
        with conn:
            for table in tables:
                try:
                    conn.execute(f"DELETE FROM {table};")
                except Exception:
                    pass
            # Reset SQLite sequence if on SQLite
            try:
                conn.execute("DELETE FROM sqlite_sequence WHERE name IN ('" + "','".join(tables) + "');")
            except Exception:
                pass
            # Restore default business root
            try:
                conn.execute(
                    "INSERT INTO businesses (id, name, trn) VALUES (1, 'Default Business', '100000000000003');"
                )
            except Exception:
                pass
    finally:
        conn.close()


def clear_chat_history(business_id: int, db_path: Optional[Path] = None) -> int:
    """Delete the saved chat conversation for one business (memory notes are kept)."""
    conn = get_db_connection(db_path=db_path, readonly=False)
    try:
        with conn:
            set_tenant_context(conn, business_id)
            cur = conn.cursor()
            cur.execute("DELETE FROM chat_messages WHERE business_id = ?;", (business_id,))
            return cur.rowcount or 0
    finally:
        conn.close()
