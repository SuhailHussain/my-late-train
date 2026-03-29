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
    search_station,
    get_service_detail,
    extract_observation,
)

logger = logging.getLogger(__name__)


def _in_window(now: datetime, window: CommuteWindow) -> bool:
    """Return True if `now` (local naive or UTC) falls within the HH:MM window."""
    start_h, start_m = (int(x) for x in window.start.split(":"))
    end_h, end_m = (int(x) for x in window.end.split(":"))
    start_mins = start_h * 60 + start_m
    end_mins = end_h * 60 + end_m
    now_mins = now.hour * 60 + now.minute
    return start_mins <= now_mins <= end_mins


def _active_window(config: Config) -> bool:
    """Return True if the current local time is within any configured commute window."""
    now = datetime.now()  # local time
    for _, window in config.commute_windows.as_list():
        if _in_window(now, window):
            return True
    return False


def run_capture(config: Config, force: bool = False) -> dict:
    """Poll RTT and record observations. Returns a summary dict.

    Args:
        config: Loaded configuration.
        force: If True, skip the commute window check (useful for manual runs).
    """
    if not force and not _active_window(config):
        logger.info("Outside commute windows — nothing to capture.")
        return {"skipped": True}

    init_db(config.database_path)

    today = date.today()
    captured_at = datetime.now(timezone.utc).isoformat()

    summary = {"date": today.isoformat(), "captured": 0, "delayed": 0, "cancelled": 0, "errors": 0}

    with _make_client(config.rtt.base_url, config.rtt.username, config.rtt.password) as client:
        try:
            services = search_station(client, config.route.origin, today)
        except Exception as exc:
            logger.error("Failed to search station %s: %s", config.route.origin, exc)
            return {"error": str(exc)}

        # Filter to services that include the destination
        # (search result gives partial info; we check the destination list)
        candidate_uids: list[str] = []
        for svc in services:
            uid = svc.get("serviceUid") or svc.get("trainIdentity", "")
            if not uid:
                continue

            # If specific UIDs are configured, filter to those only
            if config.route.service_uids and uid not in config.route.service_uids:
                continue

            # Check destination is in the calling points summary
            dest_crs = config.route.destination.upper()
            destination_list = [
                (loc.get("crs") or "").upper()
                for loc in (svc.get("locationDetail", {}).get("subsequentLocations") or [])
            ]
            # Also check origin itself is the departure point
            origin_crs = (
                svc.get("locationDetail", {}).get("crs") or ""
            ).upper()

            if origin_crs != config.route.origin.upper():
                continue  # doesn't depart from our origin

            if dest_crs not in destination_list and not config.route.service_uids:
                continue  # doesn't call at destination (skip if not pinned)

            candidate_uids.append(uid)

        if not candidate_uids:
            logger.info("No candidate services found for %s→%s on %s",
                        config.route.origin, config.route.destination, today)
            return summary

        logger.info("Found %d candidate services for %s→%s",
                    len(candidate_uids), config.route.origin, config.route.destination)

        with get_connection(config.database_path) as conn:
            for uid in candidate_uids:
                try:
                    detail = get_service_detail(client, uid, today)
                except Exception as exc:
                    logger.warning("Failed to fetch detail for %s: %s", uid, exc)
                    summary["errors"] += 1
                    continue

                obs = extract_observation(
                    detail, config.route.origin, config.route.destination, today, captured_at
                )
                if obs is None:
                    logger.debug("Service %s does not serve %s→%s — skipping",
                                 uid, config.route.origin, config.route.destination)
                    continue

                # Upsert the service metadata
                upsert_service(conn, {
                    "service_uid": uid,
                    "run_date": today.isoformat(),
                    "origin": config.route.origin,
                    "destination": config.route.destination,
                    "operator_code": detail.get("atocCode") or detail.get("trainOperatorCode"),
                    "operator_name": detail.get("atocName") or detail.get("trainOperatorName"),
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
