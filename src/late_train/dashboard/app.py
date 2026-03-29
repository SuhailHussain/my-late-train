"""Flask dashboard application.

Run via: python -m late_train dashboard
Or with gunicorn: gunicorn -w 2 -b 127.0.0.1:8000 late_train.dashboard.app:app

The dashboard is intentionally thin — HTML/Tailwind/Chart.js rendered by the
browser, with Flask serving JSON from the /api/* endpoints.
"""
from __future__ import annotations

import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path

from flask import Flask, jsonify, render_template, request

from late_train.config import Config, load_config
from late_train.db import (
    get_connection,
    init_db,
    query_today_observations,
    query_daily_trends,
    query_worst_days,
    query_delay_reasons,
    query_hsp_summary,
)

_DEFAULT_CONFIG_PATH = Path(__file__).parent.parent.parent.parent / "config.yaml"


def create_app(config: Config | None = None) -> Flask:
    if config is None:
        config = load_config(_DEFAULT_CONFIG_PATH)

    init_db(config.database_path)

    app = Flask(__name__, template_folder="templates")
    app.config["LATE_TRAIN_CONFIG"] = config

    def get_config() -> Config:
        return app.config["LATE_TRAIN_CONFIG"]

    @app.route("/")
    def index():
        cfg = get_config()
        today = date.today().isoformat()
        with get_connection(cfg.database_path) as conn:
            obs = query_today_observations(conn, today)
        return render_template(
            "index.html",
            today=today,
            origin=cfg.route.origin,
            destination=cfg.route.destination,
            observations=[dict(r) for r in obs],
        )

    @app.route("/api/today")
    def api_today():
        cfg = get_config()
        today_str = request.args.get("date", date.today().isoformat())
        with get_connection(cfg.database_path) as conn:
            rows = query_today_observations(conn, today_str)
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
        with get_connection(cfg.database_path) as conn:
            rows = query_daily_trends(conn, days)
        return jsonify([dict(r) for r in rows])

    @app.route("/api/worst-days")
    def api_worst_days():
        cfg = get_config()
        limit = min(int(request.args.get("limit", 10)), 50)
        with get_connection(cfg.database_path) as conn:
            rows = query_worst_days(conn, limit)
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
        with get_connection(cfg.database_path) as conn:
            total = conn.execute(
                "SELECT COUNT(*) FROM daily_observations WHERE cancelled=0"
            ).fetchone()[0]
            delayed = conn.execute(
                "SELECT COUNT(*) FROM daily_observations WHERE delay_mins > 5 AND cancelled=0"
            ).fetchone()[0]
            avg_delay = conn.execute(
                "SELECT ROUND(AVG(delay_mins),1) FROM daily_observations "
                "WHERE cancelled=0 AND delay_mins IS NOT NULL"
            ).fetchone()[0]
            worst = conn.execute(
                "SELECT MAX(delay_mins) FROM daily_observations WHERE cancelled=0"
            ).fetchone()[0]
            cancels = conn.execute(
                "SELECT COUNT(*) FROM daily_observations WHERE cancelled=1"
            ).fetchone()[0]

        pct_on_time = round(100 * (1 - delayed / total), 1) if total else None
        return jsonify({
            "total_journeys": total,
            "pct_on_time": pct_on_time,
            "avg_delay_mins": avg_delay,
            "worst_delay_mins": worst,
            "total_cancellations": cancels,
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
