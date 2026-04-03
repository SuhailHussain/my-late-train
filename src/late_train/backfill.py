"""Weekly HSP backfill.

Pulls historical performance data for the configured route. Intended to run
weekly via cron to fill in any gaps and to populate initial history.

Usage:
    python -m late_train backfill              # last 1 week
    python -m late_train backfill --weeks=52   # full year of history
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

from late_train.config import Config
from late_train.db import (
    get_connection,
    init_db,
    upsert_hsp_metrics,
    upsert_observation,
)
from late_train.hsp import (
    _make_client,
    get_service_metrics,
    get_service_details,
    locations_to_observation,
)

logger = logging.getLogger(__name__)


def _date_range_for_week(weeks_ago: int) -> tuple[str, str]:
    """Return (from_date, to_date) for a given number of weeks back."""
    today = date.today()
    to_date = today - timedelta(days=today.weekday() + 1 + (weeks_ago - 1) * 7)
    from_date = to_date - timedelta(days=6)
    return from_date.isoformat(), to_date.isoformat()


def run_backfill(config: Config, weeks_back: int = 1) -> dict:
    """Pull HSP data for the past N weeks and store in database.

    For each week and commute window:
    1. Call serviceMetrics for aggregate stats.
    2. For each RID, call serviceDetails to get individual journey times.
    3. Insert individual observations into daily_observations (source='hsp')
       only if no RTT record already exists for that service/date.
    """
    weeks_back = min(weeks_back, 52)
    init_db(config.database_path)
    captured_at = datetime.now(timezone.utc).isoformat()

    summary = {
        "weeks_processed": 0,
        "metrics_upserted": 0,
        "observations_inserted": 0,
        "errors": 0,
    }

    with _make_client(config.hsp.base_url, config.hsp.api_key) as client:
        for week_i in range(1, weeks_back + 1):
            from_date, to_date = _date_range_for_week(week_i)
            logger.info("Backfilling week %d/%d: %s to %s", week_i, weeks_back, from_date, to_date)

            for window_name, window in config.commute_windows.as_list():
                from_time = window.start.replace(":", "")
                to_time = window.end.replace(":", "")

                try:
                    buckets, rids = get_service_metrics(
                        client,
                        config.route.origin,
                        config.route.destination,
                        from_time,
                        to_time,
                        from_date,
                        to_date,
                    )
                except Exception as exc:
                    logger.error(
                        "serviceMetrics failed for %s window %s–%s: %s",
                        window_name, from_date, to_date, exc,
                    )
                    summary["errors"] += 1
                    continue

                # Upsert aggregate metrics
                with get_connection(config.database_path) as conn:
                    upsert_hsp_metrics(conn, {
                        "origin": config.route.origin,
                        "destination": config.route.destination,
                        "from_time": from_time,
                        "to_time": to_time,
                        "period_start": from_date,
                        "period_end": to_date,
                        **buckets,
                        "retrieved_at": captured_at,
                    })
                summary["metrics_upserted"] += 1

                # Fetch individual service details and insert observations
                for rid in rids:
                    try:
                        locations = get_service_details(
                            client, rid, config.route.origin, config.route.destination
                        )
                    except Exception as exc:
                        logger.warning("serviceDetails failed for RID %s: %s", rid, exc)
                        summary["errors"] += 1
                        continue

                    obs = locations_to_observation(
                        rid, locations,
                        config.route.origin, config.route.destination,
                        captured_at,
                    )
                    if obs is None:
                        continue

                    with get_connection(config.database_path) as conn:
                        # Only insert if no RTT record exists for this service/date
                        existing = conn.execute(
                            "SELECT 1 FROM daily_observations "
                            "WHERE service_uid=? AND run_date=? AND source='rtt'",
                            (obs["service_uid"], obs["run_date"]),
                        ).fetchone()

                        if existing is None:
                            try:
                                upsert_observation(conn, obs)
                                summary["observations_inserted"] += 1
                            except Exception as exc:
                                logger.warning("Failed to insert HSP observation: %s", exc)
                                summary["errors"] += 1

            summary["weeks_processed"] += 1

    logger.info(
        "Backfill complete: %d weeks, %d metric rows, %d observations, %d errors",
        summary["weeks_processed"],
        summary["metrics_upserted"],
        summary["observations_inserted"],
        summary["errors"],
    )
    return summary
