#!/usr/bin/env bash
# Deploy the Trading Bot web UI (python main.py --web) to Contabo.
#
# Serves: https://33ai.edos.uk  (Cloudflare DNS -> kamal-proxy -> trading-web.service)
#
# Usage:
#   ./deploy_to_contabo.sh
#
# Env overrides:
#   CONTABO_HOST     SSH alias/host (default: contabo-edos -> 89.117.58.19, user deploy)
#   CONTABO_KEY      SSH private key (default: ~/.ssh/contabo-edos)
#   REMOTE_APP_DIR   Remote app dir (default: /home/deploy/apps/trading)
#   SERVICE          systemd unit name (default: trading-web)
#   APP_HOST         Cloudflare subdomain host (default: 33ai)
#   DOMAIN           Cloudflare zone (default: edos.uk)
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

CONTABO_HOST="${CONTABO_HOST:-contabo-edos}"
CONTABO_KEY="${CONTABO_KEY:-$HOME/.ssh/contabo-edos}"
REMOTE_APP_DIR="${REMOTE_APP_DIR:-/home/deploy/apps/trading}"
SERVICE="${SERVICE:-trading-web}"
APP_HOST="${APP_HOST:-33ai}"
DOMAIN="${DOMAIN:-edos.uk}"
FQDN="${APP_HOST}.${DOMAIN}"
URL="https://${FQDN}"

SSH_OPTS=(
  -i "$CONTABO_KEY"
  -o IdentitiesOnly=yes
  -o BatchMode=yes
  -o PasswordAuthentication=no
  -o PreferredAuthentications=publickey
  -o StrictHostKeyChecking=accept-new
  -o ConnectTimeout=30
  -o ServerAliveInterval=15
)

ssh_r() { ssh "${SSH_OPTS[@]}" "$CONTABO_HOST" "$@"; }

need() { command -v "$1" >/dev/null 2>&1 || { echo "Missing: $1" >&2; exit 1; }; }
need rsync
need ssh
need curl

if [[ ! -f "$CONTABO_KEY" ]]; then
  echo "SSH key not found: $CONTABO_KEY" >&2
  exit 1
fi

echo "==> Cloudflare DNS check ($FQDN)"
resolved="$(getent hosts "$FQDN" | awk '{print $1}' | head -1 || true)"
if [[ -z "$resolved" ]]; then
  echo "WARNING: $FQDN does not resolve — create an A record in Cloudflare for $APP_HOST.$DOMAIN." >&2
else
  echo "    $FQDN -> $resolved"
fi

echo "==> Syncing code -> ${CONTABO_HOST}:${REMOTE_APP_DIR}"
RSYNC_SSH="ssh ${SSH_OPTS[*]}"
rsync -az \
  -e "$RSYNC_SSH" \
  --exclude '.git' \
  --exclude '.venv' \
  --exclude '__pycache__' \
  --exclude '.pytest_cache' \
  --exclude '.ruff_cache' \
  --exclude '.mypy_cache' \
  --exclude '.env' \
  --exclude 'charts' \
  --exclude 'logs' \
  --exclude 'data/cache' \
  --exclude 'graphify-out' \
  --exclude '*.pyc' \
  "$ROOT_DIR/" "$CONTABO_HOST:${REMOTE_APP_DIR}/"

echo "==> Remote: venv + deps + restart ${SERVICE}"
ssh_r bash -s <<REMOTE
set -euo pipefail
cd "$REMOTE_APP_DIR"

if [[ ! -x .venv/bin/python ]]; then
  echo "    creating venv"
  python3 -m venv .venv
fi

.venv/bin/python -m pip install --quiet --upgrade pip
.venv/bin/python -m pip install --quiet -r requirements.txt

if ! grep -qE '^WEB_UI_PASSWORD=.+' .env 2>/dev/null; then
  echo "WARNING: WEB_UI_PASSWORD not set in $REMOTE_APP_DIR/.env — the web UI will refuse to bind." >&2
fi

sudo systemctl restart "$SERVICE"
sudo systemctl is-active "$SERVICE" >/dev/null
echo "    $SERVICE restarted and active"
REMOTE

echo "==> Health check $URL/health"
ok=0
for _ in $(seq 1 30); do
  if curl -fsS --max-time 10 "$URL/health" >/dev/null 2>&1; then
    ok=1
    break
  fi
  sleep 2
done
if [[ $ok -ne 1 ]]; then
  echo "WARNING: $URL/health not responding yet — check: ssh $CONTABO_HOST 'sudo journalctl -u $SERVICE -n 50'" >&2
  exit 1
fi

echo
echo "Deployed: $URL"
