"""Network Rail delay attribution CSV parser.

Attribution CSVs are available monthly from:
  https://www.networkrail.co.uk/who-we-are/transparency-and-ethics/transparency/

Download the "Delay Attribution" files and drop them in the configured
csv_directory. This module scans for new files and ingests them.

Key columns in the CSV:
  INCIDENT_NUMBER, RESP_MANAGER, FINANCIAL_YEAR_PERIOD, TRAIN_SERVICE_CODE,
  ENGLISH_DAY_TYPE, STANOX, EVENT_TYPE, PFPI_MINUTES, TRUST_TRAIN_ID,
  INCIDENT_REASON, INCIDENT_REASON_DESCRIPTION, INCIDENT_RESPONSIBLE_TRAIN_OPERATOR,
  START_DATETIME (or similar date column)

Note: Formal attribution only covers delays >= 3 minutes. Sub-threshold delays
(~35% of all delay minutes) are never attributed.
"""
from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd

from late_train.config import Config
from late_train.db import get_connection, init_db, insert_attributions
from late_train.stations import route_stanox_codes

logger = logging.getLogger(__name__)

# Mapping from CSV column names to our DB column names.
# NR attribution CSVs have varied column naming across years — we check alternatives.
_COLUMN_ALIASES: dict[str, list[str]] = {
    "incident_number": ["INCIDENT_NUMBER", "INCIDENTNUMBER", "Incident Number"],
    "trust_train_id": ["TRUST_TRAIN_ID", "TRUSTTRAINID", "Trust Train ID"],
    "stanox": ["STANOX", "STANOX_AFFECTED", "Stanox"],
    "event_type": ["EVENT_TYPE", "EVENTTYPE", "Event Type"],
    "delay_mins": ["PFPI_MINUTES", "PFPI Minutes", "DELAY_MINUTES", "Delay Minutes"],
    "reason_code": ["INCIDENT_REASON", "INCIDENT REASON", "Incident Reason"],
    "reason_text": ["INCIDENT_REASON_DESCRIPTION", "REASON_DESCRIPTION", "Reason Description"],
    "responsible_org": [
        "INCIDENT_RESPONSIBLE_TRAIN_OPERATOR",
        "RESP_MANAGER",
        "Responsible Manager",
        "Responsible Train Operator",
    ],
    "financial_period": ["FINANCIAL_YEAR_PERIOD", "FINANCIALYEARPERIOD", "Financial Year Period"],
    "run_date": ["START_DATETIME", "INCIDENT_DATE", "Incident Date", "START_DATE", "Date"],
    "train_service_code": ["TRAIN_SERVICE_CODE", "TRAINSERVICECODE"],
}


def _find_column(df: pd.DataFrame, key: str) -> str | None:
    """Return the first matching column name for a given key."""
    for candidate in _COLUMN_ALIASES.get(key, []):
        if candidate in df.columns:
            return candidate
    return None


def _parse_date(series: pd.Series) -> pd.Series:
    """Try to parse a date column. Prefer ISO format (YYYY-MM-DD) before dayfirst."""
    # Try ISO/unambiguous format first, then fall back to dayfirst British convention
    parsed = pd.to_datetime(series, errors="coerce", dayfirst=False)
    if parsed.isna().all():
        parsed = pd.to_datetime(series, errors="coerce", dayfirst=True)
    return parsed.dt.strftime("%Y-%m-%d")


def parse_attribution_csv(
    csv_path: Path,
    filter_stanox: set[str] | None = None,
) -> list[dict]:
    """Parse a NR attribution CSV and return a list of attribution row dicts.

    Args:
        csv_path: Path to the CSV file.
        filter_stanox: If provided, only include rows where STANOX matches.
                       Pass None to import all rows (useful for initial testing).
    """
    logger.info("Parsing attribution CSV: %s", csv_path.name)

    try:
        df = pd.read_csv(csv_path, encoding="latin-1", low_memory=False)
    except Exception as exc:
        logger.error("Failed to read CSV %s: %s", csv_path.name, exc)
        return []

    logger.debug("CSV has %d rows, columns: %s", len(df), list(df.columns))

    # Filter by STANOX if requested
    stanox_col = _find_column(df, "stanox")
    if filter_stanox and stanox_col:
        # Normalise: strip trailing .0 from float-typed integers (e.g. 33081.0 → "33081")
        stanox_str = df[stanox_col].apply(
            lambda v: str(int(float(v))) if pd.notna(v) else ""
        )
        df = df[stanox_str.isin(filter_stanox)]
        logger.debug("After STANOX filter: %d rows", len(df))

    if df.empty:
        return []

    # Resolve column names
    def col(key: str):
        return _find_column(df, key)

    rows = []
    date_col = col("run_date")
    if date_col:
        df["_run_date"] = _parse_date(df[date_col])
    else:
        df["_run_date"] = None

    for _, row in df.iterrows():
        def get(key: str):
            c = col(key)
            if c is None:
                return None
            val = row.get(c)
            if pd.isna(val):
                return None
            return str(val).strip()

        run_date = row.get("_run_date")
        if not run_date or run_date == "NaT":
            run_date = None

        delay_mins = get("delay_mins")
        try:
            delay_mins = float(delay_mins) if delay_mins is not None else None
        except (ValueError, TypeError):
            delay_mins = None

        rows.append({
            "incident_number": get("incident_number"),
            "run_date": run_date or "1970-01-01",
            "trust_train_id": get("trust_train_id"),
            "service_uid": None,  # HSP/RTT UID not available in attribution data
            "stanox": get("stanox"),
            "event_type": get("event_type"),
            "delay_mins": delay_mins,
            "reason_code": get("reason_code"),
            "reason_text": get("reason_text"),
            "responsible_org": get("responsible_org"),
            "financial_period": get("financial_period"),
            "csv_filename": csv_path.name,
        })

    return rows


def _already_ingested(conn, csv_filename: str) -> bool:
    """Return True if any rows from this CSV have already been inserted."""
    result = conn.execute(
        "SELECT 1 FROM delay_attributions WHERE csv_filename = ? LIMIT 1",
        (csv_filename,),
    ).fetchone()
    return result is not None


def ingest_new_csvs(config: Config) -> int:
    """Scan the attribution CSV directory and ingest any new files.

    Returns the total number of new records inserted.
    """
    csv_dir = config.attribution_csv_directory
    if not csv_dir.exists():
        logger.warning("Attribution CSV directory does not exist: %s", csv_dir)
        return 0

    csv_files = sorted(csv_dir.glob("*.csv")) + sorted(csv_dir.glob("*.CSV"))
    if not csv_files:
        logger.info("No CSV files found in %s", csv_dir)
        return 0

    init_db(config.database_path)
    filter_stanox = route_stanox_codes(config.route.origin, config.route.destination)

    total_inserted = 0

    for csv_path in csv_files:
        with get_connection(config.database_path) as conn:
            if _already_ingested(conn, csv_path.name):
                logger.debug("Skipping already-ingested file: %s", csv_path.name)
                continue

        rows = parse_attribution_csv(csv_path, filter_stanox or None)
        if not rows:
            logger.info("No matching rows in %s", csv_path.name)
            continue

        with get_connection(config.database_path) as conn:
            inserted = insert_attributions(conn, rows)
            total_inserted += inserted
            logger.info("Inserted %d new attribution records from %s", inserted, csv_path.name)

    return total_inserted
