#!/usr/bin/env bash
# Install cron jobs for the late-train tracker.
# Run once after deployment: bash scripts/setup_cron.sh
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

chmod +x "$DIR/scripts/capture.sh" "$DIR/scripts/backfill.sh" "$DIR/scripts/attribution.sh"

# Build the new cron entries
NEW_JOBS=$(cat <<EOF
# my-late-train: RTT capture every 3 min on weekday commute hours
*/3 7-9 * * 1-5 $DIR/scripts/capture.sh >> /dev/null 2>&1
*/3 17-19 * * 1-5 $DIR/scripts/capture.sh >> /dev/null 2>&1
# my-late-train: HSP weekly backfill (Sunday 20:00)
0 20 * * 0 $DIR/scripts/backfill.sh >> /dev/null 2>&1
# my-late-train: Attribution CSV ingest (1st of month 21:00)
0 21 1 * * $DIR/scripts/attribution.sh >> /dev/null 2>&1
EOF
)

# Remove any existing my-late-train cron entries, then append the new ones
(crontab -l 2>/dev/null | grep -v "my-late-train" | grep -v "$DIR/scripts/"; echo "$NEW_JOBS") | crontab -

echo "Cron jobs installed:"
crontab -l | grep -A1 "my-late-train"
