"""Tests for HSP API client — tolerance parsing and observation conversion."""
import json
from pathlib import Path

import pytest

from late_train.hsp import _parse_tolerance_buckets, locations_to_observation

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def test_parse_tolerance_buckets():
    data = _load("hsp_metrics.json")
    services = data["Services"]
    buckets = _parse_tolerance_buckets(services)

    assert buckets["total_services"] == 20
    assert buckets["on_time_count"] == 12
    assert buckets["late_1_5_count"] == 3
    assert buckets["late_5_10_count"] == 2
    assert buckets["late_10_15_count"] == 1
    assert buckets["late_15_20_count"] == 1
    assert buckets["late_20_30_count"] == 0
    assert buckets["late_30_plus_count"] == 0
    assert buckets["cancel_count"] == 0


def test_locations_to_observation_delayed():
    data = _load("hsp_details.json")
    locations = data["serviceAttributesDetails"]["locations"]
    rid = "20260101W12345"

    obs = locations_to_observation(rid, locations, "LBG", "BTN", "2026-01-01T10:00:00Z")

    assert obs is not None
    assert obs["run_date"] == "2026-01-01"
    assert obs["scheduled_departure"] == "07:45"
    assert obs["actual_departure"] == "07:52"
    assert obs["scheduled_arrival"] == "09:00"
    assert obs["actual_arrival"] == "09:12"
    assert obs["delay_mins"] == 12
    assert obs["cancelled"] == 0
    assert obs["is_actual"] == 1
    assert obs["source"] == "hsp"
    assert obs["cancel_reason_code"] == "IA"


def test_locations_to_observation_missing_destination():
    data = _load("hsp_details.json")
    locations = data["serviceAttributesDetails"]["locations"]
    obs = locations_to_observation("20260101W12345", locations, "LBG", "VIC", "now")
    assert obs is None


def test_locations_to_observation_short_rid():
    """A RID shorter than 8 chars should return None (can't extract date)."""
    obs = locations_to_observation("ABC", [], "LBG", "BTN", "now")
    assert obs is None
