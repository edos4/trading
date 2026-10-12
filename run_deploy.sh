#!/usr/bin/env bash
# run_deploy.sh — run the authenticated deploy gateway, exposed to the internet
# through a dedicated Cloudflare Tunnel (driven by the `cf` CLI).
#
#   Local:   http://127.0.0.1:${DEPLOY_GATEWAY_PORT:-8099}
#   Public:  https://${DEPLOY_TUNNEL_HOSTNAME}  (only when the tunnel token is set)
#
# Another computer posts a git ref here and this machine checks it out, syncs
# deps and restarts trading-web.service. See deploy_to_engine.sh (client).
#
# Usage:
#   ./run_deploy.sh                  # gateway + Cloudflare Tunnel
#   ./run_deploy.sh --no-tunnel      # gateway only (loopback)
#   ./run_deploy.sh --require-tunnel # fail (exit 1) if the tunnel can't start
#
# Settings come from .env (see .env.example):
#   DEPLOY_GATEWAY_TOKEN, DEPLOY_GATEWAY_HOST, DEPLOY_GATEWAY_PORT,
#   DEPLOY_TUNNEL_TOKEN, DEPLOY_TUNNEL_HOSTNAME
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

NO_TUNNEL=0
REQUIRED=0
for arg in "$@"; do
  case "$arg" in
    --no-tunnel) NO_TUNNEL=1 ;;
    --require-tunnel) REQUIRED=1 ;;
    -h|--help) sed -n '2,19p' "$0"; exit 0 ;;
    *) echo "Unknown option: $arg (try --help)" >&2; exit 2 ;;
  esac
done

# cf lives under nvm; make sure it is reachable from non-login shells (systemd).
if ! command -v cf >/dev/null 2>&1 && [[ -d "${HOME}/.nvm/versions/node" ]]; then
  for d in "${HOME}"/.nvm/versions/node/*/bin; do
    case ":${PATH}:" in *":${d}:"*) ;; *) PATH="${d}:${PATH}" ;; esac
  done
fi

# shellcheck disable=SC1091
source .venv/bin/activate

env_val() { { sed -n "s/^$1=//p" .env 2>/dev/null || true; } | tail -1; }
PORT="${DEPLOY_GATEWAY_PORT:-$(env_val DEPLOY_GATEWAY_PORT)}"; PORT="${PORT:-8099}"
TOKEN="${DEPLOY_TUNNEL_TOKEN:-$(env_val DEPLOY_TUNNEL_TOKEN)}"
PUBLIC_HOST="${DEPLOY_TUNNEL_HOSTNAME:-$(env_val DEPLOY_TUNNEL_HOSTNAME)}"

if [[ -z "${DEPLOY_GATEWAY_TOKEN:-$(env_val DEPLOY_GATEWAY_TOKEN)}" ]]; then
  echo "ERROR: DEPLOY_GATEWAY_TOKEN is empty in .env — the gateway refuses to bind." >&2
  exit 1
fi

# Decide up front whether the tunnel can run, so --require-tunnel fails fast
# (before spawning the app) instead of silently serving loopback-only. The
# public hostname is host-specific and comes from .env, never a baked default.
TUNNEL=1
if [[ "$NO_TUNNEL" -eq 1 ]]; then
  TUNNEL=0
  REQUIRED=0  # an explicit local-only run overrides --require-tunnel
elif [[ -z "$TOKEN" ]]; then
  TUNNEL=0
  if [[ "$REQUIRED" -eq 1 ]]; then
    echo "ERROR: --require-tunnel but DEPLOY_TUNNEL_TOKEN is empty in .env." >&2
    exit 1
  fi
  echo "DEPLOY_TUNNEL_TOKEN is empty in .env — the deploy gateway stays loopback-only." >&2
elif ! command -v cf >/dev/null 2>&1; then
  TUNNEL=0
  echo "'cf' (Cloudflare CLI) not found — the deploy gateway stays loopback-only." >&2
  if [[ "$REQUIRED" -eq 1 ]]; then
    exit 1
  fi
elif [[ -z "$PUBLIC_HOST" ]]; then
  TUNNEL=0
  echo "DEPLOY_TUNNEL_TOKEN is set but DEPLOY_TUNNEL_HOSTNAME is empty in .env." >&2
  if [[ "$REQUIRED" -eq 1 ]]; then
    exit 1
  fi
fi

pids=()
cleanup() {
  trap - EXIT INT TERM
  for pid in "${pids[@]}"; do kill "$pid" 2>/dev/null || true; done
}
trap cleanup EXIT INT TERM

echo "==> Deploy gateway  http://127.0.0.1:${PORT}"
python -m web.deploy_gateway &
APP_PID=$!
pids+=("$APP_PID")

if [[ "$TUNNEL" -eq 0 ]]; then
  wait "$APP_PID"
  exit $?
fi

if [[ "$PORT" != "8099" ]]; then
  echo "WARNING: DEPLOY_GATEWAY_PORT=${PORT}, but the tunnel ingress targets :8099." >&2
fi

# Don't expose the tunnel until the gateway is actually answering.
for _ in $(seq 1 30); do
  if curl -fsS --max-time 2 "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then
    break
  fi
  if ! kill -0 "$APP_PID" 2>/dev/null; then
    echo "Deploy gateway exited during startup." >&2
    exit 1
  fi
  sleep 1
done

echo "==> Cloudflare Tunnel  https://${PUBLIC_HOST}"
cf tunnels run --token "$TOKEN" --log-level info &
TUN_PID=$!
pids+=("$TUN_PID")

# Run until either process exits; the EXIT trap stops the survivor.
wait -n "$APP_PID" "$TUN_PID"
