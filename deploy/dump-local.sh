#!/usr/bin/env bash
# Snapshot the local Docker database so production can restore it.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="${1:-$ROOT/deploy/firewatch.dump}"

docker compose -f "$ROOT/docker-compose.yml" exec -T db \
  pg_dump -U firewatch -Fc firewatch > "$OUT"

echo "Wrote $OUT ($(wc -c < "$OUT") bytes)"
