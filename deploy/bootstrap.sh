#!/usr/bin/env bash
# First boot on the VPS: schema, then optional restore of a local dump.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

COMPOSE=(docker compose -f docker-compose.prod.yml --env-file .env.production)

"${COMPOSE[@]}" up -d db
"${COMPOSE[@]}" run --rm api python -m firewatch initdb

DUMP="${1:-}"
if [[ -n "$DUMP" ]]; then
  echo "Restoring $DUMP"
  "${COMPOSE[@]}" exec -T db pg_restore \
    -U "${POSTGRES_USER:-firewatch}" \
    -d "${POSTGRES_DB:-firewatch}" \
    --clean --if-exists --no-owner \
    < "$DUMP"
fi

"${COMPOSE[@]}" up -d
echo "Fire Watch is up. Point GoDaddy A records here, then open https://${DOMAIN:-yellowducklabs.org}"
