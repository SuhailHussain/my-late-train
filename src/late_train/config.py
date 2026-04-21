"""Load and validate configuration from config.yaml.

Credentials are never stored in the YAML directly — use ${ENV_VAR} placeholders
which are resolved from the process environment at load time.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import yaml

_ENV_RE = re.compile(r"\$\{([^}]+)\}")

# Default config path relative to project root
_DEFAULT_CONFIG = Path(__file__).parent.parent.parent / "config.yaml"


def _resolve_path(raw: str, base_dir: Path) -> Path:
    p = Path(raw)
    return p if p.is_absolute() else base_dir / p


def _resolve_env(value: str) -> str:
    """Replace ${VAR} placeholders with values from os.environ."""
    def replacer(m: re.Match) -> str:
        var = m.group(1)
        val = os.environ.get(var)
        if val is None:
            raise EnvironmentError(
                f"Environment variable '{var}' is required but not set. "
                f"Set it in your shell or a .env file."
            )
        return val

    return _ENV_RE.sub(replacer, value)


def _resolve_values(obj):
    """Recursively resolve ${ENV_VAR} in all string values."""
    if isinstance(obj, dict):
        return {k: _resolve_values(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_resolve_values(v) for v in obj]
    if isinstance(obj, str):
        return _resolve_env(obj)
    return obj


@dataclass
class CommuteWindow:
    start: str  # HH:MM
    end: str    # HH:MM

    def datetimes(self, for_date: date) -> tuple[datetime, datetime]:
        h0, m0 = map(int, self.start.split(":"))
        h1, m1 = map(int, self.end.split(":"))
        return (
            datetime(for_date.year, for_date.month, for_date.day, h0, m0),
            datetime(for_date.year, for_date.month, for_date.day, h1, m1),
        )


@dataclass
class CommuteWindows:
    morning: CommuteWindow
    evening: CommuteWindow

    def as_list(self) -> list[tuple[str, CommuteWindow]]:
        return [("morning", self.morning), ("evening", self.evening)]


@dataclass
class RTTConfig:
    """RTT API config — uses OAuth2 Bearer token auth."""
    base_url: str
    refresh_token: str  # Long-lived token used to obtain short-lived access tokens


@dataclass
class ApiCredentials:
    """HSP API credentials — Rail Data Marketplace x-apikey header auth."""
    base_url: str
    api_key: str


@dataclass
class RouteConfig:
    origin: str
    destination: str
    service_uids: list[str] = field(default_factory=list)


@dataclass
class Config:
    route: RouteConfig
    commute_windows: CommuteWindows
    rtt: RTTConfig
    hsp: ApiCredentials
    attribution_csv_directory: Path
    database_path: Path
    log_level: str = "INFO"
    log_file: Optional[Path] = None


def load_config(path: Path | None = None) -> Config:
    """Load configuration from a YAML file, resolving ${ENV_VAR} placeholders."""
    config_path = Path(path) if path else _DEFAULT_CONFIG

    if not config_path.exists():
        raise FileNotFoundError(
            f"Config file not found: {config_path}\n"
            "Copy config.yaml.example to config.yaml and fill in your settings."
        )

    with config_path.open() as f:
        raw = yaml.safe_load(f)

    raw = _resolve_values(raw)

    route = RouteConfig(
        origin=raw["route"]["origin"].upper(),
        destination=raw["route"]["destination"].upper(),
        service_uids=raw["route"].get("service_uids") or [],
    )

    windows = CommuteWindows(
        morning=CommuteWindow(**raw["commute_windows"]["morning"]),
        evening=CommuteWindow(**raw["commute_windows"]["evening"]),
    )

    rtt = RTTConfig(
        base_url=raw["apis"]["rtt"]["base_url"].rstrip("/"),
        refresh_token=raw["apis"]["rtt"]["refresh_token"],
    )

    hsp = ApiCredentials(
        base_url=raw["apis"]["hsp"]["base_url"].rstrip("/"),
        api_key=raw["apis"]["hsp"]["api_key"],
    )

    # Resolve paths relative to the config file's directory
    base_dir = config_path.parent

    attribution_dir = _resolve_path(raw["attribution"]["csv_directory"], base_dir)
    db_path = _resolve_path(raw["database"]["path"], base_dir)

    log_cfg = raw.get("logging", {})
    log_file = _resolve_path(log_cfg["file"], base_dir) if log_cfg.get("file") else None

    return Config(
        route=route,
        commute_windows=windows,
        rtt=rtt,
        hsp=hsp,
        attribution_csv_directory=attribution_dir,
        database_path=db_path,
        log_level=log_cfg.get("level", "INFO").upper(),
        log_file=log_file,
    )
