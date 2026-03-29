"""CLI entry point: python -m late_train <subcommand>"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path


def _setup_logging(level: str, log_file: Path | None = None) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        from logging.handlers import RotatingFileHandler
        handlers.append(
            RotatingFileHandler(log_file, maxBytes=5 * 1024 * 1024, backupCount=3)
        )
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
    )


def _cmd_capture(args: argparse.Namespace) -> None:
    from late_train.config import load_config
    from late_train.capture import run_capture

    config = load_config(args.config)
    _setup_logging(config.log_level, config.log_file)
    run_capture(config)


def _cmd_backfill(args: argparse.Namespace) -> None:
    from late_train.config import load_config
    from late_train.backfill import run_backfill

    config = load_config(args.config)
    _setup_logging(config.log_level, config.log_file)
    run_backfill(config, weeks_back=args.weeks)


def _cmd_attribution(args: argparse.Namespace) -> None:
    from late_train.config import load_config
    from late_train.attribution import ingest_new_csvs

    config = load_config(args.config)
    _setup_logging(config.log_level, config.log_file)
    count = ingest_new_csvs(config)
    print(f"Ingested {count} new attribution records.")


def _cmd_dashboard(args: argparse.Namespace) -> None:
    from late_train.config import load_config
    from late_train.dashboard.app import create_app

    config = load_config(args.config)
    _setup_logging(config.log_level, config.log_file)
    app = create_app(config)
    app.run(host=args.host, port=args.port, debug=args.debug)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="late_train",
        description="UK Train Delay Tracker",
    )
    parser.add_argument(
        "--config", type=Path, default=None,
        help="Path to config.yaml (default: ./config.yaml)",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    # capture
    p_capture = sub.add_parser("capture", help="Poll RTT API and record today's trains")
    p_capture.set_defaults(func=_cmd_capture)

    # backfill
    p_backfill = sub.add_parser("backfill", help="Pull HSP historical data")
    p_backfill.add_argument(
        "--weeks", type=int, default=1,
        help="Number of weeks to backfill (default: 1, max: 52)",
    )
    p_backfill.set_defaults(func=_cmd_backfill)

    # attribution
    p_attr = sub.add_parser("attribution", help="Ingest NR attribution CSVs from data/attribution/")
    p_attr.set_defaults(func=_cmd_attribution)

    # dashboard
    p_dash = sub.add_parser("dashboard", help="Run the Flask dashboard")
    p_dash.add_argument("--host", default="127.0.0.1")
    p_dash.add_argument("--port", type=int, default=8000)
    p_dash.add_argument("--debug", action="store_true")
    p_dash.set_defaults(func=_cmd_dashboard)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
