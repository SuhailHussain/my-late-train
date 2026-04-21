"""Flask dashboard application.

Run via: python -m late_train dashboard
Or with gunicorn: gunicorn -w 2 -b 127.0.0.1:8000 late_train.dashboard.app:app

The dashboard is intentionally thin — HTML/Tailwind/Chart.js rendered by the
browser, with Flask serving JSON from the /api/* endpoints.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from flask import Flask, jsonify, render_template, request

from late_train.config import Config, load_config
from late_train.db import (
    get_connection,
    init_db,
    query_departure_times,
    query_today_observations,
    query_daily_trends,
    query_worst_days,
    query_delay_reasons,
    query_hsp_summary,
    query_performance_from_db,
    query_hsp_on_demand_cache,
    upsert_hsp_on_demand_cache,
    buckets_to_performance,
)

_DEFAULT_CONFIG_PATH = Path(__file__).parent.parent.parent.parent / "config.yaml"
logger = logging.getLogger(__name__)


def create_app(config: Config | None = None) -> Flask:
    if config is None:
        config = load_config(_DEFAULT_CONFIG_PATH)

    init_db(config.database_path)

    app = Flask(__name__, template_folder="templates")
    app.config["LATE_TRAIN_CONFIG"] = config

    def get_config() -> Config:
        return app.config["LATE_TRAIN_CONFIG"]

    @app.route("/")
    def landing():
        return render_template("landing.html")

    @app.route("/dashboard")
    def index():
        cfg = get_config()
        origin = request.args.get("from", cfg.route.origin).upper()
        destination = request.args.get("to", cfg.route.destination).upper()
        today = date.today().isoformat()
        with get_connection(cfg.database_path) as conn:
            obs = query_today_observations(conn, today)
        return render_template(
            "index.html",
            today=today,
            origin=origin,
            destination=destination,
            observations=[dict(r) for r in obs],
        )

    @app.route("/api/departure-times")
    def api_departure_times():
        cfg = get_config()
        with get_connection(cfg.database_path) as conn:
            times = query_departure_times(conn)
        return jsonify(times)

    @app.route("/api/today")
    def api_today():
        cfg = get_config()
        today_str = request.args.get("date", date.today().isoformat())
        departure_time = request.args.get("departure_time") or None
        with get_connection(cfg.database_path) as conn:
            rows = query_today_observations(conn, today_str, departure_time)
        result = []
        for r in rows:
            d = dict(r)
            d["status"] = _status_label(d)
            result.append(d)
        return jsonify(result)

    @app.route("/api/trends")
    def api_trends():
        cfg = get_config()
        days = min(int(request.args.get("days", 30)), 365)
        departure_time = request.args.get("departure_time") or None
        with get_connection(cfg.database_path) as conn:
            rows = query_daily_trends(conn, days, departure_time)
        return jsonify([dict(r) for r in rows])

    @app.route("/api/worst-days")
    def api_worst_days():
        cfg = get_config()
        limit = min(int(request.args.get("limit", 10)), 50)
        departure_time = request.args.get("departure_time") or None
        with get_connection(cfg.database_path) as conn:
            rows = query_worst_days(conn, limit, departure_time)
        return jsonify([dict(r) for r in rows])

    @app.route("/api/reasons")
    def api_reasons():
        cfg = get_config()
        months = min(int(request.args.get("months", 3)), 24)
        with get_connection(cfg.database_path) as conn:
            rows = query_delay_reasons(conn, months)
        return jsonify([dict(r) for r in rows])

    @app.route("/api/hsp-summary")
    def api_hsp_summary():
        cfg = get_config()
        with get_connection(cfg.database_path) as conn:
            rows = query_hsp_summary(conn)
        return jsonify([dict(r) for r in rows])

    @app.route("/api/stats")
    def api_stats():
        """Quick summary stats for the header cards."""
        cfg = get_config()
        departure_time = request.args.get("departure_time") or None
        with get_connection(cfg.database_path) as conn:
            row = conn.execute(
                """
                SELECT
                  SUM(CASE WHEN cancelled=0 THEN 1 ELSE 0 END) AS total,
                  SUM(CASE WHEN delay_mins > 5 AND cancelled=0 THEN 1 ELSE 0 END) AS delayed,
                  ROUND(AVG(CASE WHEN cancelled=0 AND delay_mins IS NOT NULL
                                 THEN delay_mins END), 1) AS avg_delay,
                  MAX(CASE WHEN cancelled=0 THEN delay_mins END) AS worst,
                  SUM(CASE WHEN cancelled=1 THEN 1 ELSE 0 END) AS cancels
                FROM daily_observations
                WHERE (? IS NULL OR scheduled_departure = ?)
                """,
                [departure_time, departure_time],
            ).fetchone()

        total = row["total"] or 0
        delayed = row["delayed"] or 0
        pct_on_time = round(100 * (1 - delayed / total), 1) if total else None
        return jsonify({
            "total_journeys": total,
            "pct_on_time": pct_on_time,
            "avg_delay_mins": row["avg_delay"],
            "worst_delay_mins": row["worst"],
            "total_cancellations": row["cancels"] or 0,
        })

    @app.route("/results")
    def results():
        origin = request.args.get("from", "").upper()
        destination = request.args.get("to", "").upper()
        departure = request.args.get("departure", "")
        days = request.args.get("days", "WEEKDAY")
        return render_template(
            "results.html",
            origin=origin,
            destination=destination,
            departure=departure,
            days=days,
        )

    @app.route("/api/trains")
    def api_trains():
        """Return actual trains for a route around a given time (from RTT)."""
        from late_train.rtt import _make_client as rtt_client, search_location, get_service_detail, _iso_to_hhmm

        cfg = get_config()
        origin = request.args.get("from", "").upper()
        destination = request.args.get("to", "").upper()
        around = request.args.get("around", "0900")  # HHMM

        if not origin or not destination:
            return jsonify([])

        try:
            h, m = int(around[:2]), int(around[2:])
        except (ValueError, IndexError):
            return jsonify([])

        # Use the most recent completed weekday — RTT reliably returns
        # fully-resolved data for past dates.
        ref = date.today() - timedelta(days=1)
        while ref.weekday() >= 5:  # skip Saturday (5) and Sunday (6)
            ref -= timedelta(days=1)

        base = datetime(ref.year, ref.month, ref.day, h, m, 0)
        time_from = base - timedelta(minutes=30)
        time_to = base + timedelta(minutes=60)
        day_start = base.replace(hour=0, minute=0)
        day_end = base.replace(hour=23, minute=59)
        time_from = max(time_from, day_start)
        time_to = min(time_to, day_end)

        try:
            with rtt_client(cfg.rtt.base_url, cfg.rtt.refresh_token) as client:
                services = search_location(client, origin, destination, time_from, time_to)
                logger.info("RTT returned %d services for %s→%s on %s", len(services), origin, destination, ref)

                seen: set[str] = set()
                trains_list = []
                for svc in services:
                    meta = svc.get("scheduleMetadata") or {}
                    service_uid = meta.get("identity") or svc.get("serviceUid") or ""
                    if not service_uid or service_uid in seen:
                        continue

                    # The search response doesn't include departure times in the
                    # real RTT API — fetch the service detail to get them.
                    try:
                        detail = get_service_detail(client, service_uid, ref)
                    except Exception:
                        continue

                    svc_data = detail.get("service") or {}
                    dep_time = None
                    for loc in (svc_data.get("locations") or []):
                        loc_crss = [c.upper() for c in (loc.get("location") or {}).get("shortCodes") or []]
                        if origin in loc_crss:
                            dep = (loc.get("temporalData") or {}).get("departure") or {}
                            dep_time = _iso_to_hhmm(dep.get("scheduleAdvertised"))
                            break

                    if not dep_time:
                        continue

                    seen.add(service_uid)
                    svc_meta = svc_data.get("scheduleMetadata") or meta
                    op = (svc_meta.get("operator") or {})
                    trains_list.append({
                        "departure": dep_time,
                        "operator": op.get("name") or op.get("code") or svc.get("atocName") or "",
                        "service_uid": service_uid,
                    })

        except Exception as exc:
            logger.warning("RTT trains lookup failed: %s", exc)
            return jsonify({"error": str(exc), "trains": []})

        trains_list.sort(key=lambda x: x["departure"])
        return jsonify(trains_list)

    @app.route("/api/performance")
    def api_performance():
        """Return historical performance for a route + departure time.

        Tier 1: local RTT daily_observations (most detail, configured route only)
        Tier 2: HSP on-demand cache (any route, refreshed weekly)
        Tier 3: live HSP API call → cached → returned
        """
        from datetime import timedelta
        from late_train.hsp import _make_client as _make_hsp_client, get_service_metrics

        cfg = get_config()
        origin = request.args.get("from", "").upper()
        destination = request.args.get("to", "").upper()
        departure = request.args.get("departure", "")  # HHMM
        days = request.args.get("days", "WEEKDAY").upper()
        months = min(int(request.args.get("months", 6)), 24)

        if not origin or not destination or not departure:
            return jsonify({"error": "from, to and departure are required"}), 400

        departure = departure.replace(":", "")
        try:
            int(departure[:2]), int(departure[2:])
        except (ValueError, IndexError):
            return jsonify({"error": "Invalid departure time"}), 400

        # Narrow ±10-min window used as the HSP query + cache key
        dep_mins = int(departure[:2]) * 60 + int(departure[2:])
        def _fmt(m): return f"{max(0, m) // 60:02d}{max(0, m) % 60:02d}"
        hsp_from = _fmt(dep_mins - 10)
        hsp_to   = _fmt(min(dep_mins + 10, 23 * 60 + 59))

        # --- Tier 1: RTT observations ---
        with get_connection(cfg.database_path) as conn:
            result = query_performance_from_db(conn, origin, destination, departure, days, months)
        if result["total"] > 0:
            logger.info("Performance: RTT hit for %s→%s %s", origin, destination, departure)
            return jsonify(result)

        # --- Tier 2: HSP cache ---
        with get_connection(cfg.database_path) as conn:
            cached = query_hsp_on_demand_cache(conn, origin, destination, hsp_from, hsp_to, days)
        if cached:
            logger.info("Performance: HSP cache hit for %s→%s %s", origin, destination, departure)
            return jsonify(cached)

        # --- Tier 3: Live HSP fetch ---
        logger.info("Performance: HSP live fetch for %s→%s dep=%s days=%s", origin, destination, departure, days)
        period_start = (date.today() - timedelta(days=180)).isoformat()
        period_end   = date.today().isoformat()
        try:
            with _make_hsp_client(cfg.hsp.base_url, cfg.hsp.api_key) as hsp_client:
                buckets, _ = get_service_metrics(
                    hsp_client, origin, destination,
                    hsp_from, hsp_to,
                    period_start, period_end,
                    days,
                )
        except Exception as exc:
            logger.warning("HSP live fetch failed for %s→%s: %s", origin, destination, exc)
            return jsonify({"total": 0, "error": f"Could not fetch data from National Rail: {exc}"}), 200

        with get_connection(cfg.database_path) as conn:
            upsert_hsp_on_demand_cache(conn, {
                "origin": origin, "destination": destination,
                "from_time": hsp_from, "to_time": hsp_to, "days": days,
                **buckets,
                "period_start": period_start, "period_end": period_end,
                "retrieved_at": datetime.now(timezone.utc).isoformat(),
            })

        result = buckets_to_performance(buckets, period_start, period_end)
        if result["total"] == 0:
            return jsonify({"total": 0, "error": "No data found for this route and departure time."})
        return jsonify(result)

    return app


def _status_label(obs: dict) -> str:
    if obs.get("cancelled"):
        return "cancelled"
    delay = obs.get("delay_mins")
    if delay is None:
        return "unknown"
    if delay <= 1:
        return "on-time"
    if delay <= 5:
        return "slight"
    if delay <= 15:
        return "delayed"
    return "very-delayed"


# Allow running directly via gunicorn: gunicorn late_train.dashboard.app:app
# Only created when config.yaml exists so tests can import without credentials.
def _make_default_app():
    if _DEFAULT_CONFIG_PATH.exists():
        try:
            return create_app()
        except Exception:
            return None
    return None

app = _make_default_app()
