#!/usr/bin/env bash
# setup_deploy_tunnel.sh — one-time Cloudflare wiring for the deploy gateway.
#
# Creates (idempotently) the tunnel, its ingress rule and the DNS record for
# $DEPLOY_TUNNEL_HOSTNAME, then prints the tunnel token for .env. Requires an
# authenticated `cf` CLI (`cf auth login` once).
#
#   scripts/setup_deploy_tunnel.sh
#   scripts/setup_deploy_tunnel.sh --dry-run   # show commands, change nothing
#
# Env (or .env):
#   DEPLOY_TUNNEL_NAME      tunnel name        (required)
#   DEPLOY_TUNNEL_HOSTNAME  public hostname    (required)
#   DEPLOY_ZONE             Cloudflare zone    (default: last two host labels)
#   DEPLOY_GATEWAY_PORT     local gateway port (default: 8099)
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

DRY_RUN=0
case "${1:-}" in
  --dry-run) DRY_RUN=1 ;;
  -h|--help) sed -n '2,17p' "$0"; exit 0 ;;
  "") ;;
  *) echo "Unknown option: $1 (try --help)" >&2; exit 2 ;;
esac

# cf lives under nvm; make it reachable from non-login shells.
if ! command -v cf >/dev/null 2>&1 && [[ -d "${HOME}/.nvm/versions/node" ]]; then
  for d in "${HOME}"/.nvm/versions/node/*/bin; do
    case ":${PATH}:" in *":${d}:"*) ;; *) PATH="${d}:${PATH}" ;; esac
  done
fi
need() { command -v "$1" >/dev/null 2>&1 || { echo "Missing: $1" >&2; exit 1; }; }
need cf
need python3

env_val() { { sed -n "s/^$1=//p" "$ROOT_DIR/.env" 2>/dev/null || true; } | tail -1; }
NAME="${DEPLOY_TUNNEL_NAME:-$(env_val DEPLOY_TUNNEL_NAME)}"
HOST="${DEPLOY_TUNNEL_HOSTNAME:-$(env_val DEPLOY_TUNNEL_HOSTNAME)}"
ZONE="${DEPLOY_ZONE:-$(env_val DEPLOY_ZONE)}"
PORT="${DEPLOY_GATEWAY_PORT:-$(env_val DEPLOY_GATEWAY_PORT)}"; PORT="${PORT:-8099}"

[[ -n "$NAME" ]] || { echo "Set DEPLOY_TUNNEL_NAME (tunnel name) in env or .env." >&2; exit 1; }
[[ -n "$HOST" ]] || { echo "Set DEPLOY_TUNNEL_HOSTNAME (public hostname) in env or .env." >&2; exit 1; }
if [[ -z "$ZONE" ]]; then
  ZONE="${HOST#*.}"   # 33ai-deploy.rootlabs.dev -> rootlabs.dev
  echo "DEPLOY_ZONE unset — derived '$ZONE' from $HOST (set DEPLOY_ZONE for multi-label TLDs)." >&2
fi

run() { if [[ "$DRY_RUN" -eq 1 ]]; then echo "+ $*"; else "$@"; fi; }

jq_py() { python3 -c "$1" "${@:2}"; }

echo "==> Account / tunnel"
cf auth whoami >/dev/null

echo "==> Tunnel '$NAME'"
tunnels_json="$(cf tunnels list 2>/dev/null || echo '[]')"
TUNNEL_ID="$(printf '%s' "$tunnels_json" | jq_py '
import json,sys
try: rows = json.load(sys.stdin)
except Exception: rows = []
print(next((t.get("id","") for t in rows if t.get("name")==sys.argv[1]), ""))
' "$NAME")"

if [[ -z "$TUNNEL_ID" ]]; then
  echo "    creating"
  if [[ "$DRY_RUN" -eq 1 ]]; then
    echo "+ cf tunnels create --name $NAME --config-src cloudflare"
    TUNNEL_ID="<new-tunnel-id>"
  else
    created="$(cf tunnels create --name "$NAME" --config-src cloudflare)"
    TUNNEL_ID="$(printf '%s' "$created" | jq_py '
import json,sys
raw = sys.stdin.read()
try: print(json.loads(raw).get("id",""))
except Exception: print("")
')"
    [[ -n "$TUNNEL_ID" ]] || { echo "Could not parse tunnel id from: $created" >&2; exit 1; }
  fi
else
  echo "    exists: $TUNNEL_ID"
fi

echo "==> Ingress  $HOST -> http://localhost:$PORT"
BODY="$(HOST="$HOST" PORT="$PORT" python3 -c '
import json, os
print(json.dumps({"config": {"ingress": [
    {"hostname": os.environ["HOST"], "service": "http://localhost:" + os.environ["PORT"]},
    {"service": "http_status:404"},
]}}))
')"
run cf tunnels config update "$TUNNEL_ID" --body "$BODY"

echo "==> DNS  $HOST"
records_json="$(cf dns records list -z "$ZONE" 2>/dev/null || echo '[]')"
host_exists="$(printf '%s' "$records_json" | jq_py '
import json,sys
try: rows = json.load(sys.stdin)
except Exception: rows = []
print("yes" if any(r.get("name")==sys.argv[1] for r in rows) else "no")
' "$HOST")"
if [[ "$host_exists" == "yes" ]]; then
  echo "    exists"
else
  DNS_BODY="$(HOST="$HOST" TID="$TUNNEL_ID" python3 -c '
import json, os
print(json.dumps({"type": "CNAME", "name": os.environ["HOST"],
                  "content": os.environ["TID"] + ".cfargotunnel.com",
                  "proxied": True, "ttl": 1}))
')"
  run cf dns records create -z "$ZONE" --body "$DNS_BODY"
fi

echo "==> Tunnel token"
if [[ "$DRY_RUN" -eq 1 ]]; then
  echo "    (skipped: --dry-run)"
  TOKEN="<tunnel-token>"
else
  TOKEN="$(cf tunnels token get "$TUNNEL_ID" | jq_py '
import json,sys
raw = sys.stdin.read()
try:
    v = json.loads(raw)
    print(v if isinstance(v, str) else (v.get("token","") or ""))
except Exception:
    print(raw.strip())
')"
fi

echo
echo "Public URL:  https://${HOST}   (tunnel ${TUNNEL_ID})"
echo
echo "Add these to $ROOT_DIR/.env (deploy gateway):"
echo "  DEPLOY_GATEWAY_HOST=127.0.0.1"
echo "  DEPLOY_GATEWAY_PORT=${PORT}"
if [[ -n "$(env_val DEPLOY_GATEWAY_TOKEN)" ]]; then
  echo "  DEPLOY_GATEWAY_TOKEN=<already set>"
else
  echo "  DEPLOY_GATEWAY_TOKEN=$(python3 -c 'import secrets;print(secrets.token_urlsafe(48))')"
fi
echo "  DEPLOY_TUNNEL_HOSTNAME=${HOST}"
echo "  DEPLOY_TUNNEL_TOKEN=${TOKEN}"
echo
echo "Then: scripts/install_deploy_service.sh"
