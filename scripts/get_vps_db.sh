#!/usr/bin/env bash
# Dump the Contabo `stocks_history` PostgreSQL database to a local file.
#
# The database lives in the docker container `trading-postgres` on the VPS
# (127.0.0.1:5437). This streams `pg_dump` from inside that container over SSH
# to a local file, so no local PostgreSQL client or tunnel is required.
#
# Usage:
#   scripts/get_vps_db.sh [OUTPUT] [--schema-only] [--format plain|custom]
#
# Defaults to ./stocks_history_<UTC timestamp>.sql.gz (gzip-compressed plain SQL).
# Restore plain:   gunzip -c FILE.sql.gz | psql -d stocks_history
# Restore custom:  pg_restore --dbname=stocks_history FILE.dump
#
# Env overrides:
#   VPS_HOST    SSH alias/host (default: contabo-edos)
#   VPS_KEY     SSH private key (default: ~/.ssh/contabo-edos)
#   CONTAINER   docker container (default: trading-postgres)
#   DB_NAME     database (default: stocks_history)
#   DB_USER     role (default: trading)
set -euo pipefail

VPS_HOST=${VPS_HOST:-contabo-edos}
VPS_KEY=${VPS_KEY:-$HOME/.ssh/contabo-edos}
CONTAINER=${CONTAINER:-trading-postgres}
DB_NAME=${DB_NAME:-stocks_history}
DB_USER=${DB_USER:-trading}

FORMAT=plain
SCHEMA_ONLY=0
OUTPUT=""

usage() {
  sed -n '3,20p' "$0" | sed 's/^# \{0,1\}//'
  exit "${1:-2}"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --format) FORMAT=${2:?--format needs plain|custom}; shift 2 ;;
    --schema-only) SCHEMA_ONLY=1; shift ;;
    -h|--help) usage 0 ;;
    -*) echo "unknown option: $1" >&2; usage ;;
    *) OUTPUT=$1; shift ;;
  esac
done

case "$FORMAT" in
  plain|custom) ;;
  *) echo "invalid --format: $FORMAT (expected plain|custom)" >&2; exit 2 ;;
esac

need() { command -v "$1" >/dev/null 2>&1 || { echo "Missing: $1" >&2; exit 1; }; }
need ssh
need gzip
[[ -f "$VPS_KEY" ]] || { echo "SSH key not found: $VPS_KEY" >&2; exit 1; }

if [[ -z "$OUTPUT" ]]; then
  stamp=$(date -u +%Y%m%dT%H%M%SZ)
  if [[ "$FORMAT" == custom ]]; then
    OUTPUT="stocks_history_${stamp}.dump"
  else
    OUTPUT="stocks_history_${stamp}.sql.gz"
  fi
fi

SSH_OPTS=(
  -i "$VPS_KEY"
  -o IdentitiesOnly=yes
  -o BatchMode=yes
  -o PasswordAuthentication=no
  -o PreferredAuthentications=publickey
  -o StrictHostKeyChecking=accept-new
  -o ConnectTimeout=30
  -o ServerAliveInterval=15
)

PG_ARGS=(-U "$DB_USER" -d "$DB_NAME" --no-owner --no-privileges)
[[ "$SCHEMA_ONLY" == 1 ]] && PG_ARGS+=(--schema-only)
[[ "$FORMAT" == custom ]] && PG_ARGS+=(--format="custom")

tmp="${OUTPUT}.partial.$$"
trap 'rm -f "$tmp"' EXIT

mode="$FORMAT"
[[ "$SCHEMA_ONLY" == 1 ]] && mode="$mode, schema only"
echo "==> Dumping ${DB_NAME} from ${VPS_HOST}:${CONTAINER} (${mode})"

if [[ "$FORMAT" == custom ]]; then
  ssh "${SSH_OPTS[@]}" "$VPS_HOST" "docker exec -i $CONTAINER pg_dump ${PG_ARGS[*]}" > "$tmp"
else
  ssh "${SSH_OPTS[@]}" "$VPS_HOST" "docker exec -i $CONTAINER pg_dump ${PG_ARGS[*]}" | gzip -c > "$tmp"
fi

mv "$tmp" "$OUTPUT"
trap - EXIT
echo "==> Wrote $OUTPUT ($(du -h "$OUTPUT" | cut -f1))"
