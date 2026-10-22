#!/usr/bin/env bash
# Daily Experiment 2 automation for a fixed save window.
#
# Phase 1 (through end_date): grade pending rows, then forecast the tracked
# symbols for five days and store the D1/D5 rows.
# Phase 2 (grade tail, +10 days): keep grading pending rows daily so the final
# D5 anchors settle without anyone clicking "Refresh actuals".
# After the tail the script removes its own crontab block and exits.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="$ROOT/logs"
LOG="$LOG_DIR/kronos_experiment2.log"
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/kronos-experiment2"
END_FILE="$STATE_DIR/end_date"
GRADE_END_FILE="$STATE_DIR/grade_end_date"
SAVE_WINDOW_DAYS=7
GRADE_TAIL_DAYS=10
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

remove_cron_block() {
  local tmp
  tmp="$(mktemp)"
  crontab -l 2>/dev/null | awk '
    /^# kronos-experiment2 BEGIN/ {skip=1; next}
    /^# kronos-experiment2 END/ {skip=0; next}
    !skip {print}
  ' > "$tmp"
  crontab "$tmp"
  rm -f "$tmp"
}

today="$(TZ=America/New_York date +%F)"
end_date="$(cat "$END_FILE" 2>/dev/null || true)"
if [[ -z "$end_date" ]]; then
  end_date="$(TZ=America/New_York date -d "+${SAVE_WINDOW_DAYS} days" +%F)"
  printf '%s\n' "$end_date" > "$END_FILE"
fi
grade_end="$(cat "$GRADE_END_FILE" 2>/dev/null || true)"
if [[ -z "$grade_end" ]]; then
  grade_end="$(TZ=America/New_York date -d "${end_date} + ${GRADE_TAIL_DAYS} days" +%F)"
  printf '%s\n' "$grade_end" > "$GRADE_END_FILE"
fi

mode="${1:-}"

if [[ "$today" > "$grade_end" && "$mode" != "--check" ]]; then
  remove_cron_block
  printf '%s window and grading tail ended (%s); removed cron block\n' \
    "$(date -Is)" "$grade_end" >> "$LOG"
  exit 0
fi

# The crontab invokes this hourly (Debian cron evaluates schedules in host
# time). Keep only the invocation in the 17:00 America/New_York hour on a
# weekday; this is independent of the host timezone and handles EDT/EST.
if [[ "$mode" != "--check" && "$mode" != "--force" ]]; then
  et_hour="$(TZ=America/New_York date +%H)"
  et_dow="$(TZ=America/New_York date +%u)"
  if [[ "$et_hour" != "17" || "$et_dow" -gt 5 ]]; then
    exit 0
  fi
fi

user="$(env_value WEB_UI_USERNAME admin)"
password="$(env_value WEB_UI_PASSWORD)"
if [[ -z "$password" ]]; then
  printf '%s WEB_UI_PASSWORD is empty; cannot authenticate\n' "$(date -Is)" >> "$LOG"
  exit 1
fi

port="$(env_value WEB_UI_PORT 8080)"
base="http://127.0.0.1:${port}"
body="$(mktemp)"
trap 'rm -f "$body"' EXIT

if [[ "${1:-}" == "--check" ]]; then
  code="$(curl -sS -o /dev/null -w '%{http_code}' -u "$user:$password" \
    "$base/api/kronos/experiment2?limit=1")"
  printf 'auth check: HTTP %s\n' "$code"
  exit 0
fi

post() {
  local path="$1" code detail summary
  code="$(curl -sS --max-time 900 -o "$body" -w '%{http_code}' \
    -u "$user:$password" -X POST "$base$path" || true)"
  if [[ "$code" == "200" ]]; then
    summary="$(python3 - "$body" <<'PY'
import json, sys
with open(sys.argv[1], encoding="utf-8") as fh:
    data = json.load(fh)
saved = data.get("saved") or {}
if saved:
    print("saved %s rows for %s symbols, graded %s" % (
        saved.get("predictions", 0), len(saved.get("symbols", [])),
        data.get("graded_now", 0)))
else:
    print("graded %s" % data.get("graded_now", 0))
PY
)" || summary="ok (summary unavailable)"
    printf '%s OK %s %s\n' "$(date -Is)" "$path" "$summary" >> "$LOG"
    return 0
  fi
  detail="$(python3 - "$body" <<'PY'
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as fh:
        print(json.load(fh).get("detail", ""))
except Exception:
    pass
PY
)" || true
  printf '%s HTTP %s %s %s\n' "$(date -Is)" "$code" "$path" "$detail" >> "$LOG"
  return 1
}

# After the save window, only grade: the final D5 rows need several more sessions.
if [[ "$today" > "$end_date" ]]; then
  post /api/kronos/experiment2/refresh
  exit 0
fi

# Grade first so a forecast failure cannot leave settled rows pending.
post /api/kronos/experiment2/refresh || true
post /api/kronos/experiment2/run
