#!/usr/bin/env bash
# Refresh live sources and re-score both municipalities.
# Install on the VPS with deploy/install-daily-refresh.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [[ ! -f .env.production ]]; then
  echo "Missing .env.production in $ROOT" >&2
  exit 1
fi

COMPOSE=(docker compose -f docker-compose.prod.yml --env-file .env.production)
LOG="${FIREWATCH_REFRESH_LOG:-/var/log/firewatch-refresh.log}"

LIVE_SOURCES=(
  cwfis_hotspots
  cwfis_fire_weather_stations
  eccc_hourly_observations
  nasa_firms
)

MUNICIPALITIES=(west-vancouver kelowna)

{
  echo "=== $(date -Is) refresh start ==="

  for municipality in "${MUNICIPALITIES[@]}"; do
    echo "--- ingest $municipality (live sources) ---"
    "${COMPOSE[@]}" exec -T api python -m firewatch ingest \
      -m "$municipality" \
      --skip-boundary \
      --only "${LIVE_SOURCES[@]}"
  done

  for municipality in "${MUNICIPALITIES[@]}"; do
    echo "--- derive + score $municipality ---"
    "${COMPOSE[@]}" exec -T api python -m firewatch derive -m "$municipality"
    "${COMPOSE[@]}" exec -T api python -m firewatch score -m "$municipality"
  done

  echo "=== $(date -Is) refresh done ==="
} >>"$LOG" 2>&1
