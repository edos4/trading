"""
web/app.py — FastAPI web UI (VPS deploy).

Auth: session cookie after form login. WEB_UI_PASSWORD is mandatory.

Run:
    python main.py --web
"""

from __future__ import annotations

import asyncio
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
from web.services import TIMEFRAMES, get_explorer

ROOT = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(ROOT / "templates"))


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

    app.mount("/static", StaticFiles(directory=str(ROOT / "static")), name="static")

    def ctx(request: Request, **extra: Any) -> dict[str, Any]:
        return {"request": request, "user": current_username(request), **extra}

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
        return render(
            request,
            "explorer.html",
            active="explorer",
            kronos_gate=default_market().kronos_gate_default,
            kronos_batch=settings.kronos_batch_enabled,
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
            default_market=default_market().id,
            markets=markets_payload(),
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
        return render(
            request,
            "kronos.html",
            active="kronos",
            default_market=default_market().id,
            markets=markets_payload(),
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
        try:
            raw = await _json_body(request)
            payload = SymbolRequest.model_validate(raw)
        except (ValueError, ValidationError) as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        if payload.timeframe not in TIMEFRAMES:
            return JSONResponse({"detail": "Invalid timeframe"}, status_code=400)
        try:
            result = get_explorer().load_symbol(
                payload.symbol.upper().strip(),
                payload.exchange.upper().strip(),
                payload.timeframe,
                run_patterns=payload.run_patterns,
                kronos_gate=payload.kronos_gate,
                kronos_batch=payload.kronos_batch,
                volume_gate=payload.volume_gate,
                market=payload.market,
            )
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
            params["n_symbols"], params["pattern"], params["kwargs"], ab=False,
            extra_symbols=params.get("extra_symbols") or "",
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
            params["n_symbols"], params["pattern"], params["kwargs"], ab=True,
            extra_symbols=params.get("extra_symbols") or "",
        )
        if err:
            return JSONResponse({"detail": err}, status_code=409)
        return {"ok": True}

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
        return paper_books.start(
            market,
            payload.n_symbols,
            extra_symbols=payload.extra_symbols,
            use_stream=payload.use_stream,
            kronos_gate=payload.kronos_gate,
            kronos_rank=payload.kronos_rank,
            kronos_batch=payload.kronos_batch,
            volume_gate=payload.volume_gate,
            pattern_only=payload.pattern_only,
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
        )
        if result.get("error"):
            return JSONResponse({"detail": result["error"]}, status_code=404)
        return result

    @app.post("/api/kronos/predict")
    async def api_kronos_predict(request: Request, _user: str = Depends(require_login)):
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
