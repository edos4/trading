#!/usr/bin/env bash
# deploy_to_engine.sh — deploy this repo to the 33ai-engine deploy gateway
# (https://33ai-deploy.rootlabs.dev) from any computer. No SSH key needed.
#
# It pushes the current branch to origin, then asks the gateway to fetch and
# check out that exact commit, sync deps and restart the web service there.
#
# Usage:
#   DEPLOY_TOKEN=... ./deploy_to_engine.sh                 # push + deploy current branch
#   DEPLOY_TOKEN=... ./deploy_to_engine.sh --ref main
#   DEPLOY_TOKEN=... ./deploy_to_engine.sh --no-push       # deploy an already-pushed commit
#   DEPLOY_TOKEN=... ./deploy_to_engine.sh --force         # allow a dirty tree on the engine
#
# Env (env var overrides .env):
#   DEPLOY_URL    gateway base URL (default: https://33ai-deploy.rootlabs.dev)
#   DEPLOY_TOKEN  the gateway's DEPLOY_GATEWAY_TOKEN (required)
set -euo pipefail

ROOT_DIR="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
cd "$ROOT_DIR"

REF=""
NO_PUSH=0
FORCE=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --ref) REF="${2:-}"; shift 2 ;;
    --ref=*) REF="${1#*=}"; shift ;;
    --no-push) NO_PUSH=1; shift ;;
    --force) FORCE=1; shift ;;
    -h|--help) sed -n '2,19p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1 (try --help)" >&2; exit 2 ;;
  esac
done

need() { command -v "$1" >/dev/null 2>&1 || { echo "Missing: $1" >&2; exit 1; }; }
need git
need curl

env_val() { { sed -n "s/^$1=//p" "$ROOT_DIR/.env" 2>/dev/null || true; } | tail -1; }
URL="${DEPLOY_URL:-$(env_val DEPLOY_URL)}"; URL="${URL:-https://33ai-deploy.rootlabs.dev}"
TOKEN="${DEPLOY_TOKEN:-$(env_val DEPLOY_TOKEN)}"
if [[ -z "$TOKEN" ]]; then
  echo "DEPLOY_TOKEN is not set (env or .env). Copy the engine's DEPLOY_GATEWAY_TOKEN." >&2
  exit 1
fi

if [[ -z "$REF" ]]; then
  REF="$(git rev-parse --abbrev-ref HEAD)"
fi
if [[ "$REF" == "HEAD" ]]; then
  echo "Detached HEAD — pass --ref=<branch>." >&2
  exit 1
fi

if [[ "$NO_PUSH" -eq 0 ]]; then
  echo "==> git push origin HEAD:refs/heads/${REF}"
  git push origin "HEAD:refs/heads/${REF}"
fi

SHA="$(git rev-parse HEAD)"
echo "==> Deploy ${REF} @ ${SHA:0:12} -> ${URL}"

payload="{\"ref\":\"${REF}\",\"commit\":\"${SHA}\",\"force\":$([[ "$FORCE" -eq 1 ]] && echo true || echo false)}"

resp="$(curl -fsS -X POST "${URL%/}/deploy" \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Content-Type: application/json" \
  -d "$payload")" || {
  echo "Deploy request failed." >&2
  exit 1
}

if command -v python3 >/dev/null 2>&1; then
  printf '%s' "$resp" | python3 -m json.tool 2>/dev/null || printf '%s\n' "$resp"
else
  printf '%s\n' "$resp"
fi
