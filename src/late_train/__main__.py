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
    from datetime import date as date_type
    from late_train.config import load_config
    from late_train.capture import run_capture

    config = load_config(args.config)
    _setup_logging(config.log_level, config.log_file)

    run_date = None
    if args.date:
        run_date = date_type.fromisoformat(args.date)

    if args.days_back:
        from datetime import timedelta
        total = 0
        for i in range(1, args.days_back + 1):
            d = date_type.today() - timedelta(days=i)
            # Skip weekends
            if d.weekday() >= 5:
                continue
            result = run_capture(config, force=True, run_date=d)
            captured = result.get("captured", 0)
            total += captured
            print(f"  {d}: {captured} captured")
        print(f"Total: {total} observations across {args.days_back} days back")
        return

    result = run_capture(config, force=args.force, run_date=run_date)
    if result.get("skipped"):
        print("Outside commute windows — use --force to capture anyway.")
    else:
        print(f"Captured: {result.get('captured', 0)}, "
              f"delayed: {result.get('delayed', 0)}, "
              f"cancelled: {result.get('cancelled', 0)}, "
              f"errors: {result.get('errors', 0)}")


def _cmd_backfill(args: argparse.Namespace) -> None:
    from late_train.config import load_config
    from late_train.backfill import run_backfill

    config = load_config(args.config)
    _setup_logging(config.log_level, config.log_file)
    run_backfill(config, weeks_back=args.weeks)


def _cmd_rtt_backfill(args: argparse.Namespace) -> None:
    from late_train.config import load_config
    from late_train.rtt_backfill import run_rtt_backfill

    config = load_config(args.config)
    _setup_logging(config.log_level, config.log_file)

    if args.dry_run:
        from datetime import date as _date, timedelta as _td
        weekdays = sum(
            1 for i in range(1, args.weeks * 7 + 1)
            if (_date.today() - _td(days=i)).weekday() < 5
        )
        estimated_calls = weekdays * 14
        print(f"Dry run: {weekdays} weekdays × ~14 API calls = ~{estimated_calls} total")
        print(f"RTT free-tier daily limit: ~1000 calls")
        return

    result = run_rtt_backfill(config, weeks_back=args.weeks, delay_secs=args.rate_limit_delay_secs)
    print(
        f"RTT backfill complete: "
        f"{result['dates_processed']} dates processed, "
        f"{result['dates_skipped']} skipped, "
        f"{result['observations_upserted']} observations upserted, "
        f"{result['api_calls']} API calls, "
        f"{result['errors']} errors"
    )


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
    p_capture.add_argument("--force", action="store_true", help="Run even outside commute windows")
    p_capture.add_argument("--date", type=str, default=None, metavar="YYYY-MM-DD",
                           help="Capture a specific historical date")
    p_capture.add_argument("--days-back", type=int, default=0, metavar="N",
                           help="Backfill the last N weekdays from RTT")
    p_capture.set_defaults(func=_cmd_capture)

    # backfill
    p_backfill = sub.add_parser("backfill", help="Pull HSP historical data")
    p_backfill.add_argument(
        "--weeks", type=int, default=1,
        help="Number of weeks to backfill (default: 1, max: 52)",
    )
    p_backfill.set_defaults(func=_cmd_backfill)

    # rtt-backfill
    p_rtt_backfill = sub.add_parser("rtt-backfill", help="Backfill history from RTT API")
    p_rtt_backfill.add_argument(
        "--weeks", type=int, default=1,
        help="Number of weeks to backfill (default: 1, max: 12)",
    )
    p_rtt_backfill.add_argument(
        "--dry-run", action="store_true",
        help="Print estimated API call count without fetching anything",
    )
    p_rtt_backfill.add_argument(
        "--rate-limit-delay-secs", type=float, default=0.5, metavar="SECS",
        help="Sleep between service detail calls (default: 0.5)",
    )
    p_rtt_backfill.set_defaults(func=_cmd_rtt_backfill)

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
