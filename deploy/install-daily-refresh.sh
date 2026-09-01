#!/usr/bin/env bash
# Install a daily cron job that refreshes live sources and re-scores.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPT="$ROOT/deploy/daily-refresh.sh"
CRON_TIME="${FIREWATCH_CRON_TIME:-0 13 * * *}"  # 06:00 America/Vancouver (PDT)

chmod +x "$SCRIPT"

MARKER="# firewatch-daily-refresh"
LINE="$CRON_TIME $SCRIPT"

TMP="$(mktemp)"
crontab -l 2>/dev/null | grep -v "$MARKER" | grep -v "$SCRIPT" >"$TMP" || true
echo "$LINE $MARKER" >>"$TMP"
crontab "$TMP"
rm -f "$TMP"

echo "Installed daily refresh:"
crontab -l | grep firewatch-daily-refresh
echo "Logs: ${FIREWATCH_REFRESH_LOG:-/var/log/firewatch-refresh.log}"
