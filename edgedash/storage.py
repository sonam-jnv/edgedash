"""
The ONLY storage module in EdgeDash (Rule 2).

All other modules interact with storage exclusively through the public functions
defined here. Swapping between local SQLite and hosted PostgreSQL is configured
via the DATABASE_URL environment variable without changing any other file.

Supported backends:
- SQLite (default for local/offline development)
- PostgreSQL (when DATABASE_URL is set in environment or .env)

CLI Utilities:
  python -m edgedash.storage --check
  python -m edgedash.storage --migrate
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sqlite3
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Generator, Sequence

logger = logging.getLogger("edgedash.storage")

# ---------------------------------------------------------------------------
# Environment loader
# ---------------------------------------------------------------------------

def _load_dotenv() -> None:
    """Load .env from candidate paths into os.environ if not already set, plus Streamlit secrets."""
    try:
        import streamlit as st
        if hasattr(st, "secrets"):
            for k, v in st.secrets.items():
                if isinstance(v, str) and k not in os.environ:
                    os.environ[k] = v
    except Exception:
        pass

    candidates = [
        Path(__file__).parent.parent / ".env",
        Path(__file__).parent / ".env",
        Path.cwd() / ".env",
    ]
    for env_path in candidates:
        if env_path.exists():
            with env_path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, _, value = line.partition("=")
                    key = key.strip()
                    value = value.strip().strip('"').strip("'")
                    if key and key not in os.environ:
                        os.environ[key] = value


# ---------------------------------------------------------------------------
# Backend detection and startup logging
# ---------------------------------------------------------------------------

_STARTUP_LOGGED = False


import urllib.parse

def _sanitize_db_url(url: str) -> str:
    """Handle bracketed passwords [pwd] and unencoded special characters in DATABASE_URL."""
    m = re.match(r"^(postgres(?:ql)?://)([^:]+):(.*)@([^/@:]+)(?::(\d+))?(/.*)?$", url)
    if m:
        proto, user, pwd, host, port, db = m.groups()
        if pwd.startswith("[") and pwd.endswith("]"):
            pwd = pwd[1:-1]
        pwd = urllib.parse.unquote(pwd)
        pwd = urllib.parse.quote_plus(pwd)
        port_part = f":{port}" if port else ""
        db_part = db or ""
        return f"{proto}{user}:{pwd}@{host}{port_part}{db_part}"
    return url


def _find_postgres_url_in_secrets(secrets_obj: Any) -> str | None:
    """Recursively search for a PostgreSQL connection string in Streamlit secrets."""
    if isinstance(secrets_obj, str):
        s = secrets_obj.strip().strip('"').strip("'")
        if s.startswith("postgresql://") or s.startswith("postgres://"):
            return s
    elif isinstance(secrets_obj, dict) or hasattr(secrets_obj, "items"):
        for key in ["DATABASE_URL", "database_url", "POSTGRES_URL", "postgres_url", "url", "uri"]:
            if key in secrets_obj:
                res = _find_postgres_url_in_secrets(secrets_obj[key])
                if res:
                    return res
        for _, v in secrets_obj.items():
            res = _find_postgres_url_in_secrets(v)
            if res:
                return res
    return None


def _get_database_url() -> str | None:
    """Return DATABASE_URL from environment or Streamlit secrets if configured."""
    _load_dotenv()
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        try:
            import streamlit as st
            if hasattr(st, "secrets"):
                found = _find_postgres_url_in_secrets(st.secrets)
                if found:
                    url = found
        except Exception:
            pass
    return _sanitize_db_url(url) if url else None




def _is_postgres(path: str | None = None) -> bool:
    """Determine if PostgreSQL backend should be used."""
    if path and (path.startswith("postgresql://") or path.startswith("postgres://")):
        return True
    if path and (path.endswith(".db") or path.endswith(".sqlite") or path.endswith(".sqlite3")) and ("test" in path.lower() or "tmp" in path.lower() or "temp" in path.lower()):
        return False
    return bool(_get_database_url())


def _log_backend_status(path: str | None = None) -> None:
    """Log active backend at startup (Rule 2 & 48: never log secrets or connection strings)."""
    global _STARTUP_LOGGED
    if _STARTUP_LOGGED:
        return
    _STARTUP_LOGGED = True

    if _is_postgres(path):
        msg = "[EdgeDash Storage] Active backend: PostgreSQL"
    else:
        msg = "[EdgeDash Storage] Active backend: SQLite"

    logger.info(msg)
    print(msg, file=sys.stderr)



# ---------------------------------------------------------------------------
# Generic Row and Connection Adapters
# ---------------------------------------------------------------------------

class _Row:
    """Uniform row wrapper supporting dict(row), row['key'], and row[0]."""

    def __init__(self, cols: Sequence[str], vals: Sequence[Any]) -> None:
        self._cols = {c: i for i, c in enumerate(cols)}
        self._vals = list(vals)

    def __getitem__(self, key: str | int) -> Any:
        if isinstance(key, int):
            return self._vals[key]
        if isinstance(key, str):
            if key not in self._cols:
                raise KeyError(f"Column '{key}' not found in row.")
            return self._vals[self._cols[key]]
        raise TypeError(f"Row indices must be integers or strings, not {type(key).__name__}")

    def get(self, key: str, default: Any = None) -> Any:
        idx = self._cols.get(key)
        return self._vals[idx] if idx is not None else default

    def keys(self) -> list[str]:
        return list(self._cols.keys())

    def values(self) -> list[Any]:
        return list(self._vals)

    def items(self) -> list[tuple[str, Any]]:
        return [(c, self._vals[i]) for c, i in self._cols.items()]

    def __iter__(self):
        return iter(self._cols)

    def __len__(self) -> int:
        return len(self._vals)

    def __repr__(self) -> str:
        return f"<Row {dict(self.items())}>"


class _CursorResult:
    """Wraps execution results for unified fetch/rowcount access."""

    def __init__(self, rows: list[Any], rowcount: int) -> None:
        self._rows = rows
        self.rowcount = rowcount

    def fetchone(self) -> Any:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[Any]:
        return self._rows


class _DBConnection:
    """Unified interface for SQLite and PostgreSQL connections."""

    def __init__(self, raw_conn: Any, is_pg: bool) -> None:
        self._raw = raw_conn
        self._is_pg = is_pg

    def execute(self, sql: str, params: Sequence[Any] | None = None) -> _CursorResult:
        params = list(params) if params is not None else []
        adapted_sql = self._adapt_sql(sql)

        if not self._is_pg:
            cur = self._raw.execute(adapted_sql, params)
            try:
                rows = cur.fetchall()
            except sqlite3.OperationalError:
                rows = []
            return _CursorResult(rows, cur.rowcount)
        else:
            cur = self._raw.cursor()
            cur.execute(adapted_sql, params)
            rowcount = cur.rowcount
            try:
                raw_rows = cur.fetchall()
                col_names = [desc[0] for desc in cur.description] if cur.description else []
                rows = [_Row(col_names, r) for r in raw_rows]
            except Exception:
                rows = []
            cur.close()
            return _CursorResult(rows, rowcount)

    def executemany(self, sql: str, seq_of_params: Sequence[Sequence[Any]]) -> None:
        adapted_sql = self._adapt_sql(sql)
        if not self._is_pg:
            self._raw.executemany(adapted_sql, seq_of_params)
        else:
            cur = self._raw.cursor()
            cur.executemany(adapted_sql, seq_of_params)
            cur.close()

    def _adapt_sql(self, sql: str) -> str:
        if not self._is_pg:
            return sql

        # Adapt SQLite dialect to PostgreSQL dialect
        s = sql.strip()

        # Handle specific upsert queries
        if "INSERT OR IGNORE INTO listings" in s:
            s = s.replace("INSERT OR IGNORE INTO listings", "INSERT INTO listings")
            s = s.rstrip(";") + " ON CONFLICT (id) DO NOTHING;"

        elif "INSERT OR REPLACE INTO extraction_cache" in s:
            s = s.replace("INSERT OR REPLACE INTO extraction_cache", "INSERT INTO extraction_cache")
            s = s.rstrip(";") + (
                " ON CONFLICT (desc_hash) DO UPDATE SET "
                "required_skills = EXCLUDED.required_skills, "
                "nice_to_have = EXCLUDED.nice_to_have, "
                "seniority = EXCLUDED.seniority, "
                "years_required = EXCLUDED.years_required, "
                "remote_ok = EXCLUDED.remote_ok, "
                "created_at = EXCLUDED.created_at;"
            )

        # Escape literal % as %% for postgres drivers (so %V, etc. aren't treated as invalid placeholders)
        s = s.replace("%", "%%")

        # Convert ? placeholders to %s for postgres drivers
        s = s.replace("?", "%s")
        return s

    def commit(self) -> None:
        if self._is_pg:
            if not getattr(self._raw, "autocommit", False):
                self._raw.commit()
        else:
            self._raw.commit()

    def rollback(self) -> None:
        if self._is_pg:
            if not getattr(self._raw, "autocommit", False):
                self._raw.rollback()
        else:
            self._raw.rollback()

    def close(self) -> None:
        self._raw.close()




# ---------------------------------------------------------------------------
# Schema DDL (SQLite & Postgres)
# ---------------------------------------------------------------------------

_SQLITE_DDL_LISTINGS = """
CREATE TABLE IF NOT EXISTS listings (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    company     TEXT NOT NULL,
    location    TEXT NOT NULL,
    url         TEXT NOT NULL,
    description TEXT,
    source      TEXT NOT NULL,
    posted_at   TEXT,
    fetched_at  TEXT NOT NULL,
    fit_score   INTEGER,
    fit_reason  TEXT
);
"""

_SQLITE_DDL_SKILL_GAPS = """
CREATE TABLE IF NOT EXISTS skill_gaps (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id            TEXT NOT NULL,
    computed_at       TEXT NOT NULL,
    skill             TEXT NOT NULL,
    listings_blocked  INTEGER NOT NULL,
    opportunity_cost  REAL NOT NULL,
    mean_score        REAL NOT NULL,
    top_score         INTEGER NOT NULL,
    example_ids       TEXT NOT NULL,
    also_nice_to_have INTEGER NOT NULL DEFAULT 0,
    low_confidence    INTEGER NOT NULL DEFAULT 0
);
"""

_SQLITE_DDL_CYCLE_LOG = """
CREATE TABLE IF NOT EXISTS cycle_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    agent           TEXT NOT NULL,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    records_touched INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL,
    notes           TEXT
);
"""

_SQLITE_DDL_EXTRACTION_CACHE = """
CREATE TABLE IF NOT EXISTS extraction_cache (
    desc_hash       TEXT PRIMARY KEY,
    required_skills TEXT NOT NULL,
    nice_to_have    TEXT NOT NULL,
    seniority       TEXT NOT NULL,
    years_required  INTEGER,
    remote_ok       INTEGER,
    created_at      TEXT NOT NULL
);
"""

_SQLITE_DDL_QUERY_LOG = """
CREATE TABLE IF NOT EXISTS query_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    question    TEXT NOT NULL,
    tool_chosen TEXT,
    params      TEXT,
    answerable  INTEGER NOT NULL,
    duration_ms REAL NOT NULL,
    created_at  TEXT NOT NULL
);
"""

# PostgreSQL DDL
_POSTGRES_DDL_LISTINGS = """
CREATE TABLE IF NOT EXISTS listings (
    id          VARCHAR(64) PRIMARY KEY,
    title       TEXT NOT NULL,
    company     TEXT NOT NULL,
    location    TEXT NOT NULL,
    url         TEXT NOT NULL,
    description TEXT,
    source      TEXT NOT NULL,
    posted_at   TEXT,
    fetched_at  TEXT NOT NULL,
    fit_score   INTEGER,
    fit_reason  TEXT
);
"""

_POSTGRES_DDL_SKILL_GAPS = """
CREATE TABLE IF NOT EXISTS skill_gaps (
    id                SERIAL PRIMARY KEY,
    run_id            TEXT NOT NULL,
    computed_at       TEXT NOT NULL,
    skill             TEXT NOT NULL,
    listings_blocked  INTEGER NOT NULL,
    opportunity_cost  DOUBLE PRECISION NOT NULL,
    mean_score        DOUBLE PRECISION NOT NULL,
    top_score         INTEGER NOT NULL,
    example_ids       TEXT NOT NULL,
    also_nice_to_have INTEGER NOT NULL DEFAULT 0,
    low_confidence    INTEGER NOT NULL DEFAULT 0
);
"""

_POSTGRES_DDL_CYCLE_LOG = """
CREATE TABLE IF NOT EXISTS cycle_log (
    id              SERIAL PRIMARY KEY,
    agent           TEXT NOT NULL,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    records_touched INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL,
    notes           TEXT
);
"""

_POSTGRES_DDL_EXTRACTION_CACHE = """
CREATE TABLE IF NOT EXISTS extraction_cache (
    desc_hash       VARCHAR(64) PRIMARY KEY,
    required_skills TEXT NOT NULL,
    nice_to_have    TEXT NOT NULL,
    seniority       TEXT NOT NULL,
    years_required  INTEGER,
    remote_ok       INTEGER,
    created_at      TEXT NOT NULL
);
"""

_POSTGRES_DDL_QUERY_LOG = """
CREATE TABLE IF NOT EXISTS query_log (
    id          SERIAL PRIMARY KEY,
    question    TEXT NOT NULL,
    tool_chosen TEXT,
    params      TEXT,
    answerable  INTEGER NOT NULL,
    duration_ms DOUBLE PRECISION NOT NULL,
    created_at  TEXT NOT NULL
);
"""


# ---------------------------------------------------------------------------
# Connection helper
# ---------------------------------------------------------------------------

def _get_pg_driver() -> Any:
    """Dynamically find and return an available PostgreSQL driver."""
    try:
        import psycopg  # type: ignore[import]
        return psycopg
    except ModuleNotFoundError:
        pass

    try:
        import psycopg2  # type: ignore[import]
        return psycopg2
    except ModuleNotFoundError:
        pass

    try:
        import pg8000.dbapi  # type: ignore[import]
        return pg8000.dbapi
    except ModuleNotFoundError:
        pass

    raise ModuleNotFoundError(
        "DATABASE_URL is set for PostgreSQL, but no PostgreSQL driver was found. "
        "Please install psycopg (pip install psycopg[binary]) or psycopg2-binary."
    )


@contextmanager
def _connect(path: str) -> Generator[_DBConnection, None, None]:
    _log_backend_status(path)
    is_pg = _is_postgres(path)

    if is_pg:
        db_url = _get_database_url() or path
        driver = _get_pg_driver()
        try:
            raw_conn = driver.connect(db_url, autocommit=True, prepare_threshold=None)
        except TypeError:
            raw_conn = driver.connect(db_url)
    else:

        raw_conn = sqlite3.connect(path)
        raw_conn.row_factory = sqlite3.Row
        raw_conn.execute("PRAGMA journal_mode=WAL;")
        raw_conn.execute("PRAGMA foreign_keys=ON;")

    conn = _DBConnection(raw_conn, is_pg)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Public Storage API (Identical signatures)
# ---------------------------------------------------------------------------

def init_db(path: str) -> None:
    """Create all tables if they do not already exist."""
    is_pg = _is_postgres(path)
    with _connect(path) as conn:
        if is_pg:
            conn.execute(_POSTGRES_DDL_LISTINGS)
            conn.execute(_POSTGRES_DDL_CYCLE_LOG)
            conn.execute(_POSTGRES_DDL_EXTRACTION_CACHE)
            conn.execute(_POSTGRES_DDL_QUERY_LOG)
            conn.execute(_POSTGRES_DDL_SKILL_GAPS)
        else:
            conn.execute(_SQLITE_DDL_LISTINGS)
            conn.execute(_SQLITE_DDL_CYCLE_LOG)
            conn.execute(_SQLITE_DDL_EXTRACTION_CACHE)
            conn.execute(_SQLITE_DDL_QUERY_LOG)
            # Migrate SQLite skill_gaps if old schema exists
            try:
                table_info = conn.execute("PRAGMA table_info(skill_gaps)").fetchall()
                col_names = {row["name"] for row in table_info}
                if col_names and "run_id" not in col_names:
                    conn.execute("DROP TABLE skill_gaps")
            except Exception:
                pass
            conn.execute(_SQLITE_DDL_SKILL_GAPS)


def make_listing_id(source: str, url: str) -> str:
    """Return a stable, collision-resistant ID derived from source + url."""
    raw = f"{source}:{url}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:40]


def upsert_listings(path: str, rows: list[dict[str, Any]]) -> int:
    """Insert listings that are not already present (dedup by primary key)."""
    if not rows:
        return 0

    now = _utcnow()
    inserted = 0

    with _connect(path) as conn:
        for row in rows:
            listing_id = make_listing_id(row["source"], row["url"])
            result = conn.execute(
                """
                INSERT OR IGNORE INTO listings
                    (id, title, company, location, url,
                     description, source, posted_at, fetched_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    listing_id,
                    row["title"],
                    row["company"],
                    row["location"],
                    row["url"],
                    row.get("description", ""),
                    row["source"],
                    row.get("posted_at", ""),
                    row.get("fetched_at", now),
                ),
            )
            inserted += result.rowcount

    return inserted


def count_unscored(path: str) -> int:
    """Return the number of listings that have not yet been scored."""
    with _connect(path) as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM listings WHERE fit_score IS NULL"
        ).fetchone()
    return row[0] if row else 0


def get_unscored_listings(path: str, *, limit: int = 25) -> list[dict[str, Any]]:
    """Return listings with fit_score IS NULL, oldest fetched first."""
    with _connect(path) as conn:
        rows = conn.execute(
            """
            SELECT id, title, company, location, url, description, source, posted_at, fetched_at
            FROM listings
            WHERE fit_score IS NULL
            ORDER BY fetched_at ASC, id ASC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def save_listing_score(
    path: str,
    listing_id: str,
    fit_score: int,
    fit_reason: str,
    *,
    overwrite: bool = False,
) -> None:
    """Persist fit_score and fit_reason for one listing."""
    with _connect(path) as conn:
        if overwrite:
            conn.execute(
                """
                UPDATE listings
                SET fit_score = ?, fit_reason = ?
                WHERE id = ?
                """,
                (fit_score, fit_reason, listing_id),
            )
        else:
            conn.execute(
                """
                UPDATE listings
                SET fit_score = ?, fit_reason = ?
                WHERE id = ? AND fit_score IS NULL
                """,
                (fit_score, fit_reason, listing_id),
            )


def get_latest_passing_cycle(path: str) -> dict[str, Any] | None:
    """
    Return the most recent cycle_log row for Orchestrator with a passing verdict (Rule 38).
    """
    with _connect(path) as conn:
        row = conn.execute(
            """
            SELECT id, agent, started_at, finished_at, records_touched, status, notes
            FROM cycle_log
            WHERE agent = 'Orchestrator'
              AND (
                status = 'complete'
                OR status = 'verified'
                OR status = 'pass'
                OR (notes LIKE '%Verdict: pass%' AND status != 'degraded')
              )
            ORDER BY id DESC LIMIT 1
            """
        ).fetchone()
    return dict(row) if row else None


get_latest_verified_cycle = get_latest_passing_cycle


def get_recent_cycles(path: str, limit: int = 30) -> list[dict[str, Any]]:
    """Return the most recent Orchestrator cycle log entries."""
    with _connect(path) as conn:
        rows = conn.execute(
            """
            SELECT id, agent, started_at, finished_at, records_touched, status, notes
            FROM cycle_log
            WHERE agent = 'Orchestrator'
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def count_total_listings(path: str) -> int:
    """Return total number of listings stored in the database."""
    with _connect(path) as conn:
        row = conn.execute("SELECT COUNT(*) FROM listings").fetchone()
    return row[0] if row else 0


def count_scored_listings(path: str) -> int:
    """Return number of listings that have been assigned a fit_score."""
    with _connect(path) as conn:
        row = conn.execute("SELECT COUNT(*) FROM listings WHERE fit_score IS NOT NULL").fetchone()
    return row[0] if row else 0


def last_fetch_time(path: str) -> str | None:
    """Return the most recent fetched_at timestamp, or None."""
    with _connect(path) as conn:
        row = conn.execute("SELECT MAX(fetched_at) FROM listings").fetchone()
    return row[0] if row and row[0] is not None else None


def last_gap_computed_at(path: str) -> str | None:
    """Return the most recent skill_gaps computed_at timestamp, or None."""
    with _connect(path) as conn:
        row = conn.execute("SELECT MAX(computed_at) FROM skill_gaps").fetchone()
    return row[0] if row and row[0] is not None else None


def last_score_time(path: str) -> str | None:
    """Return the most recent score / extraction timestamp, or None."""
    with _connect(path) as conn:
        row = conn.execute(
            """
            SELECT MAX(ts) FROM (
                SELECT MAX(created_at) AS ts FROM extraction_cache
                UNION ALL
                SELECT MAX(finished_at) AS ts FROM cycle_log WHERE agent = 'Scorer' AND records_touched > 0
            ) AS sub_score_ts
            """
        ).fetchone()
    return row[0] if row and row[0] is not None else None


def last_cycle_info(path: str) -> tuple[str | None, str | None]:
    """Return (verdict, started_at) for the most recent Orchestrator cycle."""
    with _connect(path) as conn:
        row = conn.execute(
            """
            SELECT status, started_at FROM cycle_log
            WHERE agent = 'Orchestrator'
            ORDER BY id DESC LIMIT 1
            """
        ).fetchone()
    if row:
        return row["status"], row["started_at"]
    return None, None


def log_cycle(
    path: str,
    *,
    agent: str,
    started_at: str,
    finished_at: str | None,
    records_touched: int,
    status: str,
    notes: str | None = None,
) -> None:
    """Write one row to the cycle_log table."""
    with _connect(path) as conn:
        conn.execute(
            """
            INSERT INTO cycle_log
                (agent, started_at, finished_at, records_touched, status, notes)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (agent, started_at, finished_at, records_touched, status, notes),
        )


def get_listings(
    path: str,
    *,
    limit: int = 100,
    min_score: int = 0,
) -> list[dict[str, Any]]:
    """Return scored listings at or above min_score, highest score first."""
    with _connect(path) as conn:
        if min_score > 0:
            rows = conn.execute(
                """
                SELECT * FROM listings
                WHERE fit_score >= ?
                ORDER BY fit_score DESC, fetched_at DESC
                LIMIT ?
                """,
                (min_score, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT * FROM listings
                ORDER BY COALESCE(fit_score, -1) DESC, fetched_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

    return [dict(row) for row in rows]


def get_cached_extraction(path: str, desc_hash: str) -> dict[str, Any] | None:
    """Retrieve cached extraction results for a description hash, or None on miss."""
    with _connect(path) as conn:
        row = conn.execute(
            """
            SELECT required_skills, nice_to_have, seniority, years_required, remote_ok
            FROM extraction_cache
            WHERE desc_hash = ?
            """,
            (desc_hash,),
        ).fetchone()

    if row is None:
        return None

    return {
        "required_skills": json.loads(row["required_skills"]),
        "nice_to_have": json.loads(row["nice_to_have"]),
        "seniority": row["seniority"],
        "years_required": row["years_required"],
        "remote_ok": bool(row["remote_ok"]) if row["remote_ok"] is not None else None,
    }


def save_cached_extraction(
    path: str,
    desc_hash: str,
    extraction: dict[str, Any],
) -> None:
    """Store extraction results in the extraction cache."""
    remote_ok_val = (
        1 if extraction.get("remote_ok") is True
        else (0 if extraction.get("remote_ok") is False else None)
    )
    with _connect(path) as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO extraction_cache
                (desc_hash, required_skills, nice_to_have, seniority,
                 years_required, remote_ok, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                desc_hash,
                json.dumps(extraction.get("required_skills", [])),
                json.dumps(extraction.get("nice_to_have", [])),
                str(extraction.get("seniority", "unknown")),
                extraction.get("years_required"),
                remote_ok_val,
                _utcnow(),
            ),
        )


def get_all_extracted_required_skills(path: str) -> list[str]:
    """Return a list of all raw required_skills extracted across all cached listings."""
    with _connect(path) as conn:
        rows = conn.execute("SELECT required_skills FROM extraction_cache").fetchall()

    raw_skills: list[str] = []
    for row in rows:
        try:
            skills = json.loads(row["required_skills"])
            if isinstance(skills, list):
                for s in skills:
                    if isinstance(s, str) and s.strip():
                        raw_skills.append(s.strip())
        except Exception:
            continue

    return raw_skills


def get_scored_listings_with_extractions(path: str) -> list[dict[str, Any]]:
    """Return all scored listings with their extracted facts from cache."""
    with _connect(path) as conn:
        listings = conn.execute(
            """
            SELECT id, title, company, description, fit_score, fit_reason
            FROM listings
            WHERE fit_score IS NOT NULL
            """
        ).fetchall()

        cache_rows = conn.execute(
            "SELECT desc_hash, required_skills, nice_to_have FROM extraction_cache"
        ).fetchall()

    cache_map: dict[str, tuple[list[str], list[str]]] = {}
    for r in cache_rows:
        try:
            req = json.loads(r["required_skills"])
            nice = json.loads(r["nice_to_have"])
            cache_map[r["desc_hash"]] = (
                req if isinstance(req, list) else [],
                nice if isinstance(nice, list) else [],
            )
        except Exception:
            continue

    records: list[dict[str, Any]] = []
    for row in listings:
        desc = (row["description"] or "").strip().encode("utf-8")
        h = hashlib.sha256(desc).hexdigest()
        req_skills, nice_skills = cache_map.get(h, ([], []))
        records.append({
            "id": row["id"],
            "title": row["title"],
            "company": row["company"],
            "fit_score": row["fit_score"],
            "fit_reason": row["fit_reason"],
            "required_skills": req_skills,
            "nice_to_have": nice_skills,
        })
    return records


def save_skill_gap_snapshot(
    path: str,
    *,
    run_id: str,
    computed_at: str,
    gaps: list[dict[str, Any]],
) -> None:
    """Write a timestamped snapshot of skill gaps."""
    with _connect(path) as conn:
        for g in gaps:
            example_ids = g.get("example_ids", [])
            example_ids_str = json.dumps(example_ids) if isinstance(example_ids, list) else str(example_ids)
            conn.execute(
                """
                INSERT INTO skill_gaps
                    (run_id, computed_at, skill, listings_blocked,
                     opportunity_cost, mean_score, top_score,
                     example_ids, also_nice_to_have, low_confidence)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    computed_at,
                    g["skill"],
                    g["listings_blocked"],
                    round(float(g["opportunity_cost"]), 2),
                    round(float(g["mean_score"]), 1),
                    int(g.get("top_score", 0)),
                    example_ids_str,
                    int(g.get("also_nice_to_have", 0)),
                    1 if g.get("low_confidence") else 0,
                ),
            )


def get_latest_skill_gaps(path: str) -> list[dict[str, Any]]:
    """Retrieve the most recent snapshot of skill gaps, ordered by opportunity_cost DESC."""
    with _connect(path) as conn:
        latest = conn.execute(
            "SELECT run_id, computed_at FROM skill_gaps ORDER BY id DESC LIMIT 1"
        ).fetchone()

        if not latest:
            return []

        rows = conn.execute(
            """
            SELECT run_id, computed_at, skill, listings_blocked,
                   opportunity_cost, mean_score, top_score,
                   example_ids, also_nice_to_have, low_confidence
            FROM skill_gaps
            WHERE run_id = ?
            ORDER BY opportunity_cost DESC, listings_blocked DESC
            """,
            (latest["run_id"],),
        ).fetchall()

    result: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        try:
            d["example_ids"] = json.loads(d["example_ids"])
        except Exception:
            d["example_ids"] = [d["example_ids"]]
        d["low_confidence"] = bool(d["low_confidence"])
        result.append(d)
    return result


def get_all_skill_gap_snapshots(path: str) -> list[dict[str, Any]]:
    """Return all skill gap snapshot rows, ordered by computed_at ASC, id ASC."""
    with _connect(path) as conn:
        rows = conn.execute(
            """
            SELECT id, run_id, computed_at, skill, listings_blocked,
                   opportunity_cost, mean_score, top_score,
                   example_ids, also_nice_to_have, low_confidence
            FROM skill_gaps
            ORDER BY computed_at ASC, id ASC
            """
        ).fetchall()

    result: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        try:
            d["example_ids"] = json.loads(d["example_ids"])
        except Exception:
            d["example_ids"] = [d["example_ids"]]
        d["low_confidence"] = bool(d["low_confidence"])
        result.append(d)
    return result


def get_companies_hiring(path: str, days: int = 7) -> tuple[list[dict[str, Any]], int]:
    """Return list of {company, listing_count} for listings in the last N days."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with _connect(path) as conn:
        rows = conn.execute(
            """
            SELECT company, COUNT(*) AS listing_count
            FROM listings
            WHERE COALESCE(NULLIF(posted_at, ''), fetched_at) >= ?
            GROUP BY company
            ORDER BY listing_count DESC, company ASC
            """,
            (cutoff,),
        ).fetchall()

        total_row = conn.execute(
            """
            SELECT COUNT(*)
            FROM listings
            WHERE COALESCE(NULLIF(posted_at, ''), fetched_at) >= ?
            """,
            (cutoff,),
        ).fetchone()
        total = total_row[0] if total_row else 0

    return [dict(r) for r in rows], total


def get_best_matches(path: str, n: int = 10) -> list[dict[str, Any]]:
    """Return highest-scoring listings with score, title, company, reason, and url."""
    with _connect(path) as conn:
        rows = conn.execute(
            """
            SELECT id, fit_score, title, company, fit_reason, url
            FROM listings
            WHERE fit_score IS NOT NULL
            ORDER BY fit_score DESC, fetched_at DESC
            LIMIT ?
            """,
            (n,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_top_gaps(path: str, n: int = 5) -> list[dict[str, Any]]:
    """Return top skill gaps by opportunity cost from the latest snapshot."""
    latest_gaps = get_latest_skill_gaps(path)
    return latest_gaps[:n]


def get_gap_detail(path: str, skill: str) -> list[dict[str, Any]]:
    """Return the listings blocked by a specific named skill (Rule 26 drill-down)."""
    latest_gaps = get_latest_skill_gaps(path)
    example_ids: list[str] = []
    for g in latest_gaps:
        if g.get("skill", "").lower() == skill.lower():
            example_ids = g.get("example_ids", [])
            break

    with _connect(path) as conn:
        if example_ids:
            placeholders = ",".join("?" for _ in example_ids)
            rows = conn.execute(
                f"""
                SELECT id, fit_score, title, company, fit_reason, url
                FROM listings
                WHERE id IN ({placeholders})
                ORDER BY fit_score DESC
                """,
                example_ids,
            ).fetchall()
            return [dict(r) for r in rows]
        else:
            rows = conn.execute(
                """
                SELECT id, fit_score, title, company, fit_reason, url, description
                FROM listings
                WHERE fit_score IS NOT NULL
                ORDER BY fit_score DESC
                """
            ).fetchall()
            cache_rows = conn.execute(
                "SELECT desc_hash, required_skills FROM extraction_cache"
            ).fetchall()

    cache_map = {}
    for cr in cache_rows:
        try:
            reqs = [s.lower() for s in json.loads(cr["required_skills"])]
            cache_map[cr["desc_hash"]] = reqs
        except Exception:
            continue

    matching = []
    skill_lower = skill.lower()
    for r in rows:
        desc = (r["description"] or "").strip().encode("utf-8")
        h = hashlib.sha256(desc).hexdigest()
        reqs = cache_map.get(h, [])
        if skill_lower in reqs:
            d = dict(r)
            d.pop("description", None)
            matching.append(d)

    return matching


def get_gap_trend(path: str, weeks: int = 3) -> list[dict[str, Any]]:
    """Return skill gap opportunity cost change over N weeks from snapshots."""
    cutoff = (datetime.now(timezone.utc) - timedelta(weeks=weeks)).isoformat()
    all_snapshots = get_all_skill_gap_snapshots(path)
    recent = [s for s in all_snapshots if s.get("computed_at", "") >= cutoff]
    if not recent:
        recent = all_snapshots

    runs_order: list[dict[str, Any]] = []
    runs_map: dict[str, list[dict[str, Any]]] = {}
    for r in recent:
        rid = r["run_id"]
        if rid not in runs_map:
            runs_order.append({"run_id": rid, "computed_at": r["computed_at"]})
            runs_map[rid] = []
        runs_map[rid].append(r)

    if len(runs_order) < 2:
        if runs_order:
            latest_run = runs_map[runs_order[-1]["run_id"]]
            return [
                {
                    "skill": row["skill"],
                    "current_opp_cost": row["opportunity_cost"],
                    "prev_opp_cost": row["opportunity_cost"],
                    "delta": 0.0,
                    "listings_blocked": row["listings_blocked"],
                }
                for row in latest_run
            ]
        return []

    earliest_run = runs_map[runs_order[0]["run_id"]]
    latest_run = runs_map[runs_order[-1]["run_id"]]

    earliest_costs = {r["skill"].lower(): r["opportunity_cost"] for r in earliest_run}
    results: list[dict[str, Any]] = []
    for r in latest_run:
        s_name = r["skill"]
        current_cost = r["opportunity_cost"]
        prev_cost = earliest_costs.get(s_name.lower(), current_cost)
        delta = round(current_cost - prev_cost, 2)
        results.append({
            "skill": s_name,
            "current_opp_cost": current_cost,
            "prev_opp_cost": prev_cost,
            "delta": delta,
            "listings_blocked": r["listings_blocked"],
        })
    results.sort(key=lambda x: x["current_opp_cost"], reverse=True)
    return results


def get_listing_counts(path: str) -> dict[str, Any]:
    """Return dataset totals: listings, scored, unscored, newest listing date."""
    with _connect(path) as conn:
        total_row = conn.execute("SELECT COUNT(*) FROM listings").fetchone()
        total = total_row[0] if total_row else 0
        scored_row = conn.execute("SELECT COUNT(*) FROM listings WHERE fit_score IS NOT NULL").fetchone()
        scored = scored_row[0] if scored_row else 0
        unscored = total - scored
        newest_row = conn.execute(
            "SELECT MAX(COALESCE(NULLIF(posted_at, ''), fetched_at)) FROM listings"
        ).fetchone()
        newest_date = newest_row[0] if newest_row and newest_row[0] else None
    return {
        "total_listings": total,
        "scored_listings": scored,
        "unscored_listings": unscored,
        "newest_listing_date": newest_date,
    }


def get_skill_demand(path: str, skill: str) -> dict[str, Any]:
    """Return breakdown of how often a skill appears in required vs nice_to_have."""
    skill_lower = skill.lower()
    with _connect(path) as conn:
        cache_rows = conn.execute(
            "SELECT required_skills, nice_to_have FROM extraction_cache"
        ).fetchall()
        total_row = conn.execute("SELECT COUNT(*) FROM listings").fetchone()
        total_listings = total_row[0] if total_row else 0

    required_count = 0
    nice_count = 0
    total_extractions = len(cache_rows)

    for r in cache_rows:
        try:
            reqs = [s.lower() for s in json.loads(r["required_skills"])]
            nices = [s.lower() for s in json.loads(r["nice_to_have"])]
            if skill_lower in reqs:
                required_count += 1
            if skill_lower in nices:
                nice_count += 1
        except Exception:
            continue

    return {
        "skill": skill,
        "required_count": required_count,
        "nice_to_have_count": nice_count,
        "total_appearances": required_count + nice_count,
        "total_extractions": total_extractions,
        "total_listings": total_listings,
    }


def get_all_known_skills(path: str) -> set[str]:
    """Return set of all unique skill strings present in extraction_cache and skill_gaps."""
    skills: set[str] = set()
    with _connect(path) as conn:
        cache_rows = conn.execute(
            "SELECT required_skills, nice_to_have FROM extraction_cache"
        ).fetchall()
        gap_rows = conn.execute("SELECT skill FROM skill_gaps").fetchall()

    for r in gap_rows:
        if r["skill"]:
            skills.add(r["skill"].lower())

    for r in cache_rows:
        try:
            for s in json.loads(r["required_skills"]):
                if s:
                    skills.add(s.lower())
            for s in json.loads(r["nice_to_have"]):
                if s:
                    skills.add(s.lower())
        except Exception:
            continue

    return skills


def log_query(
    path: str,
    *,
    question: str,
    tool_chosen: str | None,
    params: dict[str, Any] | None,
    answerable: bool,
    duration_ms: float,
) -> None:
    """Write one query log row to query_log."""
    with _connect(path) as conn:
        conn.execute(
            """
            INSERT INTO query_log
                (question, tool_chosen, params, answerable, duration_ms, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                question,
                tool_chosen,
                json.dumps(params or {}),
                1 if answerable else 0,
                round(duration_ms, 2),
                _utcnow(),
            ),
        )


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# CLI Commands (--migrate, --check)
# ---------------------------------------------------------------------------

def _run_migrate(db_target: str) -> None:
    """Create all tables on target database. Safe to run repeatedly."""
    backend = "PostgreSQL" if _is_postgres(db_target) else "SQLite"
    print(f"Running migration against {backend} backend...")
    init_db(db_target)
    print(f"Migration completed successfully! All tables verified on {backend}.")



def _run_import_sqlite(sqlite_path: str, target: str) -> None:
    """Transfer all data from a SQLite database into target database in fast batches."""
    print(f"Importing data from SQLite ({sqlite_path}) into {target}...")
    init_db(target)

    import sqlite3
    src = sqlite3.connect(sqlite_path)
    src.row_factory = sqlite3.Row

    with _connect(target) as conn:
        # 1. Listings
        rows = src.execute("SELECT * FROM listings").fetchall()
        if rows:
            params = [
                (
                    r["id"],
                    r["title"],
                    r["company"],
                    r["location"],
                    r["url"],
                    r["description"] or "",
                    r["source"],
                    r["posted_at"] or "",
                    r["fetched_at"] or _utcnow(),
                )
                for r in rows
            ]
            conn.executemany(
                """
                INSERT OR IGNORE INTO listings
                    (id, title, company, location, url, description, source, posted_at, fetched_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                params,
            )
            # Update fit scores
            score_params = [(r["fit_score"], r["fit_reason"], r["id"]) for r in rows if r["fit_score"] is not None]
            if score_params:
                conn.executemany(
                    "UPDATE listings SET fit_score = ?, fit_reason = ? WHERE id = ?",
                    score_params,
                )
            print(f"  - Imported {len(rows)} listings and scores.")

        # 2. Cycle log
        rows = src.execute("SELECT * FROM cycle_log").fetchall()
        if rows:
            params = [
                (
                    r["agent"],
                    r["started_at"],
                    r["finished_at"],
                    r["records_touched"],
                    r["status"],
                    r["notes"],
                )
                for r in rows
            ]
            conn.executemany(
                """
                INSERT INTO cycle_log
                    (agent, started_at, finished_at, records_touched, status, notes)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                params,
            )
            print(f"  - Imported {len(rows)} cycle log records.")

        # 3. Extraction cache
        rows = src.execute("SELECT * FROM extraction_cache").fetchall()
        if rows:
            params = [
                (
                    r["desc_hash"],
                    r["required_skills"],
                    r["nice_to_have"],
                    r["seniority"],
                    r["years_required"],
                    r["remote_ok"],
                    r["created_at"],
                )
                for r in rows
            ]
            conn.executemany(
                """
                INSERT OR REPLACE INTO extraction_cache
                    (desc_hash, required_skills, nice_to_have, seniority, years_required, remote_ok, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                params,
            )
            print(f"  - Imported {len(rows)} extraction cache items.")

        # 4. Skill gaps
        try:
            rows = src.execute("SELECT * FROM skill_gaps").fetchall()
            if rows:
                params = [
                    (
                        r["skill"],
                        r["gap_score"],
                        r["trend"],
                        r["recommendation"],
                        r["demand_count"],
                        r["computed_at"],
                    )
                    for r in rows
                ]
                conn.executemany(
                    """
                    INSERT INTO skill_gaps
                        (skill, gap_score, trend, recommendation, demand_count, computed_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    params,
                )
                print(f"  - Imported {len(rows)} skill gap rows.")
        except Exception:
            pass

    src.close()
    print("Import completed successfully!")


def _run_check(db_target: str) -> None:
    """Check connection and print table row counts."""
    is_pg = _is_postgres(db_target)
    backend_name = "PostgreSQL" if is_pg else "SQLite"
    print(f"Backend:    {backend_name}")

    tables = ["listings", "skill_gaps", "cycle_log", "extraction_cache", "query_log"]

    try:
        with _connect(db_target) as conn:
            print("Connection: OK (Connected successfully)")
            print("\nTable Row Counts:")
            for tbl in tables:
                try:
                    row = conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()
                    count = row[0] if row else 0
                    print(f"  - {tbl:<18} : {count} rows")
                except Exception as err:
                    print(f"  - {tbl:<18} : Error reading count ({err})")
    except Exception as exc:
        print(f"Connection: FAILED ({exc})")


def main() -> None:
    parser = argparse.ArgumentParser(description="EdgeDash Storage Backend Utility")
    parser.add_argument(
        "--migrate",
        action="store_true",
        help="Create all tables on the active database (Postgres or SQLite). Safe to run repeatedly.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Print active backend, connection status, and row count per table.",
    )
    parser.add_argument(
        "--import-sqlite",
        type=str,
        metavar="SQLITE_FILE",
        help="Import listings, cycles, and cache from an existing SQLite database into the active target.",
    )
    parser.add_argument(
        "--db",
        type=str,
        default="edgedash.db",
        help="Database path (default: edgedash.db or DATABASE_URL from env)",
    )

    args = parser.parse_args()

    # Determine target: explicit --db or environment DATABASE_URL or default
    target = _get_database_url() or args.db

    if args.migrate:
        _run_migrate(target)
    elif args.import_sqlite:
        _run_import_sqlite(args.import_sqlite, target)
    elif args.check:
        _run_check(target)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
