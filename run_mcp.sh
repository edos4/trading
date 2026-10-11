#!/usr/bin/env bash
# run_mcp.sh — launch the stdio MCP server so an AI agent can operate the bot.
#
# stdout is the MCP JSON-RPC channel, so this script must never write to
# stdout. It cd's to the repo root first, so `.env` and repo-relative paths
# resolve no matter which directory the MCP client spawns it from.
#
# Prereq: the web UI must be running (`python main.py --web` / ./run_web.sh) —
# the MCP server proxies to its /api surface.
#
# Usage:
#   ./run_mcp.sh                 # normal stdio server (run by the MCP client)
#   ./run_mcp.sh --help          # show this header
set -euo pipefail

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  sed -n '2,15p' "$0"
  exit 0
fi

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

PY="${ROOT_DIR}/.venv/bin/python"
if [[ ! -x "$PY" ]]; then
  echo "run_mcp.sh: $PY not found. Create the venv and install deps:" >&2
  echo "  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
  exit 1
fi

exec "$PY" -m mcp_server
