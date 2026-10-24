#!/usr/bin/env bash
# run_web.sh — run the authenticated web UI.
#
# By default this is a plain local server (http://localhost:${WEB_UI_PORT:-8080}).
# If CLOUDFLARE_TUNNEL_TOKEN and CLOUDFLARE_TUNNEL_HOSTNAME are set in .env, the
# UI is additionally exposed at https://${CLOUDFLARE_TUNNEL_HOSTNAME} through a
# Cloudflare Tunnel (driven by the `cf` CLI — `cf auth login` once). That is
# opt-in and host-specific, so a clone without those values stays local-only.
#
# Usage:
#   ./run_web.sh                  # local; + tunnel when .env opts in
#   ./run_web.sh --no-tunnel      # force local-only (ignore .env tunnel)
#   ./run_web.sh --require-tunnel # fail (exit 1) if the tunnel can't start;
#                                 # used by the systemd service (deploy/)
#
# Tunnel settings come from .env (see .env.example):
#   CLOUDFLARE_TUNNEL_TOKEN, CLOUDFLARE_TUNNEL_HOSTNAME
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

NO_TUNNEL=0
REQUIRED=0
for arg in "$@"; do
  case "$arg" in
    --no-tunnel) NO_TUNNEL=1 ;;
    --require-tunnel) REQUIRED=1 ;;
    -h|--help) sed -n '2,17p' "$0"; exit 0 ;;
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
PORT="${WEB_UI_PORT:-$(env_val WEB_UI_PORT)}"; PORT="${PORT:-8080}"
TOKEN="${CLOUDFLARE_TUNNEL_TOKEN:-$(env_val CLOUDFLARE_TUNNEL_TOKEN)}"
PUBLIC_HOST="${CLOUDFLARE_TUNNEL_HOSTNAME:-$(env_val CLOUDFLARE_TUNNEL_HOSTNAME)}"

run_local() {
  echo "==> Web UI  http://0.0.0.0:${PORT}"
  exec python main.py --web
}

# The tunnel is strictly opt-in: no token in .env means a plain local server,
# so a clone on another machine never touches Cloudflare.
if [[ "$NO_TUNNEL" -eq 1 ]]; then
  run_local
fi

if [[ -z "$TOKEN" ]]; then
  if [[ "$REQUIRED" -eq 1 ]]; then
    echo "ERROR: --require-tunnel but CLOUDFLARE_TUNNEL_TOKEN is empty in .env." >&2
    exit 1
  fi
  run_local
fi

if ! command -v cf >/dev/null 2>&1; then
  echo "'cf' (Cloudflare CLI) not found — install it, then run: cf auth login" >&2
  if [[ "$REQUIRED" -eq 1 ]]; then
    exit 1
  fi
  echo "Falling back to local-only; no public URL will be served." >&2
  run_local
fi

if [[ -z "$PUBLIC_HOST" ]]; then
  echo "CLOUDFLARE_TUNNEL_TOKEN is set but CLOUDFLARE_TUNNEL_HOSTNAME is empty in .env." >&2
  if [[ "$REQUIRED" -eq 1 ]]; then
    exit 1
  fi
  echo "Falling back to local-only; no public URL will be served." >&2
  run_local
fi

pids=()
cleanup() {
  trap - EXIT INT TERM
  for pid in "${pids[@]}"; do kill "$pid" 2>/dev/null || true; done
}
trap cleanup EXIT INT TERM

echo "==> Web UI  http://0.0.0.0:${PORT}"
python main.py --web &
APP_PID=$!
pids+=("$APP_PID")

if [[ "$PORT" != "8080" ]]; then
  echo "WARNING: WEB_UI_PORT=${PORT}, but the tunnel ingress targets :8080." >&2
  echo "         Update it: cf tunnels config update <tunnel-id> --body '...'" >&2
fi

# Don't expose the tunnel until the app is actually answering.
ready=0
for _ in $(seq 1 30); do
  if curl -fsS --max-time 2 "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then
    ready=1
    break
  fi
  if ! kill -0 "$APP_PID" 2>/dev/null; then
    echo "Web UI exited during startup." >&2
    exit 1
  fi
  sleep 1
done

if [[ "$ready" -ne 1 ]]; then
  echo "ERROR: Web UI did not become healthy; refusing to start the tunnel." >&2
  exit 1
fi

echo "==> Cloudflare Tunnel  https://${PUBLIC_HOST}"
cf tunnels run --token "$TOKEN" --log-level info &
TUN_PID=$!
pids+=("$TUN_PID")

# Run until either process exits; the EXIT trap stops the survivor.
wait -n "$APP_PID" "$TUN_PID"
