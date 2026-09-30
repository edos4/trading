"""
web/app.py — FastAPI web UI (VPS deploy).

Auth: session cookie after form login. WEB_UI_PASSWORD is mandatory.

Run:
    python main.py --web
"""

from __future__ import annotations

import asyncio
import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Literal, Optional
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, ValidationError
from starlette.middleware.gzip import GZipMiddleware

from config import settings
from core.market import default_market, markets_payload
from utils.logger import log
from web.auth import (
    clear_session_cookie,
    current_username,
    require_history,
    require_login,
    require_password_configured,
    set_session_cookie,
    verify_credentials,
)
from web.jobs import (
    backtest_job,
    backtest_param_schema,
    normalize_backtest_form,
    paper_books,
)
from web.runs import backtest_runs
from web.patterns import pattern_edits, patterns_error
from web import replay_store
from web.remote_proxy import RemotePatternProxy
from web.services import TIMEFRAMES, get_explorer

ROOT = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(ROOT / "templates"))


class HistoryBulkRequest(BaseModel):
    symbols: list[str] = Field(default_factory=list, max_length=500)
    after_ts: Optional[int] = None
    limit: Optional[int] = Field(None, ge=1, le=2000)
    market: Optional[Literal["us", "ph"]] = None


class SymbolRequest(BaseModel):
    symbol: str
    exchange: str
    timeframe: str = "1d"
    run_patterns: bool = True
    kronos_gate: Optional[bool] = None
    kronos_batch: Optional[bool] = None
    volume_gate: Optional[bool] = None
    market: Optional[str] = None


class PaperStartRequest(BaseModel):
    n_symbols: int = Field(50, ge=5, le=5000)
    extra_symbols: str = ""
    use_stream: bool = False
    kronos_gate: bool = True
    kronos_rank: bool = False
    kronos_batch: bool = False
    volume_gate: bool = True
    pattern_only: bool = False
    collect_first: bool = False
    collect_first_top_n: int = 4
    stream_start: Optional[str] = None
    market: Optional[Literal["us", "ph"]] = None


class PaperStopRequest(BaseModel):
    market: Optional[Literal["us", "ph", "all"]] = "all"


class PaperStartBothRequest(BaseModel):
    us: Optional[PaperStartRequest] = None
    ph: Optional[PaperStartRequest] = None


class KronosPredictRequest(BaseModel):
    symbol: str
    days: int = Field(5, ge=1, le=120)
    market: Optional[Literal["us", "ph"]] = None


class BacktestRunRequest(BaseModel):
    """Shared-service backtest run (offline or historical stream)."""
    mode: Literal["offline", "historical-stream"] = "offline"
    market: Literal["us", "ph"] = "us"
    preset_id: Optional[str] = None
    preset_name: Optional[str] = None
    symbols: list[str] | str = Field(default_factory=list)
    universe: Optional[str] = None
    versions: Optional[dict[str, str]] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    session_count: int = Field(0, ge=0, le=5000)
    warmup_bars: int = Field(40, ge=0, le=400)
    initial_capital: float = Field(100_000.0, gt=0)
    sizing_mode: Literal["fixed-notional", "paper-risk"] = "fixed-notional"
    position_notional: float = Field(10_000.0, gt=0)
    txn_cost_pct: float = Field(0.0, ge=0, lt=1)
    slippage_pct: float = Field(0.0005, ge=0, lt=1)
    pattern_only: bool = False
    volume_gate: bool = False
    kronos_gate: bool = False
    kronos_rank: bool = False
    collect_first: int = Field(0, ge=0, le=50)
    end_policy: Literal["keep-open", "force-close"] = "keep-open"
    idempotency_key: Optional[str] = None


class BacktestPresetRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    preset_id: Optional[str] = None
    expected_generation: Optional[int] = None
    settings: dict[str, Any]


class PatternEditRequest(BaseModel):
    """Submit an AI edit against an explicit immutable base version."""
    pattern_id: str
    base_version_id: str
    instruction: str = Field(..., min_length=1, max_length=16000)
    preset_id: Optional[str] = None
    preset_name: Optional[str] = None
    settings: Optional[dict[str, Any]] = None
    idempotency_key: Optional[str] = None
    chart_context: Optional[dict[str, Any]] = None


class PatternDefaultRequest(BaseModel):
    expected_generation: int = Field(..., ge=0)
    idempotency_key: Optional[str] = None


class PatternArchiveRequest(BaseModel):
    expected_generation: int = Field(..., ge=0)
    replacement_default_version_id: Optional[str] = None
    idempotency_key: Optional[str] = None


def _backtest_error(exc: Exception) -> JSONResponse:
    from core.pattern_edit_store import Conflict, EditError
    from core.pattern_editor_db import DatabaseUnavailable, MigrationRequired

    if isinstance(exc, Conflict):
        return JSONResponse({"detail": str(exc)}, status_code=409)
    if isinstance(exc, (DatabaseUnavailable, MigrationRequired)):
        return JSONResponse({"detail": str(exc)}, status_code=503)
    if isinstance(exc, EditError):
        return JSONResponse({"detail": str(exc)}, status_code=400)
    if isinstance(exc, ValueError):
        return JSONResponse({"detail": str(exc)}, status_code=400)
    log.exception("Web backtest | unexpected failure")
    return JSONResponse({"detail": "Backtest service failed."}, status_code=500)


def _run_request(payload: BacktestRunRequest):
    """Build a validated durable request from posted form values."""
    from core.backtest_service import request_from_values
    from core.remote_pattern_store import remote_patterns_enabled

    # With a remote registry nothing is written here: the run is executed
    # locally and the host resolves the preset when it records the evidence.
    return request_from_values(
        backtest_runs.service(), payload.model_dump(),
        versions=payload.versions, preset_id=payload.preset_id,
        preset_name=payload.preset_name, idempotency_key=payload.idempotency_key,
        persist=not remote_patterns_enabled())


class ReplayChartRequest(BaseModel):
    market: Optional[Literal["us", "ph"]] = None
    symbol: str
    side: str = "open"
    action: Optional[str] = None
    pattern: Optional[str] = None
    pattern_version_id: Optional[str] = None
    timeframe: str = "1d"
    entry: Optional[float] = None
    stop: Optional[float] = None
    target: Optional[float] = None
    exit: Optional[float] = None
    exit_reason: Optional[str] = None
    current: Optional[float] = None
    chart_annotations: list[dict] | None = None
    entry_time: Optional[str] = None
    exit_time: Optional[str] = None


async def _json_body(request: Request) -> dict[str, Any]:
    """Read JSON object from request. Avoids FastAPI Body()/query mis-binding."""
    try:
        data = await request.json()
    except Exception as exc:
        raise ValueError(f"Invalid JSON body: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError("JSON body must be an object")
    return data


def create_app() -> FastAPI:
    require_password_configured()

    app = FastAPI(title="Trading Bot Web UI", docs_url=None, redoc_url=None)
    app.add_middleware(GZipMiddleware, minimum_size=500)
    # Inert unless PATTERN_API_URL is set: a client with no editor database has
    # the registry endpoints served by the host that owns the database.
    app.add_middleware(RemotePatternProxy)

    app.mount("/static", StaticFiles(directory=str(ROOT / "static")), name="static")

    def ctx(request: Request, **extra: Any) -> dict[str, Any]:
        return {
            "request": request,
            "user": current_username(request),
            "kronos_enabled": settings.enable_kronos,
            **extra,
        }

    def render(request: Request, name: str, *, status_code: int = 200, **extra: Any):
        return templates.TemplateResponse(
            request, name, ctx(request, **extra), status_code=status_code,
        )

    # ── Auth ──────────────────────────────────────────────────────────────
    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request, next: str = "/", error: str = ""):
        if current_username(request):
            return RedirectResponse("/", status_code=303)
        return render(request, "login.html", next_url=_safe_next(next), error=error)

    @app.post("/login")
    async def login_submit(
        request: Request,
        username: str = Form(...),
        password: str = Form(...),
        next: str = Form("/"),
    ):
        if not verify_credentials(username, password):
            log.warning(f"Web auth | failed login for user={username!r} from {request.client}")
            return render(
                request,
                "login.html",
                status_code=401,
                next_url=_safe_next(next),
                error="Invalid username or password.",
            )
        resp = RedirectResponse(_safe_next(next), status_code=303)
        set_session_cookie(resp, username)
        log.info(f"Web auth | login ok user={username!r}")
        return resp

    @app.post("/logout")
    async def logout():
        resp = RedirectResponse("/login", status_code=303)
        clear_session_cookie(resp)
        return resp

    @app.get("/health")
    async def health():
        return {"ok": True}

    # ── Pages ─────────────────────────────────────────────────────────────
    @app.get("/", response_class=HTMLResponse)
    async def explorer_page(request: Request, _user: str = Depends(require_login)):
        kronos = settings.enable_kronos
        return render(
            request,
            "explorer.html",
            active="explorer",
            kronos_gate=kronos and default_market().kronos_gate_default,
            kronos_batch=kronos and settings.kronos_batch_enabled,
            volume_gate=settings.volume_gate_enabled,
            default_market=default_market().id,
            markets=markets_payload(),
            default_n_symbols=30 if default_market().id == "ph" else 50,
        )

    @app.get("/backtest", response_class=HTMLResponse)
    async def backtest_page(request: Request, _user: str = Depends(require_login)):
        return render(
            request,
            "backtest.html",
            active="backtest",
            params=backtest_param_schema(),
            replay_params=replay_param_schema(),
            default_market=default_market().id,
            markets=markets_payload(),
            stream_start_default=_stream_start_default(),
        )

    @app.get("/paper", response_class=HTMLResponse)
    async def paper_page(request: Request, _user: str = Depends(require_login)):
        us = next(m for m in markets_payload() if m["id"] == "us")
        ph = next(m for m in markets_payload() if m["id"] == "ph")
        return render(
            request,
            "paper.html",
            active="paper",
            volume_gate=settings.volume_gate_enabled,
            markets=markets_payload(),
            us=us,
            ph=ph,
            book_cards=[us, ph],
            stream_start_default=_stream_start_default(),
        )

    @app.get("/kronos", response_class=HTMLResponse)
    async def kronos_page(request: Request, _user: str = Depends(require_login)):
        if not settings.enable_kronos:
            return JSONResponse({"detail": "Not found."}, status_code=404)
        return render(
            request,
            "kronos.html",
            active="kronos",
            default_market=default_market().id,
            markets=markets_payload(),
        )

    @app.get("/patterns", response_class=HTMLResponse)
    async def patterns_page(request: Request, _user: str = Depends(require_login)):
        return render(
            request,
            "patterns.html",
            active="patterns",
            params=replay_param_schema(),
            default_market=default_market().id,
            markets=markets_payload(),
            stream_start_default=_stream_start_default(),
        )

    @app.get("/replay", response_class=HTMLResponse)
    async def replay_page(request: Request, _user: str = Depends(require_login)):
        markets = markets_payload()
        return render(
            request,
            "replay.html",
            active="replay",
            markets=markets,
            book_cards=markets,
        )

    # ── Explorer API ──────────────────────────────────────────────────────
    @app.get("/api/markets")
    async def api_markets(_user: str = Depends(require_login)):
        return {"markets": markets_payload(), "default": default_market().id}

    @app.get("/api/symbols")
    async def api_symbols(
        n: int = 50,
        market: str = "",
        _user: str = Depends(require_login),
    ):
        n = max(5, min(int(n), 5000))
        symbols = get_explorer().fetch_symbols(n, market=market or None)
        return {"symbols": symbols}

    @app.get("/api/history/symbols")
    def api_history_symbols(
        market: str = "",
        _user: str = Depends(require_history),
    ):
        from data import db

        mid = (market or "").strip().lower() or None
        if mid not in (None, "us", "ph"):
            return JSONResponse({"detail": "market must be us or ph."}, status_code=400)
        try:
            conn = db.get_conn()
        except Exception:
            log.exception("History API | cannot open Postgres")
            return JSONResponse({"detail": "History database unavailable."}, status_code=503)
        try:
            rows = db.all_symbols(conn, market=mid)
        except Exception:
            log.exception("History API | all_symbols failed")
            return JSONResponse({"detail": "History database unavailable."}, status_code=503)
        finally:
            conn.close()
        return {
            "symbols": [
                {
                    "symbol": r["symbol"],
                    "market": r.get("market") or "us",
                    "last_bar_ts": r.get("last_bar_ts"),
                    "row_count": int(r.get("row_count") or 0),
                }
                for r in rows
            ]
        }

    @app.post("/api/history/bulk")
    def api_history_bulk(
        body: HistoryBulkRequest,
        _user: str = Depends(require_history),
    ):
        from data.history_bulk import load_daily_ohlcv_rows_bulk

        try:
            results = load_daily_ohlcv_rows_bulk(
                body.symbols,
                after_ts=body.after_ts,
                limit=body.limit,
                market=body.market,
            )
        except Exception:
            log.exception("History API | bulk load failed")
            return JSONResponse(
                {"detail": "History database unavailable."}, status_code=503,
            )
        if results is None:
            return JSONResponse(
                {"detail": "History database unavailable."}, status_code=503,
            )
        return {"results": results}

    @app.get("/api/history/{symbol}/meta")
    def api_history_meta(
        symbol: str,
        market: str = "",
        _user: str = Depends(require_history),
    ):
        from data.db import load_symbol_meta

        mid = (market or "").strip().lower() or None
        meta = load_symbol_meta(symbol, market=mid)
        if not meta:
            return JSONResponse({"detail": "Unknown symbol."}, status_code=404)
        return meta

    @app.get("/api/history/{symbol}")
    def api_history_bars(
        symbol: str,
        after_ts: int | None = None,
        limit: int | None = None,
        market: str = "",
        _user: str = Depends(require_history),
    ):
        from data.db import load_daily_ohlcv_rows

        ticker = symbol.upper().strip()
        mid = (market or "").strip().lower() or None
        bars = load_daily_ohlcv_rows(
            ticker, after_ts=after_ts, limit=limit, market=mid,
        )
        if not bars:
            return JSONResponse({"detail": f"No daily bars for {ticker}."}, status_code=404)
        from core.market import ph_history_symbol, resolve_market_id

        out_sym = ph_history_symbol(ticker) if mid and resolve_market_id(mid) == "ph" else ticker
        return {"symbol": out_sym, "bars": bars}

    @app.post("/api/symbol")
    async def api_symbol(request: Request, _user: str = Depends(require_login)):
        from core.pattern_editor_db import DatabaseUnavailable, MigrationRequired

        try:
            raw = await _json_body(request)
            payload = SymbolRequest.model_validate(raw)
        except (ValueError, ValidationError) as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        if payload.timeframe not in TIMEFRAMES:
            return JSONResponse({"detail": "Invalid timeframe"}, status_code=400)
        kronos = settings.enable_kronos
        try:
            result = get_explorer().load_symbol(
                payload.symbol.upper().strip(),
                payload.exchange.upper().strip(),
                payload.timeframe,
                run_patterns=payload.run_patterns,
                kronos_gate=payload.kronos_gate if kronos else False,
                kronos_batch=payload.kronos_batch if kronos else False,
                volume_gate=payload.volume_gate,
                market=payload.market,
            )
        except (DatabaseUnavailable, MigrationRequired) as exc:
            # Pattern resolution needs the editor database; that is a server
            # outage, not a bad request.
            return JSONResponse({"detail": str(exc)}, status_code=503)
        except ValueError as exc:
            # Safe, user-facing message (bad timeframe, no history, etc.).
            return JSONResponse({"detail": str(exc)}, status_code=400)
        except Exception:
            log.exception("Web explorer | load_symbol failed")
            return JSONResponse(
                {"detail": "Failed to load symbol. Check server logs."},
                status_code=400,
            )
        return result

    # ── Backtest API ──────────────────────────────────────────────────────
    @app.get("/api/backtest/status")
    async def api_backtest_status(_user: str = Depends(require_login)):
        return backtest_job.snapshot()

    @app.post("/api/backtest/run")
    async def api_backtest_run(request: Request, _user: str = Depends(require_login)):
        try:
            payload = await _json_body(request)
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        params = normalize_backtest_form(payload)
        err = backtest_job.start(
            params["pattern"], params["kwargs"],
            extra_symbols=params.get("extra_symbols") or "",
            universe=params.get("universe"),
        )
        if err:
            return JSONResponse({"detail": err}, status_code=409)
        return {"ok": True}

    @app.post("/api/backtest/ab")
    async def api_backtest_ab(request: Request, _user: str = Depends(require_login)):
        try:
            payload = await _json_body(request)
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        params = normalize_backtest_form(payload)
        err = backtest_job.start(
            params["pattern"], params["kwargs"],
            extra_symbols=params.get("extra_symbols") or "",
            universe=params.get("universe"),
        )
        if err:
            return JSONResponse({"detail": err}, status_code=409)
        return {"ok": True}

    # ── Shared-service backtest API (offline + historical stream) ─────────
    @app.get("/api/backtest/catalog")
    async def api_backtest_catalog(_user: str = Depends(require_login)):
        try:
            return {"patterns": await asyncio.to_thread(backtest_runs.catalog)}
        except Exception as exc:  # noqa: BLE001 - mapped to a safe status
            return _backtest_error(exc)

    @app.get("/api/backtest/catalog/{pattern_id}/versions")
    async def api_backtest_versions(
        pattern_id: str, include_archived: bool = False,
        _user: str = Depends(require_login),
    ):
        try:
            return {"versions": await asyncio.to_thread(
                backtest_runs.versions, pattern_id,
                include_archived=include_archived)}
        except Exception as exc:  # noqa: BLE001
            return _backtest_error(exc)

    @app.get("/api/backtest/presets")
    async def api_backtest_presets(_user: str = Depends(require_login)):
        try:
            return {"presets": await asyncio.to_thread(backtest_runs.presets)}
        except Exception as exc:  # noqa: BLE001
            return _backtest_error(exc)

    @app.post("/api/backtest/presets")
    async def api_backtest_preset_save(request: Request,
                                       _user: str = Depends(require_login)):
        from core.backtest_params import settings_from_values

        try:
            body = BacktestPresetRequest.model_validate(await _json_body(request))
        except (ValueError, ValidationError) as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        try:
            settings = settings_from_values({**body.settings, "mode": body.settings.get("mode", "offline")})
            preset = await asyncio.to_thread(
                backtest_runs.save_preset, body.name, settings,
                preset_id=body.preset_id, expected_generation=body.expected_generation)
        except Exception as exc:  # noqa: BLE001
            return _backtest_error(exc)
        return {"preset": preset}

    @app.post("/api/backtest/runs")
    async def api_backtest_run_start(request: Request,
                                     _user: str = Depends(require_login)):
        try:
            body = BacktestRunRequest.model_validate(await _json_body(request))
        except (ValueError, ValidationError) as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        try:
            built = await asyncio.to_thread(_run_request, body)
            job = await asyncio.to_thread(backtest_runs.submit, built)
        except Exception as exc:  # noqa: BLE001
            return _backtest_error(exc)
        return {"run_id": job["id"], "state": job["state"]}

    @app.get("/api/backtest/runs/{run_id}")
    async def api_backtest_run_status(run_id: str,
                                      _user: str = Depends(require_login)):
        from core.backtest_jobs import UnknownJob

        try:
            return await asyncio.to_thread(backtest_runs.run_payload, run_id)
        except UnknownJob as exc:
            return JSONResponse({"detail": str(exc)}, status_code=404)
        except Exception as exc:  # noqa: BLE001
            return _backtest_error(exc)

    @app.post("/api/backtest/runs/{run_id}/cancel")
    async def api_backtest_run_cancel(run_id: str,
                                      _user: str = Depends(require_login)):
        from core.backtest_jobs import UnknownJob

        try:
            return await asyncio.to_thread(backtest_runs.cancel, run_id)
        except UnknownJob as exc:
            return JSONResponse({"detail": str(exc)}, status_code=404)
        except Exception as exc:  # noqa: BLE001
            return _backtest_error(exc)

    @app.post("/api/backtest/runs/{run_id}/retry")
    async def api_backtest_run_retry(run_id: str,
                                     _user: str = Depends(require_login)):
        from core.backtest_jobs import UnknownJob

        try:
            job = await asyncio.to_thread(backtest_runs.retry, run_id)
        except UnknownJob as exc:
            return JSONResponse({"detail": str(exc)}, status_code=404)
        except Exception as exc:  # noqa: BLE001
            return _backtest_error(exc)
        return {"run_id": job["id"], "state": job["state"]}

    # ── Pattern Editor API (Patterns tab) ─────────────────────────────────
    @app.get("/api/patterns")
    async def api_patterns_catalog(_user: str = Depends(require_login)):
        try:
            editor = pattern_edits.editor()
            return {"patterns": await asyncio.to_thread(editor.catalog)}
        except Exception as exc:  # noqa: BLE001 - mapped to a safe status
            return patterns_error(exc)

    @app.get("/api/patterns/pinned")
    async def api_patterns_pinned(request: Request, _user: str = Depends(require_login)):
        """Eligibility-checked version set for a caller with no database."""
        try:
            selected = _selected_pairs(request.query_params.getlist("selected"))
            editor = pattern_edits.editor()
            return {"versions": await asyncio.to_thread(
                editor.pinned, request.query_params.getlist("disabled"), selected)}
        except Exception as exc:  # noqa: BLE001
            return patterns_error(exc)

    @app.get("/api/patterns/bundle")
    async def api_patterns_bundle(request: Request, _user: str = Depends(require_login)):
        """Pinned versions plus the bytes of every file a loader must execute."""
        try:
            selected = _selected_pairs(request.query_params.getlist("selected"))
            editor = pattern_edits.editor()
            return await asyncio.to_thread(
                editor.bundle, request.query_params.getlist("disabled"), selected)
        except Exception as exc:  # noqa: BLE001
            return patterns_error(exc)

    @app.get("/api/patterns/{pattern_id}/versions")
    async def api_pattern_versions(
        pattern_id: str, include_archived: bool = False,
        _user: str = Depends(require_login),
    ):
        try:
            editor = pattern_edits.editor()
            return {"versions": await asyncio.to_thread(
                editor.versions_for, pattern_id, include_archived=include_archived)}
        except Exception as exc:  # noqa: BLE001
            return patterns_error(exc)

    @app.get("/api/patterns/versions/{version_id}")
    async def api_pattern_version_detail(version_id: str,
                                         _user: str = Depends(require_login)):
        try:
            editor = pattern_edits.editor()
            return await asyncio.to_thread(editor.version_detail, version_id)
        except Exception as exc:  # noqa: BLE001
            return patterns_error(exc)

    @app.get("/api/patterns/versions/{version_id}/source")
    async def api_pattern_version_source(version_id: str,
                                         _user: str = Depends(require_login)):
        try:
            editor = pattern_edits.editor()
            return await asyncio.to_thread(editor.source, version_id)
        except Exception as exc:  # noqa: BLE001
            return patterns_error(exc)

    @app.get("/api/patterns/versions/{version_id}/diff")
    async def api_pattern_version_diff(version_id: str, base_version_id: str | None = None,
                                       _user: str = Depends(require_login)):
        try:
            editor = pattern_edits.editor()
            return await asyncio.to_thread(editor.diff, version_id, base_version_id)
        except Exception as exc:  # noqa: BLE001
            return patterns_error(exc)

    @app.post("/api/patterns/runs")
    async def api_pattern_run_ingest(request: Request,
                                     _user: str = Depends(require_login)):
        """Record a run executed on another host, with its evidence."""
        try:
            body = await _json_body(request)
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        try:
            editor = pattern_edits.editor()
            return await asyncio.to_thread(editor.ingest_run, body)
        except Exception as exc:  # noqa: BLE001
            return patterns_error(exc)

    @app.get("/api/patterns/versions/{version_id}/bundle")
    async def api_pattern_version_bundle(version_id: str,
                                         _user: str = Depends(require_login)):
        """Single-version execution material, for a version that just landed."""
        try:
            editor = pattern_edits.editor()
            return await asyncio.to_thread(editor.version_bundle, version_id)
        except Exception as exc:  # noqa: BLE001
            return patterns_error(exc)

    @app.get("/api/patterns/provider")
    async def api_pattern_provider(_user: str = Depends(require_login)):
        """AI provider configuration and account balance (informational)."""
        try:
            editor = pattern_edits.editor()
            return await asyncio.to_thread(editor.balance)
        except Exception as exc:  # noqa: BLE001
            return patterns_error(exc)

    @app.post("/api/patterns/versions/{version_id}/backtest")
    async def api_pattern_backtest(version_id: str, request: Request,
                                   _user: str = Depends(require_login)):
        try:
            values = await _json_body(request)
            return await asyncio.to_thread(pattern_edits.editor().backtest_version,
                                           version_id, values)
        except Exception as exc:
            return patterns_error(exc)

    @app.get("/api/patterns/runs/{run_id}/chart")
    async def api_pattern_run_chart(run_id: str, detection: int,
                                    _user: str = Depends(require_login)):
        try:
            return await asyncio.to_thread(pattern_edits.editor().run_chart,
                                           run_id, detection)
        except Exception as exc:
            return patterns_error(exc)

    @app.post("/api/patterns/edits")
    async def api_pattern_edit_submit(request: Request,
                                      _user: str = Depends(require_login)):
        from core.pattern_editor_api import edit_request_from_values

        try:
            body = PatternEditRequest.model_validate(await _json_body(request))
        except (ValueError, ValidationError) as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        try:
            editor = pattern_edits.editor()
            built = await asyncio.to_thread(
                edit_request_from_values, editor, body.settings or {},
                pattern_id=body.pattern_id, base_version_id=body.base_version_id,
                instruction=body.instruction, preset_id=body.preset_id,
                chart_context=body.chart_context,
                preset_name=body.preset_name, idempotency_key=body.idempotency_key)
            job = await asyncio.to_thread(editor.submit_edit, built)
        except Exception as exc:  # noqa: BLE001
            return patterns_error(exc)
        return {"job_id": job["id"], "state": job["state"]}

    @app.get("/api/patterns/edits/{job_id}")
    async def api_pattern_edit_status(job_id: str,
                                      _user: str = Depends(require_login)):
        try:
            editor = pattern_edits.editor()
            return await asyncio.to_thread(editor.job_detail, job_id)
        except Exception as exc:  # noqa: BLE001
            return patterns_error(exc)

    @app.post("/api/patterns/edits/{job_id}/cancel")
    async def api_pattern_edit_cancel(job_id: str,
                                      _user: str = Depends(require_login)):
        try:
            editor = pattern_edits.editor()
            return await asyncio.to_thread(editor.cancel_job, job_id)
        except Exception as exc:  # noqa: BLE001
            return patterns_error(exc)

    @app.post("/api/patterns/edits/{job_id}/retry")
    async def api_pattern_edit_retry(job_id: str,
                                     _user: str = Depends(require_login)):
        try:
            editor = pattern_edits.editor()
            job = await asyncio.to_thread(editor.retry_backtest, job_id)
        except Exception as exc:  # noqa: BLE001
            return patterns_error(exc)
        return {"job_id": job["id"], "state": job["state"]}

    @app.get("/api/patterns/runs/{run_id}")
    async def api_pattern_run(run_id: str, _user: str = Depends(require_login)):
        try:
            editor = pattern_edits.editor()
            return await asyncio.to_thread(editor.run_payload, run_id)
        except Exception as exc:  # noqa: BLE001
            return patterns_error(exc)

    @app.post("/api/patterns/versions/{version_id}/default")
    async def api_pattern_set_default(version_id: str, request: Request,
                                      _user: str = Depends(require_login)):
        try:
            body = PatternDefaultRequest.model_validate(await _json_body(request))
        except (ValueError, ValidationError) as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        try:
            editor = pattern_edits.editor()
            version = await asyncio.to_thread(editor.version_detail, version_id)
            result = await asyncio.to_thread(
                editor.set_default, pattern_id=version["pattern_id"],
                version_id=version_id, expected_generation=body.expected_generation,
                idempotency_key=body.idempotency_key)
        except Exception as exc:  # noqa: BLE001
            return patterns_error(exc)
        return {"pattern": {
            "pattern_id": result["id"], "default_version_id": result["active"],
            "generation": int(result["generation"]),
        }}

    @app.post("/api/patterns/versions/{version_id}/archive")
    async def api_pattern_archive(version_id: str, request: Request,
                                  _user: str = Depends(require_login)):
        try:
            body = PatternArchiveRequest.model_validate(await _json_body(request))
        except (ValueError, ValidationError) as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        try:
            editor = pattern_edits.editor()
            version = await asyncio.to_thread(editor.version_detail, version_id)
            result = await asyncio.to_thread(
                editor.archive, pattern_id=version["pattern_id"],
                version_id=version_id,
                replacement_default_version_id=body.replacement_default_version_id,
                expected_generation=body.expected_generation,
                idempotency_key=body.idempotency_key)
        except Exception as exc:  # noqa: BLE001
            return patterns_error(exc)
        return {"pattern": {
            "pattern_id": result["id"], "default_version_id": result["active"],
            "generation": int(result["generation"]),
        }}

    # ── Paper API ─────────────────────────────────────────────────────────
    @app.get("/api/paper/status")
    async def api_paper_status(
        request: Request, _user: str = Depends(require_login),
    ):
        lamps = (request.query_params.get("lamps") or "").lower() in (
            "1", "true", "yes",
        )
        market = request.query_params.get("market") or None
        if lamps:
            return await asyncio.to_thread(paper_books.lamps)
        if market:
            return await asyncio.to_thread(paper_books.snapshot, market)
        return await asyncio.to_thread(paper_books.snapshot_all)

    def _start_book(payload: PaperStartRequest, market: str) -> str | None:
        # A hidden Kronos control can never enable the gate: force it off.
        kronos = settings.enable_kronos
        return paper_books.start(
            market,
            payload.n_symbols,
            extra_symbols=payload.extra_symbols,
            use_stream=payload.use_stream,
            kronos_gate=payload.kronos_gate and kronos,
            kronos_rank=payload.kronos_rank and kronos,
            kronos_batch=payload.kronos_batch and kronos,
            volume_gate=payload.volume_gate,
            pattern_only=payload.pattern_only,
            collect_first=payload.collect_first,
            collect_first_top_n=payload.collect_first_top_n,
            stream_start=payload.stream_start,
        )

    @app.post("/api/paper/start")
    async def api_paper_start(request: Request, _user: str = Depends(require_login)):
        try:
            raw = await _json_body(request)
            payload = PaperStartRequest.model_validate(raw)
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        except ValidationError as exc:
            msgs = "; ".join(
                f"{'.'.join(str(x) for x in e.get('loc', ()))}: {e.get('msg')}"
                for e in exc.errors()
            )
            return JSONResponse({"detail": msgs}, status_code=400)
        if not payload.market:
            return JSONResponse({"detail": "market is required (us or ph)."}, status_code=400)
        err = _start_book(payload, payload.market)
        if err:
            return JSONResponse({"detail": err}, status_code=409)
        return {"ok": True}

    @app.post("/api/paper/start-both")
    async def api_paper_start_both(request: Request, _user: str = Depends(require_login)):
        try:
            raw = await _json_body(request)
            payload = PaperStartBothRequest.model_validate(raw)
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        except ValidationError as exc:
            msgs = "; ".join(
                f"{'.'.join(str(x) for x in e.get('loc', ()))}: {e.get('msg')}"
                for e in exc.errors()
            )
            return JSONResponse({"detail": msgs}, status_code=400)
        specs: dict[str, dict[str, Any]] = {}
        if payload.us is not None:
            specs["us"] = payload.us.model_dump()
        if payload.ph is not None:
            specs["ph"] = payload.ph.model_dump()
        if not specs:
            return JSONResponse({"detail": "us and/or ph start payload required."}, status_code=400)
        if not settings.enable_kronos:
            for spec in specs.values():
                spec["kronos_gate"] = False
                spec["kronos_rank"] = False
                spec["kronos_batch"] = False
        errors = paper_books.start_both(specs)
        if errors and len(errors) == len(specs):
            return JSONResponse({"ok": False, "errors": errors}, status_code=409)
        return {"ok": not errors, "errors": errors}

    @app.post("/api/paper/stop")
    async def api_paper_stop(request: Request, _user: str = Depends(require_login)):
        market = "all"
        try:
            raw = await _json_body(request)
            if raw:
                payload = PaperStopRequest.model_validate(raw)
                market = payload.market or "all"
        except ValueError:
            market = "all"
        except ValidationError as exc:
            msgs = "; ".join(
                f"{'.'.join(str(x) for x in e.get('loc', ()))}: {e.get('msg')}"
                for e in exc.errors()
            )
            return JSONResponse({"detail": msgs}, status_code=400)
        paper_books.stop(market)
        return {"ok": True}

    @app.post("/api/paper/reset")
    async def api_paper_reset(request: Request, _user: str = Depends(require_login)):
        market = None
        try:
            raw = await _json_body(request)
            market = (raw or {}).get("market")
        except ValueError:
            market = None
        if not market:
            return JSONResponse({"detail": "market is required (us or ph)."}, status_code=400)
        err = paper_books.reset(market)
        if err:
            return JSONResponse({"detail": err}, status_code=409)
        return {"ok": True}

    @app.post("/api/paper/reset-logs")
    async def api_paper_reset_logs(request: Request, _user: str = Depends(require_login)):
        market = "all"
        try:
            raw = await _json_body(request)
            if raw:
                market = str((raw or {}).get("market") or "all").strip().lower()
        except ValueError:
            market = "all"
        if market not in ("us", "ph", "all"):
            return JSONResponse(
                {"detail": "market must be us, ph, or all."}, status_code=400,
            )
        paper_books.reset_logs(market)
        return {"ok": True}

    @app.get("/api/paper/chart")
    async def api_paper_chart(
        request: Request, _user: str = Depends(require_login),
    ):
        market = (request.query_params.get("market") or "").strip().lower()
        if market not in ("us", "ph"):
            return JSONResponse({"detail": "market is required (us or ph)."}, status_code=400)
        side = (request.query_params.get("side") or "").strip().lower()
        symbol = request.query_params.get("symbol") or None
        index_raw = request.query_params.get("index")
        index = None
        if index_raw not in (None, ""):
            try:
                index = int(index_raw)
            except ValueError:
                return JSONResponse({"detail": "index must be an integer"}, status_code=400)
        result = paper_books.chart(
            market, side=side, symbol=symbol, index=index,
            log_time=request.query_params.get("log_time") or None,
            **({"trade_id":request.query_params["trade_id"]} if request.query_params.get("trade_id") else {}),
        )
        if result.get("error"):
            return JSONResponse({"detail": result["error"]}, status_code=404)
        return result

    # ── Replay API ───────────────────────────────────────────────────────
    @app.post("/api/replay/upload")
    async def api_replay_upload(request: Request, _user: str = Depends(require_login)):
        try:
            payload = await _json_body(request)
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        books = payload.get("books")
        if not isinstance(books, (list, dict)) or not books:
            return JSONResponse(
                {"detail": "payload must contain a non-empty 'books' field."},
                status_code=400,
            )
        try:
            from uuid import uuid4
            payload["correction_replay_id"] = str(uuid4())
            replay_store.save(payload)
        except OSError:
            log.exception("Web | replay upload failed to persist")
            return JSONResponse({"detail": "Failed to persist replay."}, status_code=500)
        return {"ok": True, "replay_id": payload["correction_replay_id"]}

    @app.get("/api/replay/load")
    async def api_replay_load(_user: str = Depends(require_login)):
        payload = await asyncio.to_thread(replay_store.load)
        if payload is None:
            return {"replay": None}
        if not payload.get("correction_replay_id"):
            import hashlib
            payload["correction_replay_id"] = hashlib.sha256(
                json.dumps(payload, sort_keys=True).encode()).hexdigest()
        return {"replay": payload}

    @app.post("/api/replay/clear")
    async def api_replay_clear(_user: str = Depends(require_login)):
        await asyncio.to_thread(replay_store.clear)
        return {"ok": True}

    @app.post("/api/replay/chart")
    async def api_replay_chart(
        request: Request, _user: str = Depends(require_login),
    ):
        try:
            body = ReplayChartRequest.model_validate(await _json_body(request))
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        except ValidationError as exc:
            msg = exc.errors()[0].get("msg") if exc.errors() else "Invalid request"
            return JSONResponse({"detail": str(msg)}, status_code=400)

        from analysis.chart_renderer import build_trade_viewer_payload
        from core.market import get_market
        from data.history import load_daily_ohlcv_df

        symbol = body.symbol.upper().strip()
        market = body.market or default_market().id
        if market not in ("us", "ph"):
            return JSONResponse({"detail": "market must be us or ph."}, status_code=400)
        side = (body.side or "open").lower()
        if side not in ("open", "closed"):
            return JSONResponse({"detail": "side must be open or closed."}, status_code=400)

        try:
            df = await asyncio.to_thread(
                load_daily_ohlcv_df, symbol, tv_fallback=False, market=market,
            )
        except Exception:
            log.exception("Web | replay chart history load failed")
            return JSONResponse({"detail": "History database unavailable."}, status_code=503)
        if df is None or len(df) < 2:
            return JSONResponse({"detail": f"No daily bars for {symbol}."}, status_code=404)

        try:
            payload = build_trade_viewer_payload(
                df,
                symbol=symbol,
                timeframe=body.timeframe or "1d",
                annotations=body.chart_annotations,
                pattern=body.pattern,
                pattern_version_id=body.pattern_version_id,
                market=market,
                action=body.action,
                session_tz=get_market(market).session_tz,
                entry=body.entry,
                stop=body.stop,
                target=body.target,
                exit_price=body.exit if side == "closed" else None,
                exit_reason=body.exit_reason if side == "closed" else None,
                current=body.current if side == "open" else None,
                entry_time=body.entry_time,
                exit_time=body.exit_time if side == "closed" else None,
            )
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        payload["replay_cutoff"] = body.entry_time
        return payload

    @app.post("/api/kronos/predict")
    async def api_kronos_predict(request: Request, _user: str = Depends(require_login)):
        if not settings.enable_kronos:
            return JSONResponse({"detail": "Not found."}, status_code=404)
        try:
            body = KronosPredictRequest.model_validate(await _json_body(request))
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        except ValidationError as exc:
            msg = exc.errors()[0].get("msg") if exc.errors() else "Invalid request"
            return JSONResponse({"detail": str(msg)}, status_code=400)
        from core.kronos_forecast import forecast_symbol

        try:
            payload = await asyncio.to_thread(
                forecast_symbol, body.symbol, body.days, market=body.market,
            )
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        except Exception:
            log.exception("Web | Kronos predict failed")
            return JSONResponse({"detail": "Kronos prediction failed."}, status_code=500)
        return payload

    @app.get("/api/paper/export")
    async def api_paper_export(
        request: Request, _user: str = Depends(require_login),
    ):
        market = (request.query_params.get("market") or "all").strip().lower()
        if market not in ("us", "ph", "all"):
            return JSONResponse({"detail": "market must be us, ph, or all."}, status_code=400)
        try:
            payload = paper_books.export_trades(None if market == "all" else market)
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        return payload

    return app


def replay_param_schema() -> list[dict[str, Any]]:
    """JSON-friendly copy of the shared REPLAY_PARAMS for the web form."""
    from core.backtest_params import REPLAY_PARAMS

    out: list[dict[str, Any]] = []
    for key, label, desc, ptype, default, choices in REPLAY_PARAMS:
        entry: dict[str, Any] = {"key": key, "label": label, "description": desc,
                                 "type": ptype}
        if ptype == "spin":
            default_val, minv, maxv, inc = default
            entry.update(default=default_val, min=minv, max=maxv, step=inc)
        elif ptype == "check":
            entry["default"] = bool(default)
        else:
            entry["default"] = default
            entry["choices"] = choices or []
        out.append(entry)
    return out


def _stream_start_default() -> str:
    """Default stream start date — mirrors the tkinter UI datepicker:
    PAPERTRADE_STREAM_START_DATE if set, else ~1 year ago on a weekday
    (weekends/NYSE fixed holidays roll forward so pin_asof does not land
    on an empty cash session)."""
    from data.stream_server import _roll_forward_session_day

    raw = settings.papertrade_stream_start_date
    if raw:
        try:
            return _roll_forward_session_day(
                date.fromisoformat(raw.strip()), "us",
            ).isoformat()
        except ValueError:
            pass
    return _roll_forward_session_day(
        date.today() - timedelta(days=365), "us",
    ).isoformat()


def _selected_pairs(values: list[str]) -> dict[str, str]:
    """Parse repeated ``pattern_id:version_id`` pairs from a query string."""
    selected: dict[str, str] = {}
    for raw in values:
        pattern_id, sep, version_id = (raw or "").partition(":")
        if not sep or not pattern_id or not version_id:
            raise ValueError(f"selected must be pattern_id:version_id, got {raw!r}")
        selected[pattern_id] = version_id
    return selected


def _safe_next(next_url: str) -> str:
    """Only allow same-origin relative paths (open-redirect guard)."""
    if not next_url:
        return "/"
    parsed = urlparse(next_url)
    if parsed.scheme or parsed.netloc:
        return "/"
    if not next_url.startswith("/"):
        return "/"
    if next_url.startswith("//"):
        return "/"
    return next_url


def run() -> None:
    require_password_configured()
    import uvicorn
    from data.ensure_history import start_web_history_backfill
    from data.history import (
        DEFAULT_STOCKS_HISTORY_URL,
        enable_ui_web_history,
        local_history_backfill_enabled,
    )

    enable_ui_web_history()
    if local_history_backfill_enabled():
        start_web_history_backfill()
    else:
        remote = (settings.stocks_history_url or DEFAULT_STOCKS_HISTORY_URL).rstrip("/")
        log.info(
            f"Web UI | remote history {remote} — skip local Postgres ping/--update-db"
        )

    host = settings.web_ui_host
    port = int(settings.web_ui_port)
    log.info(
        f"Web UI | starting on http://{host}:{port} "
        f"(auth user={settings.web_ui_username!r}, https_cookie={settings.web_ui_https})"
    )
    uvicorn.run(
        "web.app:create_app",
        factory=True,
        host=host,
        port=port,
        log_level="info",
        timeout_keep_alive=75,
    )


if __name__ == "__main__":
    run()
