"""Tests for database schema, upserts, and query helpers."""
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from late_train.db import (
    init_db,
    get_connection,
    upsert_service,
    upsert_observation,
    upsert_hsp_metrics,
    insert_attributions,
    query_today_observations,
    query_daily_trends,
    query_worst_days,
    query_delay_reasons,
)

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def db_path(tmp_path) -> Path:
    path = tmp_path / "test.db"
    init_db(path)
    return path


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _obs(service_uid="W12345", run_date="2026-03-10", source="rtt", delay=5, cancelled=0):
    return {
        "service_uid": service_uid,
        "run_date": run_date,
        "scheduled_departure": "07:45",
        "actual_departure": "07:50",
        "scheduled_arrival": "09:00",
        "actual_arrival": "09:05",
        "delay_mins": delay,
        "platform": "1",
        "platform_changed": 0,
        "cancelled": cancelled,
        "cancel_reason_code": None,
        "cancel_reason_text": None,
        "is_actual": 1,
        "source": source,
        "captured_at": _now(),
    }


def test_init_db_creates_tables(db_path):
    with sqlite3.connect(db_path) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    assert {"services", "daily_observations", "hsp_metrics", "delay_attributions", "delay_codes"} <= tables


def test_upsert_observation_insert_and_replace(db_path):
    with get_connection(db_path) as conn:
        upsert_observation(conn, _obs(delay=5))
        rows = conn.execute("SELECT delay_mins FROM daily_observations").fetchall()
        assert len(rows) == 1
        assert rows[0]["delay_mins"] == 5

        # Replace with updated delay (same service_uid + run_date + source)
        upsert_observation(conn, _obs(delay=12))
        rows = conn.execute("SELECT delay_mins FROM daily_observations").fetchall()
        assert len(rows) == 1
        assert rows[0]["delay_mins"] == 12


def test_upsert_observation_rtt_and_hsp_coexist(db_path):
    """RTT and HSP records for the same service/date are stored separately."""
    with get_connection(db_path) as conn:
        upsert_observation(conn, _obs(source="rtt"))
        upsert_observation(conn, _obs(source="hsp"))
        count = conn.execute("SELECT COUNT(*) FROM daily_observations").fetchone()[0]
        assert count == 2


def test_upsert_hsp_metrics(db_path):
    row = {
        "origin": "LBG",
        "destination": "BTN",
        "from_time": "0700",
        "to_time": "0930",
        "period_start": "2026-01-01",
        "period_end": "2026-01-31",
        "total_services": 20,
        "on_time_count": 12,
        "late_1_5_count": 3,
        "late_5_10_count": 2,
        "late_10_15_count": 1,
        "late_15_20_count": 1,
        "late_20_30_count": 1,
        "late_30_plus_count": 0,
        "cancel_count": 0,
        "retrieved_at": _now(),
    }
    with get_connection(db_path) as conn:
        upsert_hsp_metrics(conn, row)
        result = conn.execute("SELECT total_services FROM hsp_metrics").fetchone()
        assert result["total_services"] == 20

        # Idempotent
        upsert_hsp_metrics(conn, row)
        count = conn.execute("SELECT COUNT(*) FROM hsp_metrics").fetchone()[0]
        assert count == 1


def test_insert_attributions(db_path):
    rows = [
        {
            "incident_number": "INC001",
            "run_date": "2026-03-10",
            "trust_train_id": "1234567890",
            "service_uid": "W12345",
            "stanox": "12345",
            "event_type": "D",
            "delay_mins": 8.5,
            "reason_code": "IA",
            "reason_text": "Signal failure",
            "responsible_org": "Network Rail",
            "financial_period": "2025-26 P12",
            "csv_filename": "test.csv",
        }
    ]
    with get_connection(db_path) as conn:
        count = insert_attributions(conn, rows)
        assert count == 1

        # Duplicate is ignored
        count2 = insert_attributions(conn, rows)
        assert count2 == 0


def test_query_today_observations(db_path):
    with get_connection(db_path) as conn:
        upsert_observation(conn, _obs(run_date="2026-03-15", delay=3))
        upsert_observation(conn, _obs(service_uid="W99999", run_date="2026-03-15", delay=15))
        upsert_observation(conn, _obs(run_date="2026-03-14"))  # different date

        results = query_today_observations(conn, "2026-03-15")
        assert len(results) == 2


def test_query_worst_days(db_path):
    with get_connection(db_path) as conn:
        upsert_observation(conn, _obs(run_date="2026-03-10", delay=2))
        upsert_observation(conn, _obs(run_date="2026-03-11", delay=30))
        upsert_observation(conn, _obs(run_date="2026-03-12", delay=5))

        results = query_worst_days(conn, limit=2)
        assert len(results) == 2
        assert results[0]["run_date"] == "2026-03-11"  # highest avg delay first
