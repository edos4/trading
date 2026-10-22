#!/usr/bin/env bash
# Daily Kronos tracked-set save for a fixed window.
#
# Reads WEB_UI_USERNAME / WEB_UI_PASSWORD / WEB_UI_PORT from the repo .env and
# POSTs /api/kronos/save. The window end is stored on first install/run; after
# that date the script removes its own crontab entry and exits.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="$ROOT/logs"
LOG="$LOG_DIR/kronos_daily_save.log"
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/kronos-daily-save"
END_FILE="$STATE_DIR/end_date"
mkdir -p "$LOG_DIR" "$STATE_DIR"

env_value() {
  local key="$1" fallback="${2:-}" value=""
  if [[ -f "$ROOT/.env" ]]; then
    value="$(grep -E "^[[:space:]]*${key}=" "$ROOT/.env" | tail -n 1 | cut -d= -f2- || true)"
  fi
  value="${value%$'\r'}"
  value="${value%\"}"; value="${value#\"}"
  value="${value%\'}"; value="${value#\'}"
  if [[ -z "$value" ]]; then printf '%s' "$fallback"; else printf '%s' "$value"; fi
}

today="$(date +%F)"
end_date="$(cat "$END_FILE" 2>/dev/null || true)"
if [[ -z "$end_date" ]]; then
  end_date="$(date -d '+7 days' +%F)"
  printf '%s\n' "$end_date" > "$END_FILE"
fi

if [[ "$today" > "$end_date" ]]; then
  tmp="$(mktemp)"
  {
    crontab -l 2>/dev/null || true
  } | grep -vF 'kronos_daily_save.sh' | grep -vF '# kronos-daily-save' > "$tmp" || true
  crontab "$tmp"
  rm -f "$tmp"
  printf '%s window ended (%s); removed cron entry\n' "$(date -Is)" "$end_date" >> "$LOG"
  exit 0
fi

user="$(env_value WEB_UI_USERNAME admin)"
password="$(env_value WEB_UI_PASSWORD)"
if [[ -z "$password" ]]; then
  printf '%s WEB_UI_PASSWORD is empty; cannot authenticate\n' "$(date -Is)" >> "$LOG"
  exit 1
fi

port="$(env_value WEB_UI_PORT 8080)"
days="${KRONOS_SAVE_DAYS:-$(env_value KRONOS_SAVE_DAYS 5)}"
base="http://127.0.0.1:${port}"
body="$(mktemp)"
trap 'rm -f "$body"' EXIT

if [[ "${1:-}" == "--check" ]]; then
  code="$(curl -sS -o /dev/null -w '%{http_code}' -u "$user:$password" \
    "$base/api/kronos/winrate")"
  printf 'auth check: HTTP %s\n' "$code"
  exit 0
fi

code="$(curl -sS --max-time 900 -o "$body" -w '%{http_code}' \
  -u "$user:$password" \
  -H 'Content-Type: application/json' \
  -d "{\"days\":${days}}" \
  "$base/api/kronos/save" || true)"

if [[ "$code" == "200" ]]; then
  summary="$(python3 -c '
import json, sys
with open(sys.argv[1], encoding="utf-8") as fh:
    saved = json.load(fh).get("saved", {})
print("%s predictions for %s symbols" % (
    saved.get("predictions", 0), len(saved.get("symbols", []))))
' "$body" 2>/dev/null || printf 'saved (summary unavailable)')"
  printf '%s OK %s\n' "$(date -Is)" "$summary" >> "$LOG"
else
  detail="$(python3 -c '
import json, sys
try:
    print(json.load(open(sys.argv[1], encoding="utf-8")).get("detail", ""))
except Exception:
    pass
' "$body" 2>/dev/null || true)"
  printf '%s HTTP %s %s\n' "$(date -Is)" "$code" "$detail" >> "$LOG"
  exit 1
fi
