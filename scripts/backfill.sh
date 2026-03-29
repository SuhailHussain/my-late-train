#!/usr/bin/env bash
# Cron wrapper for HSP weekly backfill.
# Add to crontab: 0 20 * * 0 /opt/my-late-train/scripts/backfill.sh
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$DIR"

if [[ -f "$DIR/.env" ]]; then
    set -a
    source "$DIR/.env"
    set +a
fi

source "$DIR/.venv/bin/activate"
exec python -m late_train backfill --weeks="${1:-1}" 2>> "$DIR/data/logs/backfill.log"
