"""Tests for config loading and env var resolution."""
import os
import pytest
import textwrap
from pathlib import Path

from late_train.config import load_config, _resolve_env


def test_resolve_env_substitutes(monkeypatch):
    monkeypatch.setenv("MY_VAR", "hello")
    assert _resolve_env("prefix_${MY_VAR}_suffix") == "prefix_hello_suffix"


def test_resolve_env_missing_raises(monkeypatch):
    monkeypatch.delenv("MISSING_VAR", raising=False)
    with pytest.raises(EnvironmentError, match="MISSING_VAR"):
        _resolve_env("${MISSING_VAR}")


def test_load_config(tmp_path, monkeypatch):
    monkeypatch.setenv("RTT_TOKEN", "my-refresh-token")
    monkeypatch.setenv("HSP_KEY", "my-api-key")

    config_file = tmp_path / "config.yaml"
    config_file.write_text(textwrap.dedent("""\
        route:
          origin: LBG
          destination: BTN
          service_uids: []

        commute_windows:
          morning:
            start: "07:30"
            end: "09:00"
          evening:
            start: "17:30"
            end: "19:00"

        apis:
          rtt:
            base_url: "https://data.rtt.io"
            refresh_token: "${RTT_TOKEN}"
          hsp:
            base_url: "https://hsp-prod.rockshore.net/api/v1"
            api_key: "${HSP_KEY}"

        attribution:
          csv_directory: "./data/attribution"

        database:
          path: "./data/late_train.db"

        logging:
          level: "DEBUG"
          file: "./data/logs/test.log"
    """))

    config = load_config(config_file)

    assert config.route.origin == "LBG"
    assert config.route.destination == "BTN"
    assert config.route.service_uids == []
    assert config.commute_windows.morning.start == "07:30"
    assert config.commute_windows.evening.end == "19:00"
    assert config.rtt.refresh_token == "my-refresh-token"
    assert config.rtt.base_url == "https://data.rtt.io"
    assert config.hsp.api_key == "my-api-key"
    assert config.hsp.base_url == "https://hsp-prod.rockshore.net/api/v1"
    assert config.log_level == "DEBUG"
    assert config.database_path.name == "late_train.db"


def test_load_config_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_config(tmp_path / "nonexistent.yaml")


def test_load_config_missing_credential_raises(tmp_path, monkeypatch):
    monkeypatch.delenv("RTT_REFRESH_TOKEN", raising=False)
    monkeypatch.setenv("HSP_KEY", "key")

    config_file = tmp_path / "config.yaml"
    config_file.write_text(textwrap.dedent("""\
        route:
          origin: LBG
          destination: BTN
          service_uids: []
        commute_windows:
          morning:
            start: "07:00"
            end: "09:00"
          evening:
            start: "17:00"
            end: "19:00"
        apis:
          rtt:
            base_url: "https://data.rtt.io"
            refresh_token: "${RTT_REFRESH_TOKEN}"
          hsp:
            base_url: "https://hsp-prod.rockshore.net/api/v1"
            api_key: "${HSP_KEY}"
        attribution:
          csv_directory: "./data/attribution"
        database:
          path: "./data/late_train.db"
    """))

    with pytest.raises(EnvironmentError, match="RTT_REFRESH_TOKEN"):
        load_config(config_file)
