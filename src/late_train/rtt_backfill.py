"""RTT historical backfill.

Fetches past weekday journeys from the Realtime Trains API and stores them in
daily_observations. Intended to seed historical data before the daily capture
has had time to accumulate enough records.

Usage:
    python -m late_train rtt-backfill              # last 1 week  (~70 API calls)
    python -m late_train rtt-backfill --weeks=12   # ~3 months    (~840 API calls)
    python -m late_train rtt-backfill --dry-run    # estimate only, no API calls

Rate budget:
    Per day: 2 commute windows × (1 search + ~6 detail calls) ≈ 14 calls
    Per week (5 weekdays): ~70 calls
    12 weeks: ~840 calls — within the ~1000/day RTT free-tier limit.
"""
from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta, timezone

from late_train.config import Config
from late_train.db import get_connection, init_db, upsert_observation
from late_train.rtt import (
    _make_client,
    extract_observation,
    get_service_detail,
    search_location,
)

logger = logging.getLogger(__name__)

_MAX_WEEKS = 12
_MIN_ACTUALS_TO_SKIP = 2  # skip date if it already has this many RTT actuals


def _weekdays_in_range(weeks_back: int) -> list[date]:
    """Return past weekdays (Mon–Fri) in reverse chronological order."""
    today = date.today()
    days = []
    for offset in range(1, weeks_back * 7 + 1):
        candidate = today - timedelta(days=offset)
        if candidate.weekday() < 5:  # 0=Mon … 4=Fri
            days.append(candidate)
    return days


def _already_captured(conn, target_date: date) -> bool:
    count = conn.execute(
        "SELECT COUNT(*) FROM daily_observations "
        "WHERE run_date = ? AND source = 'rtt' AND is_actual = 1",
        (target_date.isoformat(),),
    ).fetchone()[0]
    return count >= _MIN_ACTUALS_TO_SKIP


def run_rtt_backfill(
    config: Config,
    weeks_back: int = 1,
    delay_secs: float = 0.5,
) -> dict:
    """Fetch RTT data for past weekdays and store in daily_observations.

    Returns a summary dict.
    """
    weeks_back = min(weeks_back, _MAX_WEEKS)
    init_db(config.database_path)
    captured_at = datetime.now(timezone.utc).isoformat()

    summary = {
        "weeks_back": weeks_back,
        "dates_processed": 0,
        "dates_skipped": 0,
        "observations_upserted": 0,
        "api_calls": 0,
        "errors": 0,
    }

    target_dates = _weekdays_in_range(weeks_back)
    logger.info(
        "RTT backfill: %d weekdays over %d week(s)", len(target_dates), weeks_back
    )

    with _make_client(config.rtt.base_url, config.rtt.refresh_token) as client:
        for target_date in target_dates:
            with get_connection(config.database_path) as conn:
                if _already_captured(conn, target_date):
                    logger.debug("Skipping %s — already has RTT actuals", target_date)
                    summary["dates_skipped"] += 1
                    continue

            logger.info("Backfilling %s", target_date)

            seen_uids: set[str] = set()

            for window_name, window in config.commute_windows.as_list():
                time_from, time_to = window.datetimes(target_date)

                try:
                    services = search_location(
                        client,
                        config.route.origin,
                        config.route.destination,
                        time_from,
                        time_to,
                    )
                    summary["api_calls"] += 1
                except Exception as exc:
                    logger.error(
                        "search_location failed for %s %s window: %s",
                        target_date, window_name, exc,
                    )
                    summary["errors"] += 1
                    continue

                logger.debug(
                    "%s %s window: %d services found",
                    target_date, window_name, len(services),
                )

                for svc in services:
                    meta = svc.get("scheduleMetadata") or {}
                    uid = meta.get("identity") or svc.get("serviceUid") or ""
                    if not uid or uid in seen_uids:
                        continue

                    if delay_secs > 0:
                        time.sleep(delay_secs)

                    try:
                        detail = get_service_detail(client, uid, target_date)
                        summary["api_calls"] += 1
                    except Exception as exc:
                        logger.warning(
                            "get_service_detail failed for %s on %s: %s",
                            uid, target_date, exc,
                        )
                        summary["errors"] += 1
                        continue

                    obs = extract_observation(
                        detail,
                        config.route.origin,
                        config.route.destination,
                        target_date,
                        captured_at,
                    )
                    if obs is None:
                        continue

                    seen_uids.add(uid)

                    try:
                        with get_connection(config.database_path) as conn:
                            upsert_observation(conn, obs)
                        summary["observations_upserted"] += 1
                    except Exception as exc:
                        logger.warning("Failed to upsert observation: %s", exc)
                        summary["errors"] += 1

            summary["dates_processed"] += 1

    logger.info(
        "RTT backfill complete: %d dates processed, %d skipped, "
        "%d observations, %d API calls, %d errors",
        summary["dates_processed"],
        summary["dates_skipped"],
        summary["observations_upserted"],
        summary["api_calls"],
        summary["errors"],
    )
    return summary
