#!/bin/bash
source .venv/bin/activate

seconds_to_hms() {
  local total=$1
  local hours=$(( total / 3600 ))
  local minutes=$(( (total % 3600) / 60 ))
  local seconds=$(( total % 60 ))
  printf "%02d:%02d:%02d\n" "$hours" "$minutes" "$seconds"
}

# Reset the timer
SECONDS=0

python main.py --paper --paper-reset --symbols=500 --pattern-only --volume-gate --collect-first=4 --stream=01/05/2026 --duration-days=180 --export-trades-log=output_trades.json

duration=$(seconds_to_hms $SECONDS)
echo "Command finished in $duration"
