#!/usr/bin/env bash
# Cron wrapper for RTT real-time capture.
# Add to crontab: */3 7-9,17-19 * * 1-5 /opt/my-late-train/scripts/capture.sh
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$DIR"

# Load .env if present (for credentials)
if [[ -f "$DIR/.env" ]]; then
    set -a
    source "$DIR/.env"
    set +a
fi

source "$DIR/.venv/bin/activate"
exec python -m late_train capture 2>> "$DIR/data/logs/capture.log"
