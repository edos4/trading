#!/usr/bin/env bash
# run_dsh.sh — run the DeepSeek Harness (dsh) Web UI and publish its launch token.
#
# DeepSeek Harness (https://github.com/deepseek-ai/deepseek-harness) is an agent
# harness whose browser UI the trading web app reverse-proxies same-origin at
# /deepseek (see web/deepseek_proxy.py, docs/deepseek-harness.md). This script
# is the harness's supervisor:
#
#   * it launches `dsh web` bound to loopback with the authority browsers use
#     named as a trusted host (the harness fences every request on Host/Origin
#     and would otherwise answer 403 for the public hostname);
#   * it captures the per-process launch token from the startup line into a
#     0600 file, so the /deepseek tab can sign a browser in without anyone
#     handling the tokenized URL;
#   * it runs in the repo root, which the harness uses as its default workspace
#     and whose .env supplies DEEPSEEK_API_KEY (the harness reads the invoking
#     directory's .env as a credential layer, so the key stays in one place).
#
# The token file is truncated on start: a token from a previous process is dead
# the moment this one boots, and replaying it would only produce a 401.
#
# Usage:
#   ./run_dsh.sh                  # local, loopback only (default PORT 3080)
#   ./run_dsh.sh --help
#
# Settings (env overrides .env; see .env.example):
#   DEEPSEEK_HARNESS_URL          public/loopback URL recorded for the web app
#   DEEPSEEK_HARNESS_TOKEN_FILE   where to write the launch token
#   DSH_HOME                      harness home (profiles, credentials, logs)
#   DSH_BIN                       dsh executable            (default: PATH, then ~/.local/bin)
#   DSH_PORT                      listen port               (default 3080)
#   DSH_PUBLIC_URL                advertised browser root   (default: https://$CLOUDFLARE_TUNNEL_HOSTNAME/deepseek/)
#   DSH_INSERT_NODE_PATH          extra bin dir for node/npm (default: ~/.nvm, ~/.hermes/tools)
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  sed -n '2,34p' "$0"
  exit 0
fi

env_val() { { sed -n "s/^$1=//p" .env 2>/dev/null || true; } | tail -1; }

# node is not on a systemd service's PATH: add a bundled one if the shell has
# none, so `dsh` (a Node CLI) can start.
if ! command -v node >/dev/null 2>&1; then
  for d in "${HOME}"/.nvm/versions/node/*/bin "${HOME}"/.hermes/tools/node-*/bin; do
    [[ -x "${d}/node" ]] || continue
    case ":${PATH}:" in *":${d}:"*) ;; *) PATH="${d}:${PATH}" ;; esac
  done
  export PATH
fi

DSH="${DSH_BIN:-}"
if [[ -z "$DSH" ]]; then
  DSH="$(command -v dsh || true)"
fi
if [[ -z "$DSH" && -x "${HOME}/.local/bin/dsh" ]]; then
  DSH="${HOME}/.local/bin/dsh"
fi
if [[ -z "$DSH" || ! -x "$DSH" ]]; then
  echo "run_dsh.sh: 'dsh' not found. Install it with:" >&2
  echo "  npm install --global --prefix ~/.local @deepseek-ai/dsh" >&2
  exit 1
fi

export DSH_HOME="${DSH_HOME:-${HOME}/.local/share/deepseek-harness/home}"
PORT="${DSH_PORT:-3080}"
TOKEN_FILE="${DEEPSEEK_HARNESS_TOKEN_FILE:-$(env_val DEEPSEEK_HARNESS_TOKEN_FILE)}"
TOKEN_FILE="${TOKEN_FILE:-${HOME}/.local/state/deepseek-harness/token}"
PUBLIC_URL="${DSH_PUBLIC_URL:-}"
if [[ -z "$PUBLIC_URL" ]]; then
  PUBLIC_HOST="${CLOUDFLARE_TUNNEL_HOSTNAME:-$(env_val CLOUDFLARE_TUNNEL_HOSTNAME)}"
  [[ -n "$PUBLIC_HOST" ]] && PUBLIC_URL="https://${PUBLIC_HOST}/deepseek/"
fi

args=(web --no-open --host 127.0.0.1 --port "$PORT")
trusted=()
if [[ -n "$PUBLIC_URL" ]]; then
  # Advertise the address browsers actually use; the fence needs it separately.
  args+=(--public-url "$PUBLIC_URL")
  host="${PUBLIC_URL#*://}"; host="${host%%/*}"; host="${host%%:*}"
  [[ -n "$host" ]] && trusted+=(--trusted-host "$host")
fi
trusted+=(--trusted-host localhost)
args+=("${trusted[@]}")

mkdir -p "$(dirname "$TOKEN_FILE")" "$DSH_HOME"
umask 077
: >"$TOKEN_FILE"

echo "==> DeepSeek Harness  http://127.0.0.1:${PORT}  (DSH_HOME=${DSH_HOME})"
[[ -n "$PUBLIC_URL" ]] && echo "==> advertised root     ${PUBLIC_URL}"
echo "==> launch token file   ${TOKEN_FILE}"

# The startup readiness line carries the launch token; capture it and pass
# every line through so the journal keeps the harness's own output.
set -o pipefail
"$DSH" "${args[@]}" 2>&1 | while IFS= read -r line; do
  printf '%s\n' "$line" >&2
  case "$line" in
    *"token="*)
      token="$(printf '%s' "$line" | sed -n 's/.*[?&]token=\([^&[:space:]]*\).*/\1/p')"
      if [[ -n "$token" ]]; then
        printf '%s' "$token" >"${TOKEN_FILE}.tmp"
        mv "${TOKEN_FILE}.tmp" "$TOKEN_FILE"
        echo "==> launch token captured" >&2
      fi
      ;;
  esac
done
