"""SQLite database schema, connection management, and upsert/query helpers."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Generator

_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS services (
    service_uid    TEXT NOT NULL,
    run_date       TEXT NOT NULL,       -- ISO date YYYY-MM-DD
    origin         TEXT NOT NULL,       -- CRS code
    destination    TEXT NOT NULL,       -- CRS code
    operator_code  TEXT,
    operator_name  TEXT,
    PRIMARY KEY (service_uid, run_date)
);

CREATE TABLE IF NOT EXISTS daily_observations (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    service_uid          TEXT NOT NULL,
    run_date             TEXT NOT NULL,       -- ISO date YYYY-MM-DD
    scheduled_departure  TEXT NOT NULL,       -- HH:MM
    actual_departure     TEXT,               -- HH:MM; NULL if not yet reported
    scheduled_arrival    TEXT NOT NULL,       -- HH:MM
    actual_arrival       TEXT,               -- HH:MM; NULL if not yet reported
    delay_mins           INTEGER,            -- positive = late; NULL if unknown
    platform             TEXT,
    platform_changed     INTEGER DEFAULT 0,  -- 1 if platform differs from booked
    cancelled            INTEGER DEFAULT 0,  -- 1 if cancelled
    cancel_reason_code   TEXT,
    cancel_reason_text   TEXT,
    is_actual            INTEGER DEFAULT 0,  -- 1 if times are confirmed actual (not estimated)
    source               TEXT NOT NULL DEFAULT 'rtt',  -- 'rtt' or 'hsp'
    captured_at          TEXT NOT NULL,      -- ISO 8601 timestamp
    UNIQUE(service_uid, run_date, source)
);

CREATE TABLE IF NOT EXISTS hsp_metrics (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    origin              TEXT NOT NULL,
    destination         TEXT NOT NULL,
    from_time           TEXT NOT NULL,       -- HHMM
    to_time             TEXT NOT NULL,       -- HHMM
    period_start        TEXT NOT NULL,       -- ISO date
    period_end          TEXT NOT NULL,       -- ISO date
    total_services      INTEGER,
    on_time_count       INTEGER,             -- arrived within 0 min of schedule
    late_1_5_count      INTEGER,             -- 1–5 mins late
    late_5_10_count     INTEGER,
    late_10_15_count    INTEGER,
    late_15_20_count    INTEGER,
    late_20_30_count    INTEGER,
    late_30_plus_count  INTEGER,
    cancel_count        INTEGER,
    retrieved_at        TEXT NOT NULL,
    UNIQUE(origin, destination, from_time, to_time, period_start, period_end)
);

CREATE TABLE IF NOT EXISTS delay_attributions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_number   TEXT,
    run_date          TEXT NOT NULL,         -- ISO date YYYY-MM-DD
    trust_train_id    TEXT,
    service_uid       TEXT,
    stanox            TEXT,
    event_type        TEXT,
    delay_mins        REAL,                  -- PFPI_MINUTES
    reason_code       TEXT,                 -- two-char DAPR code
    reason_text       TEXT,
    responsible_org   TEXT,
    financial_period  TEXT,
    csv_filename      TEXT,                 -- source file for provenance
    UNIQUE(incident_number, trust_train_id, event_type)
);

CREATE TABLE IF NOT EXISTS delay_codes (
    code             TEXT PRIMARY KEY,
    description      TEXT NOT NULL,
    category         TEXT NOT NULL,
    responsible_type TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_obs_date   ON daily_observations(run_date);
CREATE INDEX IF NOT EXISTS idx_obs_uid    ON daily_observations(service_uid);
CREATE INDEX IF NOT EXISTS idx_attr_date  ON delay_attributions(run_date);
CREATE INDEX IF NOT EXISTS idx_attr_code  ON delay_attributions(reason_code);
"""


def init_db(db_path: Path, delay_codes_path: Path | None = None) -> None:
    """Create all tables and indexes. Safe to call on an existing database."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.executescript(_SCHEMA)
        if delay_codes_path and delay_codes_path.exists():
            _load_delay_codes(conn, delay_codes_path)


def _load_delay_codes(conn: sqlite3.Connection, path: Path) -> None:
    """Populate the delay_codes reference table from delay_codes.json."""
    data = json.loads(path.read_text())
    rows = [
        (code, info["description"], info["category"], info["responsible"])
        for code, info in data["codes"].items()
    ]
    conn.executemany(
        "INSERT OR REPLACE INTO delay_codes(code, description, category, responsible_type) "
        "VALUES (?, ?, ?, ?)",
        rows,
    )
    conn.commit()


@contextmanager
def get_connection(db_path: Path) -> Generator[sqlite3.Connection, None, None]:
    """Yield a SQLite connection with WAL mode and Row factory."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def upsert_service(conn: sqlite3.Connection, row: dict) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO services
           (service_uid, run_date, origin, destination, operator_code, operator_name)
           VALUES (:service_uid, :run_date, :origin, :destination, :operator_code, :operator_name)""",
        row,
    )


def upsert_observation(conn: sqlite3.Connection, row: dict) -> None:
    """Insert or replace a daily observation. RTT data overwrites HSP for the same service/date."""
    conn.execute(
        """INSERT OR REPLACE INTO daily_observations
           (service_uid, run_date, scheduled_departure, actual_departure,
            scheduled_arrival, actual_arrival, delay_mins, platform,
            platform_changed, cancelled, cancel_reason_code, cancel_reason_text,
            is_actual, source, captured_at)
           VALUES
           (:service_uid, :run_date, :scheduled_departure, :actual_departure,
            :scheduled_arrival, :actual_arrival, :delay_mins, :platform,
            :platform_changed, :cancelled, :cancel_reason_code, :cancel_reason_text,
            :is_actual, :source, :captured_at)""",
        row,
    )


def upsert_hsp_metrics(conn: sqlite3.Connection, row: dict) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO hsp_metrics
           (origin, destination, from_time, to_time, period_start, period_end,
            total_services, on_time_count, late_1_5_count, late_5_10_count,
            late_10_15_count, late_15_20_count, late_20_30_count, late_30_plus_count,
            cancel_count, retrieved_at)
           VALUES
           (:origin, :destination, :from_time, :to_time, :period_start, :period_end,
            :total_services, :on_time_count, :late_1_5_count, :late_5_10_count,
            :late_10_15_count, :late_15_20_count, :late_20_30_count, :late_30_plus_count,
            :cancel_count, :retrieved_at)""",
        row,
    )


def insert_attributions(conn: sqlite3.Connection, rows: list[dict]) -> int:
    """Insert attribution rows, ignoring conflicts. Returns count of new rows inserted."""
    inserted = 0
    for row in rows:
        cur = conn.execute(
            """INSERT OR IGNORE INTO delay_attributions
               (incident_number, run_date, trust_train_id, service_uid, stanox,
                event_type, delay_mins, reason_code, reason_text,
                responsible_org, financial_period, csv_filename)
               VALUES
               (:incident_number, :run_date, :trust_train_id, :service_uid, :stanox,
                :event_type, :delay_mins, :reason_code, :reason_text,
                :responsible_org, :financial_period, :csv_filename)""",
            row,
        )
        inserted += cur.rowcount
    return inserted


# ---------------------------------------------------------------------------
# Query helpers used by the dashboard
# ---------------------------------------------------------------------------

def query_departure_times(conn: sqlite3.Connection) -> list[str]:
    """Return all distinct scheduled departure times in the DB, sorted."""
    rows = conn.execute(
        "SELECT DISTINCT scheduled_departure FROM daily_observations ORDER BY scheduled_departure"
    ).fetchall()
    return [r[0] for r in rows]


def query_today_observations(
    conn: sqlite3.Connection,
    date: str,
    departure_time: str | None = None,
) -> list[sqlite3.Row]:
    sql = """SELECT o.*, dc.description AS reason_description, dc.category AS reason_category
             FROM daily_observations o
             LEFT JOIN delay_codes dc ON dc.code = o.cancel_reason_code
             WHERE o.run_date = ?"""
    params: list = [date]
    if departure_time:
        sql += " AND o.scheduled_departure = ?"
        params.append(departure_time)
    sql += " ORDER BY o.scheduled_departure"
    return conn.execute(sql, params).fetchall()


def query_daily_trends(
    conn: sqlite3.Connection,
    days: int = 30,
    departure_time: str | None = None,
) -> list[sqlite3.Row]:
    extra = "AND scheduled_departure = ?" if departure_time else ""
    params: list = [f"-{days}"]
    if departure_time:
        params.append(departure_time)
    return conn.execute(
        f"""SELECT
               run_date,
               COUNT(*) AS num_services,
               AVG(CASE WHEN cancelled = 0 THEN delay_mins END) AS avg_delay_mins,
               MAX(CASE WHEN cancelled = 0 THEN delay_mins END) AS max_delay_mins,
               ROUND(
                   100.0 * SUM(CASE WHEN delay_mins IS NOT NULL AND delay_mins <= 5 AND cancelled = 0 THEN 1 ELSE 0 END)
                   / NULLIF(SUM(CASE WHEN cancelled = 0 THEN 1 ELSE 0 END), 0),
                   1
               ) AS pct_on_time,
               SUM(cancelled) AS num_cancelled
           FROM daily_observations
           WHERE run_date >= date('now', ? || ' days')
             {extra}
             AND source = (
                 SELECT MAX(source) FROM daily_observations d2
                 WHERE d2.service_uid = daily_observations.service_uid
                   AND d2.run_date = daily_observations.run_date
             )
           GROUP BY run_date
           ORDER BY run_date""",
        params,
    ).fetchall()


def query_worst_days(
    conn: sqlite3.Connection,
    limit: int = 10,
    departure_time: str | None = None,
) -> list[sqlite3.Row]:
    extra = "AND scheduled_departure = ?" if departure_time else ""
    params: list = []
    if departure_time:
        params.append(departure_time)
    params.append(limit)
    return conn.execute(
        f"""SELECT
               run_date,
               COUNT(*) AS num_services,
               ROUND(AVG(CASE WHEN cancelled = 0 THEN delay_mins END), 1) AS avg_delay_mins,
               MAX(CASE WHEN cancelled = 0 THEN delay_mins END) AS max_delay_mins,
               SUM(cancelled) AS num_cancelled
           FROM daily_observations
           WHERE 1=1 {extra}
           GROUP BY run_date
           HAVING num_services > 0
           ORDER BY avg_delay_mins DESC NULLS LAST
           LIMIT ?""",
        params,
    ).fetchall()


def query_delay_reasons(conn: sqlite3.Connection, months: int = 3) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT
               da.reason_code,
               COALESCE(dc.description, da.reason_text, 'Unknown') AS reason_text,
               COALESCE(dc.category, 'Unknown') AS category,
               COALESCE(dc.responsible_type, 'Unknown') AS responsible_type,
               COUNT(*) AS incident_count,
               ROUND(SUM(da.delay_mins), 1) AS total_delay_mins
           FROM delay_attributions da
           LEFT JOIN delay_codes dc ON dc.code = da.reason_code
           WHERE da.run_date >= date('now', ? || ' months')
             AND da.reason_code IS NOT NULL
           GROUP BY da.reason_code
           ORDER BY total_delay_mins DESC""",
        (f"-{months}",),
    ).fetchall()


def query_hsp_summary(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT
               period_start,
               period_end,
               total_services,
               on_time_count,
               late_1_5_count,
               late_5_10_count,
               late_10_15_count,
               late_15_20_count,
               late_20_30_count,
               late_30_plus_count,
               cancel_count
           FROM hsp_metrics
           ORDER BY period_start DESC
           LIMIT 12""",
    ).fetchall()
