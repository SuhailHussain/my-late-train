#!/usr/bin/env bash
# Cron wrapper for NR attribution CSV ingestion.
# Add to crontab: 0 21 1 * * /opt/my-late-train/scripts/attribution.sh
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$DIR"

if [[ -f "$DIR/.env" ]]; then
    set -a
    source "$DIR/.env"
    set +a
fi

source "$DIR/.venv/bin/activate"
exec python -m late_train attribution 2>> "$DIR/data/logs/attribution.log"
