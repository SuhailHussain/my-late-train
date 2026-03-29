"""Historical Service Performance (HSP) API client.

API base: https://hsp-prod.rockshore.net/api/v1/
Authentication: HTTP Basic (National Rail Data Portal / Rail Data Marketplace credentials).
Rate limit: ~1,000 requests/hour.

Two endpoints:
  - serviceMetrics: aggregate tolerance-based stats for a route/time/period
  - serviceDetails: per-service actual times for a given RID

Delay minutes are NOT returned directly — computed from gbtt_ptd vs actual_td.
"""
from __future__ import annotations

import logging
import time
from datetime import date, datetime, timezone

import httpx

from late_train.rtt import compute_delay, parse_rtt_time

logger = logging.getLogger(__name__)

_MAX_RETRIES = 3
_RETRY_DELAYS = [2, 4, 8]


def _make_client(base_url: str, username: str, password: str) -> httpx.Client:
    return httpx.Client(
        base_url=base_url,
        auth=(username, password),
        timeout=60.0,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )


def _post(client: httpx.Client, path: str, payload: dict) -> dict:
    """POST with retry logic."""
    for attempt, delay in enumerate(_RETRY_DELAYS + [None], start=1):
        try:
            resp = client.post(path, json=payload)
        except (httpx.TimeoutException, httpx.ConnectError) as exc:
            if delay is None:
                raise
            logger.warning("HSP request attempt %d failed (%s), retrying in %ds", attempt, exc, delay)
            time.sleep(delay)
            continue

        if resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", "60"))
            logger.warning("HSP rate limited, sleeping %ds", retry_after)
            time.sleep(retry_after)
            continue

        if resp.status_code >= 500:
            if delay is None:
                resp.raise_for_status()
            logger.warning("HSP server error %d on attempt %d", resp.status_code, attempt)
            time.sleep(delay)
            continue

        resp.raise_for_status()
        return resp.json()

    raise RuntimeError("HSP request failed after all retries")


def _parse_tolerance_buckets(services: list[dict]) -> dict:
    """Aggregate tolerance bucket counts across all services in the response.

    HSP serviceMetrics returns a list of services, each with a `metrics` array.
    Each metrics entry has a `tolerance_value` (0, 1, 5, 10, 15, 20, 30) and
    `num_not_on_time` / `num_on_time` counts.
    """
    totals = {
        "total_services": 0,
        "on_time_count": 0,
        "late_1_5_count": 0,
        "late_5_10_count": 0,
        "late_10_15_count": 0,
        "late_15_20_count": 0,
        "late_20_30_count": 0,
        "late_30_plus_count": 0,
        "cancel_count": 0,
    }

    for svc in services:
        metrics = svc.get("metrics") or []
        for m in metrics:
            tol = m.get("tolerance_value", 0)
            num_on_time = m.get("num_on_time", 0)
            num_not = m.get("num_not_on_time", 0)
            total = num_on_time + num_not

            if tol == 0:
                totals["total_services"] += total
                totals["on_time_count"] += num_on_time
            elif tol == 1:
                totals["late_1_5_count"] += num_on_time
            elif tol == 5:
                totals["late_5_10_count"] += num_on_time
            elif tol == 10:
                totals["late_10_15_count"] += num_on_time
            elif tol == 15:
                totals["late_15_20_count"] += num_on_time
            elif tol == 20:
                totals["late_20_30_count"] += num_on_time
            elif tol == 30:
                totals["late_30_plus_count"] += num_on_time

        if svc.get("serviceAttributesMetrics", {}).get("cancelled", False):
            totals["cancel_count"] += 1

    return totals


def get_service_metrics(
    client: httpx.Client,
    origin: str,
    destination: str,
    from_time: str,  # HHMM
    to_time: str,    # HHMM
    from_date: str,  # YYYY-MM-DD
    to_date: str,    # YYYY-MM-DD
    days: str = "WEEKDAY",
) -> tuple[dict, list[str]]:
    """Query serviceMetrics endpoint.

    Returns (aggregated_buckets_dict, list_of_rids).
    """
    payload = {
        "from_loc": origin.upper(),
        "to_loc": destination.upper(),
        "from_time": from_time.replace(":", ""),
        "to_time": to_time.replace(":", ""),
        "from_date": from_date,
        "to_date": to_date,
        "days": days,
    }

    logger.debug("HSP serviceMetrics: %s", payload)
    data = _post(client, "/serviceMetrics", payload)

    services = data.get("Services") or []
    rids = [
        svc.get("serviceAttributesMetrics", {}).get("rid")
        for svc in services
        if svc.get("serviceAttributesMetrics", {}).get("rid")
    ]
    buckets = _parse_tolerance_buckets(services)
    return buckets, rids


def get_service_details(
    client: httpx.Client,
    rid: str,
    from_loc: str,
    to_loc: str,
) -> list[dict]:
    """Query serviceDetails for a single RID. Returns list of calling point dicts."""
    payload = {
        "rid": rid,
        "from_loc": from_loc.upper(),
        "to_loc": to_loc.upper(),
    }
    data = _post(client, "/serviceDetails", payload)
    return data.get("serviceAttributesDetails", {}).get("locations") or []


def locations_to_observation(
    rid: str,
    locations: list[dict],
    origin: str,
    destination: str,
    captured_at: str,
) -> dict | None:
    """Convert HSP serviceDetails locations into an observation row."""
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

    # HSP uses "gbtt_ptd" (public timetable departure) and "actual_td" (actual departure)
    sched_dep = parse_rtt_time(origin_loc.get("gbtt_ptd"))
    actual_dep = parse_rtt_time(origin_loc.get("actual_td"))
    sched_arr = parse_rtt_time(dest_loc.get("gbtt_pta"))
    actual_arr = parse_rtt_time(dest_loc.get("actual_ta"))

    # HSP date is embedded in RID: first 8 chars are YYYYMMDD
    run_date = f"{rid[:4]}-{rid[4:6]}-{rid[6:8]}" if len(rid) >= 8 else None
    if run_date is None:
        return None

    delay = compute_delay(sched_arr, actual_arr)
    if delay is None:
        delay = compute_delay(sched_dep, actual_dep)

    # Cancelled = no actual times at destination
    cancelled = actual_arr is None and actual_dep is None and sched_arr is not None

    late_canc_reason = dest_loc.get("late_canc_reason") or origin_loc.get("late_canc_reason")

    if sched_dep is None and sched_arr is None:
        return None

    return {
        "service_uid": rid[8:] if len(rid) > 8 else rid,  # HSP RID embeds UID after date
        "run_date": run_date,
        "scheduled_departure": sched_dep or sched_arr or "",
        "actual_departure": actual_dep,
        "scheduled_arrival": sched_arr or sched_dep or "",
        "actual_arrival": actual_arr,
        "delay_mins": delay,
        "platform": None,
        "platform_changed": 0,
        "cancelled": int(cancelled),
        "cancel_reason_code": str(late_canc_reason) if late_canc_reason else None,
        "cancel_reason_text": None,
        "is_actual": int(actual_arr is not None or actual_dep is not None),
        "source": "hsp",
        "captured_at": captured_at,
    }
