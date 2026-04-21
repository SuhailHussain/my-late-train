"""Daily capture orchestrator.

Called every 3 minutes by cron during commute windows. Each call fetches the
latest real-time data and upserts into the database. Because captures repeat,
early calls get estimated times and later calls get confirmed actuals — both
stored via INSERT OR REPLACE so we always keep the freshest data.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone

from late_train.config import Config, CommuteWindow
from late_train.db import get_connection, init_db, upsert_service, upsert_observation
from late_train.rtt import (
    _make_client,
    _get_crs,
    search_location,
    get_service_detail,
    extract_observation,
)

logger = logging.getLogger(__name__)


def _in_window(now: datetime, window: CommuteWindow) -> bool:
    start_h, start_m = (int(x) for x in window.start.split(":"))
    end_h, end_m = (int(x) for x in window.end.split(":"))
    start_mins = start_h * 60 + start_m
    end_mins = end_h * 60 + end_m
    now_mins = now.hour * 60 + now.minute
    return start_mins <= now_mins <= end_mins


def _active_window(config: Config) -> tuple[bool, CommuteWindow | None]:
    """Return (is_active, window) for the current local time."""
    now = datetime.now()
    for _, window in config.commute_windows.as_list():
        if _in_window(now, window):
            return True, window
    return False, None


def run_capture(config: Config, force: bool = False, run_date: date | None = None) -> dict:
    """Poll RTT and record observations. Returns a summary dict.

    Args:
        config: Loaded configuration.
        force: If True, skip the commute window check (useful for manual runs).
        run_date: Capture a specific date (defaults to today). When a past date
                  is given, both commute windows are fetched regardless of force flag.
    """
    historical = run_date is not None and run_date != date.today()

    if not historical:
        active, window = _active_window(config)
        if not force and not active:
            logger.info("Outside commute windows — nothing to capture.")
            return {"skipped": True}

    target_date = run_date or date.today()
    now = datetime.now()
    captured_at = datetime.now(timezone.utc).isoformat()

    init_db(config.database_path)

    summary = {"date": target_date.isoformat(), "captured": 0, "delayed": 0, "cancelled": 0, "errors": 0}

    # Build list of (time_from, time_to) windows to search
    if historical:
        windows_to_search = [w.datetimes(target_date) for _, w in config.commute_windows.as_list()]
    elif force or window is None:
        # Forced outside commute hours: 2-hour window from now
        tf = now.replace(second=0, microsecond=0)
        tt = now.replace(hour=min(now.hour + 2, 23), minute=59, second=0, microsecond=0)
        windows_to_search = [(tf, tt)]
    else:
        tf, tt = window.datetimes(target_date)
        tf = tf.replace(second=0, microsecond=0)
        tt = tt.replace(second=0, microsecond=0)
        windows_to_search = [(tf, tt)]

    with _make_client(config.rtt.base_url, config.rtt.refresh_token) as client:
        # Collect all candidate service IDs across all windows (deduplicated)
        seen_ids: set[str] = set()
        services: list[dict] = []
        for time_from, time_to in windows_to_search:
            try:
                batch = search_location(
                    client,
                    config.route.origin,
                    config.route.destination,
                    time_from,
                    time_to,
                )
                services.extend(batch)
            except Exception as exc:
                logger.error("Failed to search location %s: %s", config.route.origin, exc)
                return {"error": str(exc)}

        # Extract service identities, optionally filtering to configured UIDs (deduplicated)
        candidate_ids: list[str] = []
        for svc in services:
            meta = svc.get("scheduleMetadata") or {}
            identity = meta.get("identity", "")
            if not identity or identity in seen_ids:
                continue
            seen_ids.add(identity)
            if config.route.service_uids and identity not in config.route.service_uids:
                continue
            candidate_ids.append(identity)

        if not candidate_ids:
            logger.info("No candidate services found for %s→%s on %s",
                        config.route.origin, config.route.destination, target_date)
            return summary

        logger.info("Found %d candidate services for %s→%s",
                    len(candidate_ids), config.route.origin, config.route.destination)

        with get_connection(config.database_path) as conn:
            for identity in candidate_ids:
                try:
                    detail = get_service_detail(client, identity, target_date)
                except Exception as exc:
                    logger.warning("Failed to fetch detail for %s: %s", identity, exc)
                    summary["errors"] += 1
                    continue

                obs = extract_observation(
                    detail, config.route.origin, config.route.destination, target_date, captured_at
                )
                if obs is None:
                    logger.debug("Service %s does not serve %s→%s — skipping",
                                 identity, config.route.origin, config.route.destination)
                    continue

                # Operator info from scheduleMetadata
                svc_meta = (detail.get("service") or {}).get("scheduleMetadata") or {}
                operator = svc_meta.get("operator") or {}

                upsert_service(conn, {
                    "service_uid": identity,
                    "run_date": target_date.isoformat(),
                    "origin": config.route.origin,
                    "destination": config.route.destination,
                    "operator_code": operator.get("code"),
                    "operator_name": operator.get("name"),
                })

                upsert_observation(conn, obs)
                summary["captured"] += 1
                if obs["cancelled"]:
                    summary["cancelled"] += 1
                elif obs["delay_mins"] is not None and obs["delay_mins"] > 5:
                    summary["delayed"] += 1

    logger.info(
        "Capture complete: %d captured, %d delayed (>5 min), %d cancelled, %d errors",
        summary["captured"], summary["delayed"], summary["cancelled"], summary["errors"],
    )
    return summary
