"""Kronos forecast-history routes: save today's forecasts, show win rates, and
run Experiment 2's 5-day close-accuracy rows.

Registered by ``web/app.py`` next to the Tasks routes. Every route requires
login. Storage is PostgreSQL-backed; when the database is unavailable or
unmigrated the errors name ``scripts/setup_kronos_db.py`` and the rest of the
Kronos tab keeps working.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from fastapi import Depends, Request
from fastapi.responses import JSONResponse

from config import settings
from web.auth import require_login

log = logging.getLogger("web.kronos_history")

_SYMBOL_RE = re.compile(r"[A-Z0-9.\-^]{1,12}")
# Experiment 2 always forecasts five trading days and stores the D1/D5 rows.
EXPERIMENT_DAYS = 5


def _store():
    from core.kronos_store import open_kronos_store

    return open_kronos_store()


def _experiment_store():
    from core.kronos_experiment import open_experiment_store

    return open_experiment_store()


def _tracked_symbols():
    """Validate the configured tracked list, or return a ready error response."""
    from web import kronos

    symbols = list(getattr(settings, "kronos_symbols", None) or [])
    if not symbols:
        return None, JSONResponse(
            {"detail": "No tracked symbols configured (KRONOS_TRACKED_SYMBOLS "
                       "or WATCHLIST)."},
            status_code=400,
        )
    if len(symbols) > kronos.MAX_SYMBOLS:
        return None, JSONResponse(
            {"detail": f"Tracked symbols exceed the {kronos.MAX_SYMBOLS}-symbol "
                       "limit; reduce KRONOS_TRACKED_SYMBOLS."},
            status_code=400,
        )
    if any(not _SYMBOL_RE.fullmatch(symbol) for symbol in symbols):
        return None, JSONResponse(
            {"detail": "Tracked symbols contain invalid characters."},
            status_code=400,
        )
    return symbols, None


async def _json_body(request: Request) -> dict[str, Any]:
    try:
        data = await request.json()
    except Exception as exc:
        raise ValueError(f"Invalid JSON body: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError("JSON body must be an object")
    return data


def _failure(exc: Exception) -> JSONResponse:
    from core.kronos_db import (KronosDatabaseUnavailable, KronosError,
                                KronosMigrationRequired)

    if isinstance(exc, KronosMigrationRequired):
        return JSONResponse(
            {"detail": "Kronos storage is not migrated; run scripts/setup_kronos_db.py"},
            status_code=503,
        )
    if isinstance(exc, KronosDatabaseUnavailable):
        return JSONResponse(
            {"detail": "Kronos storage is unavailable; check the PostgreSQL connection"},
            status_code=503,
        )
    if isinstance(exc, KronosError):
        return JSONResponse({"detail": str(exc)}, status_code=422)
    log.exception("Kronos history | unexpected error")
    return JSONResponse({"detail": "Unexpected Kronos history error"}, status_code=500)


def register_kronos_history_routes(app, templates, ctx) -> None:
    @app.post("/api/kronos/save")
    async def api_kronos_save(request: Request, user: str = Depends(require_login)):
        from web import kronos

        try:
            body = await _json_body(request)
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)

        raw_days = body.get("days", getattr(settings, "kronos_save_days", 5))
        try:
            days = int(raw_days)
        except (TypeError, ValueError):
            return JSONResponse({"detail": "Days must be a whole number."}, status_code=400)
        if not 1 <= days <= kronos.MAX_DAYS:
            return JSONResponse(
                {"detail": f"Days must be between 1 and {kronos.MAX_DAYS}."},
                status_code=400,
            )

        symbols, error = _tracked_symbols()
        if error is not None:
            return error

        try:
            payload = await asyncio.to_thread(kronos.forecast, symbols, days)
        except kronos.KronosBusy:
            return JSONResponse(
                {"detail": "A Kronos forecast is already running — wait for it to finish."},
                status_code=409,
            )
        except kronos.KronosUnavailable as exc:
            return JSONResponse({"detail": str(exc)}, status_code=503)
        except kronos.KronosError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=502)
        except Exception:
            log.exception("Kronos history | forecast failed")
            return JSONResponse(
                {"detail": "Kronos forecast failed. Check server logs."}, status_code=500,
            )

        try:
            saved = await asyncio.to_thread(
                _store().save_payload, payload, saved_by=user)
        except Exception as exc:  # noqa: BLE001 - mapped to a stable error response
            return _failure(exc)
        return {**payload, "saved": saved}

    @app.post("/api/kronos/experiment2/run")
    async def api_kronos_experiment2_run(user: str = Depends(require_login)):
        from web import kronos

        symbols, error = _tracked_symbols()
        if error is not None:
            return error

        try:
            payload = await asyncio.to_thread(
                kronos.forecast, symbols, EXPERIMENT_DAYS)
        except kronos.KronosBusy:
            return JSONResponse(
                {"detail": "A Kronos forecast is already running — wait for it to finish."},
                status_code=409,
            )
        except kronos.KronosUnavailable as exc:
            return JSONResponse({"detail": str(exc)}, status_code=503)
        except kronos.KronosError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=502)
        except Exception:
            log.exception("Kronos Experiment 2 | forecast failed")
            return JSONResponse(
                {"detail": "Kronos forecast failed. Check server logs."}, status_code=500,
            )

        try:
            store = _experiment_store()
            saved = await asyncio.to_thread(
                store.save_payload, payload, saved_by=user)
            graded_now = await asyncio.to_thread(store.grade)
            summary = await asyncio.to_thread(store.summary)
        except Exception as exc:  # noqa: BLE001 - mapped to a stable error response
            return _failure(exc)
        return {"days": EXPERIMENT_DAYS, "symbols": symbols, "saved": saved,
                "graded_now": graded_now, "summary": summary}

    @app.get("/api/kronos/experiment2")
    async def api_kronos_experiment2(request: Request,
                                     _user: str = Depends(require_login)):
        try:
            limit = int(request.query_params.get("limit", "500"))
        except (TypeError, ValueError):
            return JSONResponse({"detail": "Limit must be a whole number."}, status_code=400)
        if not 1 <= limit <= 2000:
            return JSONResponse(
                {"detail": "Limit must be between 1 and 2000."}, status_code=400)
        try:
            store = _experiment_store()
            data = await asyncio.to_thread(store.recent, limit)
            summary = await asyncio.to_thread(store.summary)
        except Exception as exc:  # noqa: BLE001 - mapped to a stable error response
            return _failure(exc)
        return {**data, "summary": summary}

    @app.post("/api/kronos/experiment2/refresh")
    async def api_kronos_experiment2_refresh(_user: str = Depends(require_login)):
        try:
            store = _experiment_store()
            graded_now = await asyncio.to_thread(store.grade)
            summary = await asyncio.to_thread(store.summary)
        except Exception as exc:  # noqa: BLE001 - mapped to a stable error response
            return _failure(exc)
        return {"graded_now": graded_now, "summary": summary}

    @app.get("/api/kronos/predictions")
    async def api_kronos_predictions(request: Request, _user: str = Depends(require_login)):
        try:
            limit = int(request.query_params.get("limit", "500"))
        except (TypeError, ValueError):
            return JSONResponse({"detail": "Limit must be a whole number."}, status_code=400)
        if not 1 <= limit <= 2000:
            return JSONResponse(
                {"detail": "Limit must be between 1 and 2000."}, status_code=400)
        try:
            return await asyncio.to_thread(_store().recent, limit)
        except Exception as exc:  # noqa: BLE001 - mapped to a stable error response
            return _failure(exc)

    @app.get("/api/kronos/winrate")
    async def api_kronos_winrate(_user: str = Depends(require_login)):
        try:
            return await asyncio.to_thread(_store().stats)
        except Exception as exc:  # noqa: BLE001 - mapped to a stable error response
            return _failure(exc)

    @app.post("/api/kronos/winrate/refresh")
    async def api_kronos_winrate_refresh(_user: str = Depends(require_login)):
        try:
            store = _store()
            graded_now = await asyncio.to_thread(store.grade)
            stats = await asyncio.to_thread(store.stats)
        except Exception as exc:  # noqa: BLE001 - mapped to a stable error response
            return _failure(exc)
        return {**stats, "graded_now": graded_now}
