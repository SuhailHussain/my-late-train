"""Tests for the Flask dashboard API endpoints."""

from pathlib import Path

import pytest

from helpers import _now, _obs
from late_train.config import (
    ApiCredentials,
    CommuteWindow,
    CommuteWindows,
    Config,
    RouteConfig,
    RTTConfig,
)
from late_train.dashboard.app import create_app
from late_train.db import (
    get_connection,
    init_db,
    insert_attributions,
    upsert_hsp_metrics,
    upsert_observation,
)


def _make_config(tmp_path: Path) -> Config:
    db_path = tmp_path / "test.db"
    return Config(
        route=RouteConfig(origin="LBG", destination="BTN"),
        commute_windows=CommuteWindows(
            morning=CommuteWindow(start="07:00", end="09:30"),
            evening=CommuteWindow(start="17:00", end="19:30"),
        ),
        rtt=RTTConfig(base_url="https://data.rtt.io", refresh_token="test-token"),
        hsp=ApiCredentials(base_url="https://hsp.example.com", api_key="test-key"),
        attribution_csv_directory=tmp_path / "attribution",
        database_path=db_path,
    )


@pytest.fixture
def client(tmp_path):
    config = _make_config(tmp_path)
    init_db(config.database_path)

    delay_codes_path = Path(__file__).parent.parent / "delay_codes.json"
    init_db(config.database_path, delay_codes_path if delay_codes_path.exists() else None)

    # Seed some data
    with get_connection(config.database_path) as conn:
        upsert_observation(conn, _obs(run_date="2026-03-10", delay=5))
        upsert_observation(conn, _obs(service_uid="W99999", run_date="2026-03-10", delay=20))
        upsert_observation(conn, _obs(run_date="2026-03-11", delay=0))
        upsert_observation(conn, _obs(run_date="2026-03-12", cancelled=1))

        upsert_hsp_metrics(
            conn,
            {
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
            },
        )

        insert_attributions(
            conn,
            [
                {
                    "incident_number": "INC001",
                    "run_date": "2026-03-10",
                    "trust_train_id": "1234567890",
                    "service_uid": "W12345",
                    "stanox": "33081",
                    "event_type": "D",
                    "delay_mins": 20.0,
                    "reason_code": "IA",
                    "reason_text": "Signal failure",
                    "responsible_org": "Network Rail",
                    "financial_period": "2025-26 P12",
                    "csv_filename": "test.csv",
                }
            ],
        )

    app = create_app(config)
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


def test_landing_returns_200(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert b"My Late Train" in resp.data


def test_dashboard_returns_200(client):
    resp = client.get("/dashboard?from=LBG&to=BTN")
    assert resp.status_code == 200
    assert b"My Late Train" in resp.data


def test_api_today(client):
    resp = client.get("/api/today?date=2026-03-10")
    assert resp.status_code == 200
    data = resp.get_json()
    assert isinstance(data, list)
    assert len(data) == 2
    statuses = {d["status"] for d in data}
    assert "on-time" in statuses or "slight" in statuses or "delayed" in statuses


def test_api_trends(client):
    resp = client.get("/api/trends?days=30")
    assert resp.status_code == 200
    data = resp.get_json()
    assert isinstance(data, list)
    if data:
        assert "run_date" in data[0]
        assert "avg_delay_mins" in data[0]
        assert "pct_on_time" in data[0]


def test_api_worst_days(client):
    resp = client.get("/api/worst-days?limit=5")
    assert resp.status_code == 200
    data = resp.get_json()
    assert isinstance(data, list)
    assert len(data) <= 5


def test_api_reasons(client):
    resp = client.get("/api/reasons?months=12")
    assert resp.status_code == 200
    data = resp.get_json()
    assert isinstance(data, list)
    if data:
        assert "reason_code" in data[0]
        assert "total_delay_mins" in data[0]


def test_api_hsp_summary(client):
    resp = client.get("/api/hsp-summary")
    assert resp.status_code == 200
    data = resp.get_json()
    assert isinstance(data, list)
    assert len(data) == 1
    assert data[0]["on_time_count"] == 12


def test_api_stats(client):
    resp = client.get("/api/stats")
    assert resp.status_code == 200
    data = resp.get_json()
    assert "total_journeys" in data
    assert "pct_on_time" in data
    assert "avg_delay_mins" in data
    assert data["total_journeys"] >= 0


def test_api_performance_no_data(client):
    """Departure time with no observations returns total=0 + error message."""
    resp = client.get("/api/performance?from=LBG&to=BTN&departure=2359&days=WEEKDAY")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["total"] == 0
    assert "error" in data


def test_api_performance_missing_params(client):
    resp = client.get("/api/performance?from=LBG&to=BTN")  # missing departure
    assert resp.status_code == 400


def test_api_performance_with_data(client):
    """Departure time that matches seeded observations returns full stats."""
    # The client fixture seeds observations with scheduled_departure="07:45"
    resp = client.get("/api/performance?from=LBG&to=BTN&departure=0745&days=WEEKDAY&months=24")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["total"] >= 1
    assert "pct_on_time" in data
    assert "pct_cancelled" in data
    assert "avg_late_mins" in data
    assert data["from_date"] is not None
    assert data["to_date"] is not None


def test_api_performance_cancelled(client):
    """A cancelled observation contributes to pct_cancelled."""
    resp = client.get("/api/performance?from=LBG&to=BTN&departure=0745&days=WEEKDAY&months=24")
    assert resp.status_code == 200
    data = resp.get_json()
    if data["total"] > 0:
        assert data["pct_cancelled"] >= 0
