# AGENTS.md

Trading bot for US + Philippine (PSE) markets: detects chart patterns,
runs paper/backtest engines, and exposes the bot through a web UI, a
desktop tkinter app, and a stdio MCP server for AI agents.

## Tech stack
- Python 3.14 (venv `.venv`); deps in requirements.txt.
- FastAPI + uvicorn (web), tkinter (desktop), mcp (stdio server),
  psycopg (PostgreSQL), pydantic-settings (.env config).
- No linter/formatter is configured or in requirements; do not add one.

## Folder map
- `core/` — engines: backtester, paper trader, scanner, rules,
  Kronos/Laya stores. `test_*.py` live here, not in `tests/`.
- `web/` — FastAPI app, templates/static, tests.
- `mcp_server/` — MCP tools (tools_*.py).
- `ui/` — tkinter desktop. `patterns/`, `patterns_v2/` — detectors
  (each has a paired .md doc). `scripts/` — one-off setup/DB scripts.
- `deploy/` — systemd units. `docs/` — design docs. `tests/` — shared tests.

## Commands
- Setup: `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`
- Web: `python main.py --web` (or `./run_web.sh`); MCP: `./run_mcp.sh`.
- Scanner: `python main.py --scan`; paper: `--paper`;
  backtest: `--backtest 100`; desktop UI: `--ui`;
  DB: `--check-db`, `--update-db`, `--update-hourly`.
- Test: `.venv/bin/pytest` (all tests); skip slow: `-m "not slow"`.

## Conventions (observed)
- `from __future__ import annotations`; type hints on public functions.
- Dotted module paths in imports; module docstrings; frozen
  pydantic-settings `config.py`; `loguru` logging; .env config.
- Laya runs in isolated `.venv-laya` (requirements-laya.txt) —
  never add laya[mcp] to the main venv.

## Do not touch
- `.env`, `data/` (gitignored runtime state), `/pattern_versions/`
  (immutable registry), `deploy/` units, `scripts/*_daily.sh` cron
  blocks, and gitignored `backtest_*.json` artifacts.
