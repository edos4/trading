#!/usr/bin/env bash
# Install/refresh the user systemd service that keeps the Hermes Agent web
# dashboard running across restarts, so the trading UI's /hermes tab always has
# something to proxy. Idempotent.
#
#   scripts/install_hermes_service.sh              # install + enable + start
#   scripts/install_hermes_service.sh --uninstall  # stop + remove
#   scripts/install_hermes_service.sh --status
#
# Runs as a user unit, so no root is needed. `loginctl enable-linger` makes it
# start at boot without an interactive login.
#
# The dashboard is bound to loopback and left unauthenticated: the trading UI
# already requires a login before it proxies /hermes. Host-specific values
# (binary path, bind host/port, tool PATH) are discovered here and rendered into
# the unit — nothing is baked into the committed template.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT="hermes-dashboard.service"
UNIT_SRC="$ROOT_DIR/deploy/$UNIT"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
HERMES_HOME_DIR="${HERMES_HOME:-$HOME/.hermes}"

case "${1:-}" in
  --uninstall)
    systemctl --user disable --now "$UNIT" 2>/dev/null || true
    rm -f "$UNIT_DIR/$UNIT"
    systemctl --user daemon-reload
    echo "Removed $UNIT (linger left enabled)."
    exit 0
    ;;
  --status)
    systemctl --user --no-pager status "$UNIT" || true
    echo
    echo "linger: $(loginctl show-user "$USER" -p Linger --value 2>/dev/null || echo unknown)"
    exit 0
    ;;
  -h|--help)
    sed -n '2,15p' "$0"
    exit 0
    ;;
  "") ;;
  *) echo "Unknown option: $1 (try --help)" >&2; exit 2 ;;
esac

[[ -f "$UNIT_SRC" ]] || { echo "missing $UNIT_SRC" >&2; exit 1; }

HERMES_BIN="$(command -v hermes 2>/dev/null || true)"
if [[ -z "$HERMES_BIN" ]]; then
  echo "ERROR: 'hermes' is not on PATH — install Hermes Agent first." >&2
  exit 1
fi

# Keep the bind in step with what the UI proxies (HERMES_DASHBOARD_URL).
URL="$(sed -n 's/^HERMES_DASHBOARD_URL=//p' "$ROOT_DIR/.env" 2>/dev/null | tail -1)"
if [[ -z "$URL" ]]; then
  echo "WARNING: HERMES_DASHBOARD_URL is empty in $ROOT_DIR/.env —" >&2
  echo "         the Hermes tab stays hidden until it is set (default: http://127.0.0.1:9119)." >&2
  URL="http://127.0.0.1:9119"
fi
hostport="${URL#*://}"; hostport="${hostport%%/*}"
HERMES_HOST="${hostport%%:*}"
HERMES_PORT="${hostport##*:}"
[[ -n "$HERMES_HOST" ]] || HERMES_HOST=127.0.0.1
case "$HERMES_PORT" in ''|*[!0-9]*) HERMES_PORT=9119 ;; esac

# Hermes' managed tools (node for the Chat TUI, ffmpeg, …) plus its own bin dir.
HERMES_PATH="$(dirname "$HERMES_BIN")"
for d in "$HERMES_HOME_DIR"/tools/*/bin; do
  [[ -d "$d" ]] && HERMES_PATH="$HERMES_PATH:$d"
done
HERMES_PATH="$HERMES_PATH:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

mkdir -p "$UNIT_DIR"
# Render the template for this host — no host-specific paths are baked in.
sed -e "s|@ROOT_DIR@|$ROOT_DIR|g" \
    -e "s|@HERMES_BIN@|$HERMES_BIN|g" \
    -e "s|@HERMES_HOST@|$HERMES_HOST|g" \
    -e "s|@HERMES_PORT@|$HERMES_PORT|g" \
    -e "s|@HERMES_PATH@|$HERMES_PATH|g" \
    "$UNIT_SRC" > "$UNIT_DIR/$UNIT"
chmod 0644 "$UNIT_DIR/$UNIT"

# Start at boot with no interactive login session.
if ! loginctl show-user "$USER" -p Linger --value 2>/dev/null | grep -q yes; then
  loginctl enable-linger "$USER" 2>/dev/null \
    || echo "WARNING: could not enable linger — the service will only start after login." >&2
fi

systemctl --user daemon-reload
systemctl --user enable --now "$UNIT"

echo
echo "Installed and started $UNIT → http://${HERMES_HOST}:${HERMES_PORT} (starts on boot; linger=$(loginctl show-user "$USER" -p Linger --value 2>/dev/null))."
systemctl --user --no-pager status "$UNIT" | head -14 || true
