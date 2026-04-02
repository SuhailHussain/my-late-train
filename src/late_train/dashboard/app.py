"""Flask dashboard application.

Run via: python -m late_train dashboard
Or with gunicorn: gunicorn -w 2 -b 127.0.0.1:8000 late_train.dashboard.app:app

The dashboard is intentionally thin — HTML/Tailwind/Chart.js rendered by the
browser, with Flask serving JSON from the /api/* endpoints.
"""
from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from flask import Flask, jsonify, redirect, render_template, request, url_for

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
    upsert_hsp_metrics,
)

_DEFAULT_CONFIG_PATH = Path(__file__).parent.parent.parent.parent / "config.yaml"
_STATIONS_PATH = Path(__file__).parent.parent / "stations_all.json"

def _load_stations() -> list[dict]:
    if _STATIONS_PATH.exists():
        import json
        return json.loads(_STATIONS_PATH.read_text())
    return []

_STATIONS = _load_stations()


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

    @app.route("/api/stations")
    def api_stations():
        return jsonify(_STATIONS)

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
        dt_filter = "AND scheduled_departure = ?" if departure_time else ""
        dt_param = [departure_time] if departure_time else []
        with get_connection(cfg.database_path) as conn:
            total = conn.execute(
                f"SELECT COUNT(*) FROM daily_observations WHERE cancelled=0 {dt_filter}",
                dt_param,
            ).fetchone()[0]
            delayed = conn.execute(
                f"SELECT COUNT(*) FROM daily_observations WHERE delay_mins > 5 AND cancelled=0 {dt_filter}",
                dt_param,
            ).fetchone()[0]
            avg_delay = conn.execute(
                f"SELECT ROUND(AVG(delay_mins),1) FROM daily_observations "
                f"WHERE cancelled=0 AND delay_mins IS NOT NULL {dt_filter}",
                dt_param,
            ).fetchone()[0]
            worst = conn.execute(
                f"SELECT MAX(delay_mins) FROM daily_observations WHERE cancelled=0 {dt_filter}",
                dt_param,
            ).fetchone()[0]
            cancels = conn.execute(
                f"SELECT COUNT(*) FROM daily_observations WHERE cancelled=1 {dt_filter}",
                dt_param,
            ).fetchone()[0]

        pct_on_time = round(100 * (1 - delayed / total), 1) if total else None
        return jsonify({
            "total_journeys": total,
            "pct_on_time": pct_on_time,
            "avg_delay_mins": avg_delay,
            "worst_delay_mins": worst,
            "total_cancellations": cancels,
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
        import logging as _logging
        from late_train.rtt import _make_client as rtt_client, search_location, get_service_detail, _iso_to_hhmm
        _log = _logging.getLogger(__name__)

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
                _log.info("RTT returned %d services for %s→%s on %s", len(services), origin, destination, ref)

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
            _log.warning("RTT trains lookup failed: %s", exc)
            return jsonify({"error": str(exc), "trains": []})

        trains_list.sort(key=lambda x: x["departure"])
        return jsonify(trains_list)

    @app.route("/api/performance")
    def api_performance():
        """Return HSP historical performance for a specific route + departure time."""
        import logging as _logging
        from late_train.hsp import _make_client as hsp_client, get_service_metrics
        _log = _logging.getLogger(__name__)

        cfg = get_config()
        origin = request.args.get("from", "").upper()
        destination = request.args.get("to", "").upper()
        departure = request.args.get("departure", "")  # HHMM
        days = request.args.get("days", "WEEKDAY")
        months = min(int(request.args.get("months", 6)), 24)

        if not origin or not destination or not departure:
            return jsonify({"error": "from, to and departure are required"}), 400

        departure = departure.replace(":", "")

        try:
            h, m = int(departure[:2]), int(departure[2:])
        except (ValueError, IndexError):
            return jsonify({"error": "Invalid departure time"}), 400

        dep_dt = datetime.now().replace(hour=h, minute=m, second=0, microsecond=0)
        from_time = (dep_dt - timedelta(minutes=45)).strftime("%H%M")
        to_time = (dep_dt + timedelta(minutes=45)).strftime("%H%M")

        today = date.today()
        from_date = (today - timedelta(days=months * 30)).isoformat()
        to_date = today.isoformat()

        _log.info(
            "HSP query: %s→%s time=%s-%s date=%s-%s days=%s",
            origin, destination, from_time, to_time, from_date, to_date, days
        )

        try:
            with hsp_client(cfg.hsp.base_url, cfg.hsp.username, cfg.hsp.password) as client:
                buckets, rids = get_service_metrics(
                    client, origin, destination,
                    from_time, to_time,
                    from_date, to_date,
                    days,
                )
        except Exception as exc:
            _log.warning("HSP query failed: %s", exc)
            return jsonify({"error": str(exc)}), 503

        _log.info("HSP buckets: %s  rids: %d", buckets, len(rids))

        total = buckets.get("total_services", 0)
        if total == 0:
            return jsonify({"total": 0, "error": "No data found for this service"})

        def pct(n):
            return round(100 * n / total, 1) if total else 0

        on_time   = buckets.get("on_time_count", 0)
        l_1_5     = buckets.get("late_1_5_count", 0)
        l_5_10    = buckets.get("late_5_10_count", 0)
        l_10_15   = buckets.get("late_10_15_count", 0)
        l_15_20   = buckets.get("late_15_20_count", 0)
        l_20_30   = buckets.get("late_20_30_count", 0)
        l_30_plus = buckets.get("late_30_plus_count", 0)
        cancelled = buckets.get("cancel_count", 0)

        total_late = l_1_5 + l_5_10 + l_10_15 + l_15_20 + l_20_30 + l_30_plus
        if total_late:
            weighted = (l_1_5*3 + l_5_10*7.5 + l_10_15*12.5 + l_15_20*17.5 + l_20_30*25 + l_30_plus*35)
            avg_late = round(weighted / total_late, 1)
        else:
            avg_late = None

        try:
            with get_connection(cfg.database_path) as conn:
                upsert_hsp_metrics(conn, {
                    "origin": origin, "destination": destination,
                    "from_time": from_time, "to_time": to_time,
                    "period_start": from_date, "period_end": to_date,
                    "total_services": total,
                    "on_time_count": on_time,
                    "late_1_5_count": l_1_5,
                    "late_5_10_count": l_5_10,
                    "late_10_15_count": l_10_15,
                    "late_15_20_count": l_15_20,
                    "late_20_30_count": l_20_30,
                    "late_30_plus_count": l_30_plus,
                    "cancel_count": cancelled,
                    "retrieved_at": datetime.now(timezone.utc).isoformat(),
                })
        except Exception:
            pass

        return jsonify({
            "total": total,
            "from_date": from_date,
            "to_date": to_date,
            "pct_on_time":      pct(on_time),
            "pct_late_1_5":     pct(l_1_5),
            "pct_late_5_10":    pct(l_5_10),
            "pct_late_10_15":   pct(l_10_15),
            "pct_late_15_20":   pct(l_15_20),
            "pct_late_20_30":   pct(l_20_30),
            "pct_late_30_plus": pct(l_30_plus),
            "pct_cancelled":    pct(cancelled),
            "avg_late_mins":    avg_late,
        })

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
