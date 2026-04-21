"""Shared test helpers."""
from datetime import datetime, timezone


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _obs(service_uid="W12345", run_date="2026-03-10", source="rtt", delay=5, cancelled=0):
    return {
        "service_uid": service_uid,
        "run_date": run_date,
        "scheduled_departure": "07:45",
        "actual_departure": "07:50" if not cancelled else None,
        "scheduled_arrival": "09:00",
        "actual_arrival": "09:05" if not cancelled else None,
        "delay_mins": delay if not cancelled else None,
        "platform": "1",
        "platform_changed": 0,
        "cancelled": cancelled,
        "cancel_reason_code": "IA" if cancelled else None,
        "cancel_reason_text": "Signal failure" if cancelled else None,
        "is_actual": 1,
        "source": source,
        "captured_at": _now(),
    }
