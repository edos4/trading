"""DB maintenance tools — called in-process (not in the web API), default-deny.

These touch local Postgres directly and are stateless with respect to the web
process, so they do not proxy. Gated behind MCP_ALLOW_MAINTENANCE.
"""

from __future__ import annotations

import contextlib
import io
from typing import Optional

from mcp_server import config as mcp_config


def db_check(stale_days: int = 7, market: Optional[str] = None) -> dict:
    """Check stock-history freshness; returns {"exit_code", "report"}.

    Non-zero exit_code means a market is stale (last bar older than stale_days).
    market is "us" or "ph" (omitted = both).
    """
    mcp_config.require_maintenance()
    from data.check import run_check

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = run_check(stale_days=stale_days, market=market)
    return {"exit_code": int(rc), "report": buf.getvalue()}


def db_update(fetch: bool = True, fetch_limit: Optional[int] = None) -> dict:
    """Incremental daily update: fetch + upsert stale symbols (destructive).

    Writes Postgres. Unbounded unless fetch_limit caps the top-N stale symbols.
    """
    mcp_config.require_maintenance()
    from data.update import run_update

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        run_update(fetch=fetch, fetch_limit=fetch_limit)
    return {"ok": True, "report": buf.getvalue()}
