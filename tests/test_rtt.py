"""Tests for RTT API client — parsing helpers and observation extraction."""
import json
from datetime import date
from pathlib import Path

import pytest

from late_train.rtt import (
    parse_rtt_time,
    compute_delay,
    _get_crs,
    _iso_to_hhmm,
    extract_observation,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


# ---------------------------------------------------------------------------
# parse_rtt_time — kept for HSP client backwards compat
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("0723", "07:23"),
    ("0723H", "07:23"),
    ("0000", "00:00"),
    ("2359", "23:59"),
    ("2359H", "23:59"),
    (None, None),
    ("", None),
])
def test_parse_rtt_time(raw, expected):
    assert parse_rtt_time(raw) == expected


# ---------------------------------------------------------------------------
# compute_delay — kept for HSP client
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("scheduled,actual,expected", [
    ("09:00", "09:12", 12),
    ("09:00", "09:00", 0),
    ("09:00", "08:55", -5),
    ("23:55", "00:03", 8),    # overnight wrap
    ("00:05", "23:58", -7),
    (None, "09:12", None),
    ("09:00", None, None),
])
def test_compute_delay(scheduled, actual, expected):
    assert compute_delay(scheduled, actual) == expected


# ---------------------------------------------------------------------------
# New API helpers
# ---------------------------------------------------------------------------

def test_get_crs():
    loc = {"shortCodes": ["LBG"], "longCodes": ["LNDNBDG"], "description": "London Bridge"}
    assert _get_crs(loc) == "LBG"


def test_get_crs_empty():
    assert _get_crs({}) == ""
    assert _get_crs({"shortCodes": []}) == ""


@pytest.mark.parametrize("raw,expected", [
    ("2026-03-27T07:45:00", "07:45"),
    ("2026-03-27T07:45:00Z", "07:45"),
    ("2026-03-27T09:12:00+00:00", "09:12"),
    (None, None),
    ("", None),
])
def test_iso_to_hhmm(raw, expected):
    assert _iso_to_hhmm(raw) == expected


# ---------------------------------------------------------------------------
# extract_observation
# ---------------------------------------------------------------------------

def test_extract_observation_delayed():
    service = _load("rtt_service_delayed.json")
    obs = extract_observation(service, "LBG", "BTN", date(2026, 3, 27), "2026-03-27T08:00:00Z")

    assert obs is not None
    assert obs["service_uid"] == "W12345"
    assert obs["run_date"] == "2026-03-27"
    assert obs["scheduled_departure"] == "07:45"
    assert obs["actual_departure"] == "07:52"
    assert obs["scheduled_arrival"] == "09:00"
    assert obs["actual_arrival"] == "09:12"
    assert obs["delay_mins"] == 12        # pre-computed by RTT
    assert obs["cancelled"] == 0
    assert obs["is_actual"] == 1
    assert obs["source"] == "rtt"
    assert obs["platform"] == "3"


def test_extract_observation_cancelled():
    service = _load("rtt_service_cancelled.json")
    obs = extract_observation(service, "LBG", "BTN", date(2026, 3, 27), "2026-03-27T08:00:00Z")

    assert obs is not None
    assert obs["cancelled"] == 1
    assert obs["cancel_reason_code"] == "IA"
    assert obs["delay_mins"] is None


def test_extract_observation_missing_destination():
    service = _load("rtt_service_delayed.json")
    assert extract_observation(service, "LBG", "VIC", date(2026, 3, 27), "now") is None


def test_extract_observation_missing_origin():
    service = _load("rtt_service_delayed.json")
    assert extract_observation(service, "EBN", "BTN", date(2026, 3, 27), "now") is None


def test_extract_observation_platform_changed():
    """Platform changed flag set when actual != planned."""
    service = _load("rtt_service_delayed.json")
    # Patch the origin location's platform
    service["service"]["locations"][0]["locationMetadata"]["platform"]["actual"] = "5"
    obs = extract_observation(service, "LBG", "BTN", date(2026, 3, 27), "now")
    assert obs is not None
    assert obs["platform"] == "5"
    assert obs["platform_changed"] == 1
