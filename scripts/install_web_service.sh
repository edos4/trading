#!/usr/bin/env bash
# Install/refresh the user systemd service that keeps the web UI + Cloudflare
# Tunnel (run_web.sh) running across restarts. Idempotent.
#
#   scripts/install_web_service.sh              # install + enable + start
#   scripts/install_web_service.sh --uninstall  # stop + remove
#   scripts/install_web_service.sh --status
#
# Runs as a user unit, so no root is needed. `loginctl enable-linger` makes it
# start at boot without an interactive login.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT="trading-web.service"
UNIT_SRC="$ROOT_DIR/deploy/$UNIT"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

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
    sed -n '2,10p' "$0"
    exit 0
    ;;
  "") ;;
  *) echo "Unknown option: $1 (try --help)" >&2; exit 2 ;;
esac

[[ -f "$UNIT_SRC" ]] || { echo "missing $UNIT_SRC" >&2; exit 1; }

mkdir -p "$UNIT_DIR"
# Render the template for this checkout — no host-specific paths are baked in.
sed "s|@ROOT_DIR@|$ROOT_DIR|g" "$UNIT_SRC" > "$UNIT_DIR/$UNIT"
chmod 0644 "$UNIT_DIR/$UNIT"

# Start at boot with no interactive login session.
if ! loginctl show-user "$USER" -p Linger --value 2>/dev/null | grep -q yes; then
  loginctl enable-linger "$USER" 2>/dev/null \
    || echo "WARNING: could not enable linger — the service will only start after login." >&2
fi

# A token is required for the tunnel; warn now rather than crash-loop later.
if ! { sed -n 's/^CLOUDFLARE_TUNNEL_TOKEN=//p' "$ROOT_DIR/.env" 2>/dev/null | tail -1 | grep -q .; }; then
  echo "WARNING: CLOUDFLARE_TUNNEL_TOKEN is empty in $ROOT_DIR/.env —" >&2
  echo "         the service will fail with --require-tunnel until it is set." >&2
fi

systemctl --user daemon-reload
systemctl --user enable --now "$UNIT"

echo
echo "Installed and started $UNIT (starts automatically on boot; linger=$(loginctl show-user "$USER" -p Linger --value 2>/dev/null))."
systemctl --user --no-pager status "$UNIT" | head -14 || true
