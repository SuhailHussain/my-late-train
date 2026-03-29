"""Tests for NR attribution CSV parser."""
from pathlib import Path

import pytest

from late_train.attribution import parse_attribution_csv, _already_ingested
from late_train.db import init_db, get_connection, insert_attributions

FIXTURES = Path(__file__).parent / "fixtures"


def test_parse_attribution_csv_all_rows():
    rows = parse_attribution_csv(FIXTURES / "attribution_sample.csv")
    assert len(rows) == 5

    first = rows[0]
    assert first["incident_number"] == "INC001"
    assert first["run_date"] == "2026-03-10"
    assert first["stanox"] == "33081"
    assert first["delay_mins"] == 8.5
    assert first["reason_code"] == "IA"
    assert first["reason_text"] == "Signal failure"
    assert first["responsible_org"] == "Network Rail"
    assert first["csv_filename"] == "attribution_sample.csv"


def test_parse_attribution_csv_stanox_filter():
    # Only STANOX 33081 (London Bridge) — rows 0, 2, 4 match
    rows = parse_attribution_csv(FIXTURES / "attribution_sample.csv", filter_stanox={"33081"})
    assert len(rows) == 3
    assert all(r["stanox"] == "33081" for r in rows)


def test_parse_attribution_csv_no_match_stanox():
    rows = parse_attribution_csv(FIXTURES / "attribution_sample.csv", filter_stanox={"00000"})
    assert rows == []


def test_parse_attribution_csv_missing_file():
    rows = parse_attribution_csv(FIXTURES / "nonexistent.csv")
    assert rows == []


def test_already_ingested_false(tmp_path):
    db = tmp_path / "test.db"
    init_db(db)
    with get_connection(db) as conn:
        assert not _already_ingested(conn, "test.csv")


def test_already_ingested_true(tmp_path):
    db = tmp_path / "test.db"
    init_db(db)
    row = {
        "incident_number": "INC001",
        "run_date": "2026-03-10",
        "trust_train_id": "1234567890",
        "service_uid": None,
        "stanox": "33081",
        "event_type": "D",
        "delay_mins": 8.5,
        "reason_code": "IA",
        "reason_text": "Signal failure",
        "responsible_org": "Network Rail",
        "financial_period": "2025-26 P12",
        "csv_filename": "test.csv",
    }
    with get_connection(db) as conn:
        insert_attributions(conn, [row])
        assert _already_ingested(conn, "test.csv")
