"""Offline tests for the MCP server (no network, no live web server)."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from config import settings
from mcp_server import (
    api_client,
    server,
    tools_backtest,
    tools_deploy,
    tools_maintenance,
    tools_market,
    tools_paper,
    tools_patterns,
)
from mcp_server.api_client import TradingApiClient, TradingApiError


def _client(handler, *, username="admin", password="pw") -> TradingApiClient:
    return TradingApiClient(
        base_url="http://testserver",
        username=username,
        password=password,
        transport=httpx.MockTransport(handler),
    )


@pytest.fixture
def install_client():
    def _install(handler, **creds):
        client = _client(handler, **creds)
        api_client.configure(client)
        return client

    yield _install
    api_client.reset_client()


# ── Registry ──────────────────────────────────────────────────────────────


def test_registry_covers_every_group():
    assert set(server.TOOL_GROUP_NAMES) == {
        "market", "patterns", "backtest", "paper", "kronos", "replay",
        "maintenance", "deploy",
    }
    for name, group in server.TOOL_GROUPS.items():
        assert group, f"tool group {name!r} is empty"


def test_registry_names_are_unique_and_include_key_tools():
    names = server.TOOL_NAMES
    assert len(names) == len(set(names)), "duplicate tool name"
    expected = {
        "get_markets", "list_symbols", "get_history_bars", "analyze_symbol",
        "server_health", "list_patterns", "submit_pattern_edit",
        "set_pattern_default", "submit_backtest_run", "get_backtest_run",
        "paper_status", "paper_start", "paper_stop", "paper_reset",
        "kronos_predict", "db_check", "deploy_status",
    }
    assert expected <= set(names)


# ── Request mapping (MockTransport) ───────────────────────────────────────


def test_get_markets(install_client):
    def handler(request):
        assert request.method == "GET"
        assert request.url.path == "/api/markets"
        return httpx.Response(200, json={"markets": [{"id": "us"}], "default": "us"})

    install_client(handler)
    assert tools_market.get_markets()["default"] == "us"


def test_list_symbols_mapping(install_client):
    seen = {}

    def handler(request):
        seen["path"] = request.url.path
        seen["n"] = request.url.params["n"]
        seen["market"] = request.url.params.get("market")
        return httpx.Response(200, json={"symbols": []})

    install_client(handler)
    tools_market.list_symbols(7, market="ph")
    assert seen == {"path": "/api/symbols", "n": "7", "market": "ph"}


def test_history_bars_clamps_limit(install_client, monkeypatch):
    monkeypatch.setattr(settings, "mcp_max_history_bars", 2000)
    seen = {}

    def handler(request):
        seen["limit"] = request.url.params.get("limit")
        seen["market"] = request.url.params.get("market")
        return httpx.Response(200, json={"symbol": "AAPL", "bars": []})

    install_client(handler)
    tools_market.get_history_bars("AAPL", limit=99999)
    assert seen["limit"] == "2000"

    tools_market.get_history_bars("AAPL")
    assert seen["limit"] == "2000"


def test_history_bars_passes_after_ts(install_client):
    seen = {}

    def handler(request):
        seen["after_ts"] = request.url.params.get("after_ts")
        return httpx.Response(200, json={"symbol": "AAPL", "bars": []})

    install_client(handler)
    tools_market.get_history_bars("AAPL", after_ts=123, limit=10)
    assert seen["after_ts"] == "123"


def test_analyze_symbol_body(install_client):
    seen = {}

    def handler(request):
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"symbol": "AAPL", "signals": [], "chart_png_b64": None})

    install_client(handler)
    tools_market.analyze_symbol("AAPL", exchange="NASDAQ", timeframe="1W", volume_gate=True)
    assert seen["path"] == "/api/symbol"
    assert seen["body"]["symbol"] == "AAPL"
    assert seen["body"]["exchange"] == "NASDAQ"
    assert seen["body"]["timeframe"] == "1W"
    assert seen["body"]["volume_gate"] is True


def test_paper_start_body(install_client):
    seen = {}

    def handler(request):
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"ok": True})

    install_client(handler)
    tools_paper.paper_start("us", n_symbols=10, volume_gate=True, use_stream=True)
    assert seen["path"] == "/api/paper/start"
    assert seen["body"]["market"] == "us"
    assert seen["body"]["n_symbols"] == 10
    assert seen["body"]["volume_gate"] is True
    assert seen["body"]["use_stream"] is True


def test_paper_status_params(install_client):
    seen = {}

    def handler(request):
        seen["market"] = request.url.params.get("market")
        seen["lamps"] = request.url.params.get("lamps")
        return httpx.Response(200, json={"books": {}})

    install_client(handler)
    tools_paper.paper_status(market="us")
    assert seen["market"] == "us"
    tools_paper.paper_status(lamps=True)
    assert seen["lamps"] == "true"


def test_submit_backtest_run_body(install_client):
    seen = {}

    def handler(request):
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"run_id": "r1", "state": "queued"})

    install_client(handler)
    out = tools_backtest.submit_backtest_run(market="ph", symbols=["AAPL"], idempotency_key="k")
    assert out["run_id"] == "r1"
    assert seen["path"] == "/api/backtest/runs"
    assert seen["body"]["market"] == "ph"
    assert seen["body"]["symbols"] == ["AAPL"]
    assert seen["body"]["idempotency_key"] == "k"
    assert "universe" not in seen["body"]


def test_patterns_bundle_selection_params(install_client):
    seen = {}

    def handler(request):
        seen["params"] = request.url.params.get_list("selected")
        seen["disabled"] = request.url.params.get_list("disabled")
        return httpx.Response(200, json={"versions": []})

    install_client(handler)
    tools_patterns.get_patterns_bundle(
        selected={"pattern_003_double_bottom": "v2"},
        disabled=["pattern_009_flag_pattern"],
    )
    assert seen["params"] == ["pattern_003_double_bottom:v2"]
    assert seen["disabled"] == ["pattern_009_flag_pattern"]


# ── Error mapping ─────────────────────────────────────────────────────────


def test_error_status_and_detail(install_client):
    def handler(request):
        return httpx.Response(409, json={"detail": "already running"})

    install_client(handler)
    with pytest.raises(TradingApiError) as exc:
        tools_paper.paper_start("us")
    assert exc.value.status_code == 409
    assert exc.value.detail == "already running"


def test_unreachable_service(install_client):
    def handler(request):
        raise httpx.ConnectError("refused")

    install_client(handler)
    with pytest.raises(TradingApiError) as exc:
        tools_market.get_markets()
    assert exc.value.status_code == 0
    assert "unreachable" in exc.value.detail


def test_server_health_never_raises(install_client):
    def handler(request):
        raise httpx.ConnectError("refused")

    install_client(handler)
    out = tools_market.server_health()
    assert out["ok"] is False
    assert out["status_code"] == 0


# ── Default-deny gating ───────────────────────────────────────────────────


def test_destructive_gating(monkeypatch, install_client):
    def handler(request):
        return httpx.Response(200, json={"ok": True})

    install_client(handler)
    monkeypatch.setattr(settings, "mcp_allow_destructive", False)
    with pytest.raises(PermissionError):
        tools_paper.paper_reset("us")

    monkeypatch.setattr(settings, "mcp_allow_destructive", True)
    assert tools_paper.paper_reset("us")["ok"] is True


def test_maintenance_gating(monkeypatch):
    monkeypatch.setattr(settings, "mcp_allow_maintenance", False)
    with pytest.raises(PermissionError):
        tools_maintenance.db_check()

    monkeypatch.setattr(settings, "mcp_allow_maintenance", True)
    import data.check
    import data.update

    def fake_check(**kwargs):
        print("fresh: us")
        return 0

    def fake_update(**kwargs):
        print("updated 3 symbols")

    monkeypatch.setattr(data.check, "run_check", fake_check)
    monkeypatch.setattr(data.update, "run_update", fake_update)

    out = tools_maintenance.db_check(stale_days=3, market="us")
    assert out["exit_code"] == 0
    assert "fresh: us" in out["report"]
    upd = tools_maintenance.db_update(fetch=True)
    assert upd["ok"] is True
    assert "updated 3 symbols" in upd["report"]


def test_deploy_gating(monkeypatch):
    monkeypatch.setattr(settings, "mcp_allow_deploy", False)
    with pytest.raises(PermissionError):
        tools_deploy.deploy_status()

    monkeypatch.setattr(settings, "mcp_allow_deploy", True)
    monkeypatch.setattr(settings, "mcp_deploy_token", "")
    with pytest.raises(PermissionError):
        tools_deploy.deploy("main")


# ── Integration against the real FastAPI app (sync ASGI bridge) ────────────


class _ASGISyncTransport(httpx.BaseTransport):
    """Bridge a sync httpx.Client to an ASGI app (tests only)."""

    def __init__(self, app):
        self._app = app

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        transport = httpx.ASGITransport(app=self._app)

        async def _run() -> httpx.Response:
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
                return await c.request(
                    request.method,
                    request.url.raw_path.decode(),
                    headers=request.headers,
                    content=request.content,
                )

        resp = asyncio.run(_run())
        # httpx already decoded the body; drop encoding headers so the outer
        # client does not decode a second time.
        headers = [
            (k, v)
            for k, v in resp.headers.multi_items()
            if k.lower() not in ("content-encoding", "content-length", "transfer-encoding")
        ]
        return httpx.Response(resp.status_code, headers=headers, content=resp.content)


def test_client_against_real_app(monkeypatch):
    monkeypatch.setattr(settings, "web_ui_username", "admin")
    monkeypatch.setattr(settings, "web_ui_password", "secretpw")

    from web.app import create_app

    app = create_app()
    good = TradingApiClient(
        base_url="http://testserver",
        username="admin",
        password="secretpw",
        transport=_ASGISyncTransport(app),
    )
    api_client.configure(good)
    try:
        assert tools_market.get_markets()["default"] in ("us", "ph")

        bad = TradingApiClient(
            base_url="http://testserver",
            username="admin",
            password="wrong",
            transport=_ASGISyncTransport(app),
        )
        api_client.configure(bad)
        with pytest.raises(TradingApiError) as exc:
            tools_market.get_markets()
        assert exc.value.status_code == 401
    finally:
        api_client.reset_client()


def test_history_calls_use_the_username_as_password_credential():
    """``/api/history/*`` needs user == password; the rest of ``/api/*`` the password.

    One credential cannot satisfy both, so a client with
    ``WEB_UI_PASSWORD != WEB_UI_USERNAME`` must send the history surface its own.
    """
    import base64

    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen[request.url.path] = request.headers["authorization"]
        return httpx.Response(200, json={})

    client = TradingApiClient(
        base_url="http://testserver",
        username="admin",
        password="secretpw",
        history_username="admin",
        transport=httpx.MockTransport(handler),
    )
    client.get("/api/backtest/status")
    client.get("/api/history/symbols")
    client.get("/api/history/AAPL")

    def cred(header: str) -> str:
        return base64.b64decode(header.split(" ", 1)[1]).decode()

    assert cred(seen["/api/backtest/status"]) == "admin:secretpw"
    assert cred(seen["/api/history/symbols"]) == "admin:admin"
    assert cred(seen["/api/history/AAPL"]) == "admin:admin"

