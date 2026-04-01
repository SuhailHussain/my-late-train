"""Realtime Trains API client (next-generation API, api-portal.rtt.io).

API spec: https://realtimetrains.github.io/api-specification/
Base URL: https://data.rtt.io

Authentication: OAuth2 Bearer tokens.
  - A long-lived refresh token is exchanged for a short-lived access token
    via GET /api/get_access_token.
  - The access token is cached and refreshed automatically when it expires.

Location codes use the format "gb-nr:CRS" (e.g. "gb-nr:LBG").
Times in responses are ISO 8601 datetimes. Lateness is pre-computed in minutes.
"""
from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

_RETRY_DELAYS = [1, 2, 4]
_NAMESPACE = "gb-nr"


# ---------------------------------------------------------------------------
# Auth — Bearer token with automatic refresh
# ---------------------------------------------------------------------------

class _RTTAuth(httpx.Auth):
    """httpx Auth handler that transparently refreshes the short-lived access token."""

    def __init__(self, base_url: str, refresh_token: str) -> None:
        self._base_url = base_url
        self._refresh_token = refresh_token
        self._access_token: str | None = None
        self._valid_until: datetime | None = None

    def _needs_refresh(self) -> bool:
        if not self._access_token or not self._valid_until:
            return True
        return datetime.now(timezone.utc) >= self._valid_until - timedelta(seconds=60)

    def _do_refresh(self) -> None:
        logger.debug("Refreshing RTT access token")
        resp = httpx.get(
            f"{self._base_url}/api/get_access_token",
            headers={"Authorization": f"Bearer {self._refresh_token}"},
            timeout=30.0,
        )
        resp.raise_for_status()
        data = resp.json()
        self._access_token = data["token"]
        self._valid_until = datetime.fromisoformat(
            data["validUntil"].replace("Z", "+00:00")
        )
        logger.debug("RTT access token valid until %s", self._valid_until.isoformat())

    def auth_flow(self, request: httpx.Request):
        if self._needs_refresh():
            self._do_refresh()
        request.headers["Authorization"] = f"Bearer {self._access_token}"
        yield request


def _make_client(base_url: str, refresh_token: str) -> httpx.Client:
    return httpx.Client(
        base_url=base_url,
        auth=_RTTAuth(base_url, refresh_token),
        timeout=30.0,
        headers={"Accept": "application/json"},
    )


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _request(client: httpx.Client, method: str, path: str, **kwargs) -> dict:
    """Make a request with retry logic for transient errors."""
    for attempt, delay in enumerate(_RETRY_DELAYS + [None], start=1):
        try:
            resp = client.request(method, path, **kwargs)
        except (httpx.TimeoutException, httpx.ConnectError) as exc:
            if delay is None:
                raise
            logger.warning("RTT attempt %d failed (%s), retrying in %ds", attempt, exc, delay)
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
        if resp.status_code == 204:
            return {}
        return resp.json()

    raise RuntimeError("RTT request failed after all retries")


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def _get_crs(location: dict) -> str:
    """Extract CRS code from a GeographicLocation dict (shortCodes field)."""
    codes = location.get("shortCodes") or []
    return codes[0].upper() if codes else ""


def _iso_to_hhmm(dt_str: Optional[str]) -> Optional[str]:
    """Convert ISO 8601 datetime string to HH:MM."""
    if not dt_str:
        return None
    try:
        dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        return dt.strftime("%H:%M")
    except (ValueError, AttributeError):
        return None


# Kept for HSP client which still uses the old 4-digit time format
def parse_rtt_time(raw: Optional[str]) -> Optional[str]:
    """Convert old-style RTT time string ('0723' or '0723H') to HH:MM.
    Used by the HSP client which returns times in this format.
    """
    if not raw:
        return None
    digits = raw.rstrip("H").strip()
    if len(digits) != 4:
        return None
    return f"{digits[:2]}:{digits[2:]}"


def compute_delay(scheduled: Optional[str], actual: Optional[str]) -> Optional[int]:
    """Compute delay in minutes from HH:MM strings. Handles overnight wrap.
    Used by the HSP client.
    """
    def to_mins(hhmm: str) -> int:
        h, m = hhmm.split(":")
        return int(h) * 60 + int(m)

    if not scheduled or not actual:
        return None
    diff = to_mins(actual) - to_mins(scheduled)
    if diff < -120:
        diff += 24 * 60
    if diff > 120:
        diff -= 24 * 60
    return diff


# ---------------------------------------------------------------------------
# API calls
# ---------------------------------------------------------------------------

def search_location(
    client: httpx.Client,
    station: str,
    destination: str,
    time_from: datetime,
    time_to: datetime,
) -> list[dict]:
    """Return services departing from station towards destination in the given window.

    GET /rtt/location?code=gb-nr:LBG&filterTo=gb-nr:BTN&timeFrom=...&timeTo=...
    """
    params = {
        "code": f"{_NAMESPACE}:{station.upper()}",
        "filterTo": f"{_NAMESPACE}:{destination.upper()}",
        "timeFrom": time_from.strftime("%Y-%m-%dT%H:%M:%S"),
        "timeTo": time_to.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    data = _request(client, "GET", "/rtt/location", params=params)
    services = data.get("services") or []
    return [
        s for s in services
        if s.get("scheduleMetadata", {}).get("inPassengerService", True)
    ]


def get_service_detail(
    client: httpx.Client,
    identity: str,
    run_date: date,
    namespace: str = _NAMESPACE,
) -> dict:
    """Return full journey detail for a service identity on a given date.

    GET /rtt/service?namespace=gb-nr&identity=W12345&departureDate=2026-04-01
    """
    params = {
        "namespace": namespace,
        "identity": identity,
        "departureDate": run_date.isoformat(),
    }
    return _request(client, "GET", "/rtt/service", params=params)


# ---------------------------------------------------------------------------
# Observation extraction
# ---------------------------------------------------------------------------

def extract_observation(
    service_response: dict,
    origin: str,
    destination: str,
    run_date: date,
    captured_at: str,
) -> Optional[dict]:
    """Extract a normalised observation row from a /rtt/service response.

    Returns None if the service doesn't call at both origin and destination.
    """
    service = service_response.get("service") or service_response
    locations = service.get("locations") or []
    meta = service.get("scheduleMetadata") or {}
    identity = meta.get("identity", "")

    origin_loc = None
    dest_loc = None
    for loc in locations:
        crs = _get_crs(loc.get("location") or {})
        if crs == origin.upper() and origin_loc is None:
            origin_loc = loc
        if crs == destination.upper() and dest_loc is None:
            dest_loc = loc

    if origin_loc is None or dest_loc is None:
        return None

    # Departure from origin
    dep = (origin_loc.get("temporalData") or {}).get("departure") or {}
    sched_dep = _iso_to_hhmm(dep.get("scheduleAdvertised"))
    actual_dep = _iso_to_hhmm(dep.get("realtimeActual"))

    # Arrival at destination
    arr = (dest_loc.get("temporalData") or {}).get("arrival") or {}
    sched_arr = _iso_to_hhmm(arr.get("scheduleAdvertised"))
    actual_arr = _iso_to_hhmm(arr.get("realtimeActual"))

    # Delay — pre-computed by RTT as minutes late vs advertised schedule
    delay = arr.get("realtimeAdvertisedLateness")
    if delay is None:
        delay = dep.get("realtimeAdvertisedLateness")

    # Cancellation
    cancelled = bool(arr.get("isCancelled") or dep.get("isCancelled"))
    cancel_code = arr.get("cancellationReasonCode") or dep.get("cancellationReasonCode")

    # Platform — PlannedActualData: {planned, forecast, actual}
    platform_data = (origin_loc.get("locationMetadata") or {}).get("platform") or {}
    platform = platform_data.get("actual") or platform_data.get("planned")
    platform_changed = bool(
        platform_data.get("actual")
        and platform_data.get("planned")
        and platform_data.get("actual") != platform_data.get("planned")
    )

    if sched_dep is None and sched_arr is None:
        logger.debug("No scheduled times for %s on %s — skipping", identity, run_date)
        return None

    return {
        "service_uid": identity,
        "run_date": run_date.isoformat(),
        "scheduled_departure": sched_dep or "",
        "actual_departure": actual_dep,
        "scheduled_arrival": sched_arr or sched_dep or "",
        "actual_arrival": actual_arr,
        "delay_mins": delay,
        "platform": platform,
        "platform_changed": int(platform_changed),
        "cancelled": int(cancelled),
        "cancel_reason_code": cancel_code,
        "cancel_reason_text": None,
        "is_actual": int(actual_arr is not None or actual_dep is not None),
        "source": "rtt",
        "captured_at": captured_at,
    }
