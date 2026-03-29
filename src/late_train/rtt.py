"""Realtime Trains API client.

RTT API docs: https://www.realtimetrains.co.uk/about/developer/pull/docs/

Authentication: HTTP Basic (username/password from api.rtt.io registration).
Time format: "0723" or "0723H" — the H suffix means 30 seconds past the minute.
"""
from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

_MAX_RETRIES = 3
_RETRY_DELAYS = [1, 2, 4]  # seconds


def _make_client(base_url: str, username: str, password: str) -> httpx.Client:
    return httpx.Client(
        base_url=base_url,
        auth=(username, password),
        timeout=30.0,
        headers={"Accept": "application/json"},
    )


def _request(client: httpx.Client, method: str, path: str, **kwargs) -> dict:
    """Make an HTTP request with retry logic for transient errors."""
    for attempt, delay in enumerate(_RETRY_DELAYS + [None], start=1):
        try:
            resp = client.request(method, path, **kwargs)
        except (httpx.TimeoutException, httpx.ConnectError) as exc:
            if delay is None:
                raise
            logger.warning("RTT request attempt %d failed (%s), retrying in %ds", attempt, exc, delay)
            time.sleep(delay)
            continue

        if resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", "60"))
            logger.warning("RTT rate limited, sleeping %ds", retry_after)
            time.sleep(retry_after)
            continue

        if resp.status_code >= 500:
            if delay is None:
                resp.raise_for_status()
            logger.warning("RTT server error %d on attempt %d, retrying", resp.status_code, attempt)
            time.sleep(delay)
            continue

        resp.raise_for_status()
        return resp.json()

    # Should not reach here
    raise RuntimeError("RTT request failed after all retries")


def parse_rtt_time(raw: Optional[str]) -> Optional[str]:
    """Convert RTT time string to HH:MM.

    RTT times are four digits optionally followed by 'H' (meaning :30s past).
    Examples: "0723" → "07:23", "0723H" → "07:23", None → None.
    """
    if not raw:
        return None
    digits = raw.rstrip("H").strip()
    if len(digits) != 4:
        return None
    return f"{digits[:2]}:{digits[2:]}"


def _time_to_minutes(hhmm: Optional[str]) -> Optional[int]:
    """Convert HH:MM to minutes since midnight."""
    if not hhmm:
        return None
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def compute_delay(scheduled: Optional[str], actual: Optional[str]) -> Optional[int]:
    """Return delay in minutes (positive = late). Handles overnight wrap-around."""
    s = _time_to_minutes(scheduled)
    a = _time_to_minutes(actual)
    if s is None or a is None:
        return None
    diff = a - s
    # Handle midnight wrap (e.g. scheduled 23:55, actual 00:03)
    if diff < -120:
        diff += 24 * 60
    if diff > 120:
        diff -= 24 * 60
    return diff


def search_station(client: httpx.Client, station: str, run_date: date) -> list[dict]:
    """Return all services departing from station on a given date.

    GET /search/{station}/{YYYY}/{MM}/{DD}
    """
    path = f"/search/{station.upper()}/{run_date.year}/{run_date.month:02d}/{run_date.day:02d}"
    data = _request(client, "GET", path)
    services = data.get("services") or []
    # Filter to passenger services only
    return [s for s in services if s.get("isPassenger", True)]


def get_service_detail(client: httpx.Client, service_uid: str, run_date: date) -> dict:
    """Return full journey detail for a specific service UID on a given date.

    GET /service/{serviceUid}/{YYYY}/{MM}/{DD}
    """
    path = f"/service/{service_uid}/{run_date.year}/{run_date.month:02d}/{run_date.day:02d}"
    return _request(client, "GET", path)


def extract_observation(
    service: dict,
    origin: str,
    destination: str,
    run_date: date,
    captured_at: str,
) -> Optional[dict]:
    """Extract a normalised observation row from a service detail response.

    Returns None if the service does not call at both origin and destination.
    """
    locations = service.get("locations") or []

    origin_loc = None
    dest_loc = None

    for loc in locations:
        crs = (loc.get("crs") or "").upper()
        if crs == origin.upper() and origin_loc is None:
            origin_loc = loc
        if crs == destination.upper() and dest_loc is None:
            dest_loc = loc

    if origin_loc is None or dest_loc is None:
        return None

    service_uid = service.get("serviceUid") or service.get("trainIdentity", "")

    # Departure from origin
    sched_dep = parse_rtt_time(
        origin_loc.get("gbttBookedDeparture") or origin_loc.get("gbttBookedArrival")
    )
    actual_dep_raw = origin_loc.get("realtimeDeparture") or origin_loc.get("realtimeArrival")
    actual_dep = parse_rtt_time(actual_dep_raw)
    dep_is_actual = bool(
        origin_loc.get("realtimeDepartureActual") or origin_loc.get("realtimeArrivalActual")
    )

    # Arrival at destination
    sched_arr = parse_rtt_time(
        dest_loc.get("gbttBookedArrival") or dest_loc.get("gbttBookedDeparture")
    )
    actual_arr_raw = dest_loc.get("realtimeArrival") or dest_loc.get("realtimeDeparture")
    actual_arr = parse_rtt_time(actual_arr_raw)
    arr_is_actual = bool(
        dest_loc.get("realtimeArrivalActual") or dest_loc.get("realtimeDepartureActual")
    )

    # Use arrival delay at destination as the primary delay metric
    delay = compute_delay(sched_arr, actual_arr)
    if delay is None:
        delay = compute_delay(sched_dep, actual_dep)

    # Cancellation — check destination call and service-level fields
    cancelled = (
        dest_loc.get("displayAs") == "CANCELLED_CALL"
        or origin_loc.get("displayAs") == "CANCELLED_CALL"
        or service.get("serviceType") == "cancelled"
    )

    cancel_code = (
        dest_loc.get("cancelReasonCode")
        or origin_loc.get("cancelReasonCode")
        or service.get("cancelReasonCode")
    )
    cancel_text = (
        dest_loc.get("cancelReasonShortText")
        or origin_loc.get("cancelReasonShortText")
        or service.get("cancelReasonShortText")
    )

    # Platform
    platform = origin_loc.get("platform")
    platform_booked = origin_loc.get("platformConfirmed")
    platform_changed = bool(platform and not platform_booked and origin_loc.get("platformChanged"))

    operator = service.get("atocCode") or service.get("trainOperatorCode") or ""

    if sched_dep is None:
        logger.debug("No scheduled departure for %s on %s — skipping", service_uid, run_date)
        return None

    return {
        "service_uid": service_uid,
        "run_date": run_date.isoformat(),
        "scheduled_departure": sched_dep,
        "actual_departure": actual_dep,
        "scheduled_arrival": sched_arr or sched_dep,
        "actual_arrival": actual_arr,
        "delay_mins": delay,
        "platform": platform,
        "platform_changed": int(platform_changed),
        "cancelled": int(cancelled),
        "cancel_reason_code": cancel_code,
        "cancel_reason_text": cancel_text,
        "is_actual": int(arr_is_actual or dep_is_actual),
        "source": "rtt",
        "captured_at": captured_at,
    }
