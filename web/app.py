"""
web/app.py — FastAPI web UI (VPS deploy).

Auth: session cookie after form login. WEB_UI_PASSWORD is mandatory.

Run:
    python main.py --web
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, ValidationError

from config import settings
from core.market import default_market, markets_payload
from utils.logger import log
from web.auth import (
    clear_session_cookie,
    current_username,
    require_login,
    require_password_configured,
    set_session_cookie,
    verify_credentials,
)
from web.jobs import (
    backtest_job,
    backtest_param_schema,
    normalize_backtest_form,
    paper_session,
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
    volume_gate: Optional[bool] = None
    market: Optional[str] = None


class PaperStartRequest(BaseModel):
    n_symbols: int = Field(100, ge=5, le=5000)
    extra_symbols: str = ""
    use_stream: bool = False
    kronos_gate: bool = True
    kronos_rank: bool = False
    volume_gate: bool = False
    stream_start: Optional[str] = None
    market: Optional[str] = None


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
        return render(
            request,
            "paper.html",
            active="paper",
            kronos_gate=default_market().kronos_gate_default,
            kronos_rank=default_market().kronos_rank_default,
            volume_gate=settings.volume_gate_enabled,
            default_market=default_market().id,
            default_n_symbols=default_market().default_n_symbols,
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
        market = request.query_params.get("market") or None
        return paper_session.snapshot(market=market)

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
        err = paper_session.start(
            payload.n_symbols,
            extra_symbols=payload.extra_symbols,
            use_stream=payload.use_stream,
            kronos_gate=payload.kronos_gate,
            kronos_rank=payload.kronos_rank,
            volume_gate=payload.volume_gate,
            stream_start=payload.stream_start,
            market=payload.market,
        )
        if err:
            return JSONResponse({"detail": err}, status_code=409)
        return {"ok": True}

    @app.post("/api/paper/stop")
    async def api_paper_stop(_user: str = Depends(require_login)):
        paper_session.stop()
        return {"ok": True}

    @app.post("/api/paper/reset")
    async def api_paper_reset(request: Request, _user: str = Depends(require_login)):
        market = None
        try:
            raw = await _json_body(request)
            market = (raw or {}).get("market")
        except ValueError:
            market = None
        err = paper_session.reset(market=market)
        if err:
            return JSONResponse({"detail": err}, status_code=409)
        return {"ok": True}

    return app


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
    )


if __name__ == "__main__":
    run()
