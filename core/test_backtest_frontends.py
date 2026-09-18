"""P07 frontend contracts: web endpoints use the shared durable service.

The web tests drive the real FastAPI app with a TestClient against a disposable
PostgreSQL schema and a fixture dataset. The desktop panel shares the same
``request_from_values`` builder, which is asserted here without a display.
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from core.pattern_editor_contracts import BacktestRequest, VersionSelection

BARCA = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "barcache"
PATTERN = "pattern_003_double_bottom"
SYMBOL = "CDNS"


def _rows(symbol: str = SYMBOL) -> list[list]:
    from data.barcache import load as load_barcache

    candles = load_barcache("us", symbol, root=BARCA)
    assert candles
    return [[c.timestamp.isoformat(), float(c.open), float(c.high), float(c.low),
             float(c.close), float(c.volume or 0.0)] for c in candles]


def _date(rows, index):
    return datetime.fromisoformat(rows[index][0]).date().isoformat()


@pytest.fixture
def web_env(published_pattern_catalog, monkeypatch):
    """Authenticated client wired to the fixture PostgreSQL catalog."""
    from core.backtest_service import BacktestService
    from web import runs as runs_module

    service = BacktestService(published_pattern_catalog, dataset_root=BARCA)
    monkeypatch.setattr(runs_module.backtest_runs, "_service", service)
    monkeypatch.setattr(runs_module.backtest_runs, "_threads", {})

    patches = [patch("web.auth.settings"), patch("web.app.settings")]
    auth_settings, app_settings = patches[0].start(), patches[1].start()
    for mock in (auth_settings, app_settings):
        mock.web_ui_password = "correct-horse"
        mock.web_ui_username = "admin"
        mock.web_ui_secret_key = "test-secret-key"
        mock.web_ui_https = False
        mock.web_ui_session_hours = 12
        mock.kronos_gate_enabled = True
        mock.volume_gate_enabled = False
    from web.app import create_app

    client = TestClient(create_app())
    login = client.post("/login", data={"username": "admin", "password": "correct-horse",
                                        "next": "/"}, follow_redirects=False)
    assert login.status_code == 303
    client.cookies.update(login.cookies)
    try:
        yield client, service, runs_module
    finally:
        for item in patches:
            item.stop()


def _body(service, rows, **overrides):
    version_id = next(p["active"] for p in service.versions.catalog() if p["id"] == PATTERN)
    body = {
        "mode": "historical-stream", "market": "us", "symbols": SYMBOL,
        "versions": {PATTERN: version_id},
        "start_date": _date(rows, 30), "end_date": _date(rows, -1),
        "warmup_bars": 30, "initial_capital": 100_000, "position_notional": 10_000,
        "txn_cost_pct": 0.001, "slippage_pct": 0.0, "end_policy": "keep-open",
        "preset_name": "web test",
    }
    body.update(overrides)
    return body


def _await(client, run_id, tries=120):
    payload = None
    for _ in range(tries):
        payload = client.get(f"/api/backtest/runs/{run_id}").json()
        if payload["state"] in ("completed", "failed", "cancelled", "blocked", "interrupted"):
            return payload
        import time
        time.sleep(0.25)
    raise AssertionError(f"run did not finish: {payload}")


def test_web_endpoints_require_auth():
    patches = [patch("web.auth.settings"), patch("web.app.settings")]
    mocks = [p.start() for p in patches]
    for mock in mocks:
        mock.web_ui_password = "correct-horse"
        mock.web_ui_username = "admin"
        mock.web_ui_secret_key = "k"
        mock.web_ui_https = False
        mock.web_ui_session_hours = 12
    try:
        from web.app import create_app
        client = TestClient(create_app())
        for path in ("/api/backtest/catalog", "/api/backtest/presets",
                     "/api/backtest/runs/x"):
            assert client.get(path).status_code == 401
    finally:
        for item in patches:
            item.stop()


def test_web_catalog_and_presets(web_env):
    client, service, _ = web_env
    catalog = client.get("/api/backtest/catalog").json()
    ids = {p["pattern_id"] for p in catalog["patterns"]}
    assert PATTERN in ids
    pattern = next(p for p in catalog["patterns"] if p["pattern_id"] == PATTERN)
    assert pattern["default_version_id"]
    versions = client.get(f"/api/backtest/catalog/{PATTERN}/versions").json()
    assert versions["versions"][0]["version_id"] == pattern["default_version_id"]
    assert client.get("/api/backtest/presets").json() == {"presets": []}


def test_web_stream_run_completes_and_is_reusable(web_env):
    client, service, _ = web_env
    rows = _rows()
    body = _body(service, rows, idempotency_key="web-key-1")
    first = client.post("/api/backtest/runs", json=body)
    assert first.status_code == 200, first.text
    run_id = first.json()["run_id"]

    duplicate = client.post("/api/backtest/runs", json=body)
    assert duplicate.json()["run_id"] == run_id  # duplicate Submit is idempotent

    payload = _await(client, run_id)
    assert payload["state"] == "completed"
    assert payload["unit"] == "sessions"
    assert payload["result"]["mode"] == "historical-stream"
    assert payload["result"]["metrics"]["trade_count"] >= 1
    assert payload["result"]["zero_trades"] is False
    assert payload["result"]["trades"]
    assert payload["result"]["end_policy"] == "keep-open"

    # a fresh app instance (page reload) still sees the persisted run
    reloaded = client.get(f"/api/backtest/runs/{run_id}").json()
    assert reloaded["state"] == "completed"
    assert reloaded["result"]["metrics"] == payload["result"]["metrics"]


def test_web_zero_trades_is_not_a_failure(web_env):
    client, service, _ = web_env
    rows = _rows()
    body = _body(service, rows, start_date=_date(rows, -2), end_date=_date(rows, -1),
                 warmup_bars=0)
    run_id = client.post("/api/backtest/runs", json=body).json()["run_id"]
    payload = _await(client, run_id)
    assert payload["state"] == "completed"
    assert payload["result"]["zero_trades"] is True
    assert payload["error"] is None


def test_web_rejects_malformed_and_unsupported_settings(web_env):
    client, service, _ = web_env
    rows = _rows()
    assert client.post("/api/backtest/runs", json=_body(service, rows, mode="nope")).status_code == 400
    assert client.post("/api/backtest/runs", json=_body(service, rows, market="de")).status_code == 400
    # stream without a start date
    bad = _body(service, rows, start_date=None)
    assert client.post("/api/backtest/runs", json=bad).status_code >= 400
    # unsupported gate combination is rejected by shared validation
    assert client.post("/api/backtest/runs", json=_body(service, rows, kronos_gate=True)).status_code == 400
    # unknown version is rejected
    unknown = _body(service, rows, versions={PATTERN: "does-not-exist"})
    assert client.post("/api/backtest/runs", json=unknown).status_code == 400


def test_web_missing_and_unavailable_database(web_env, monkeypatch):
    client, service, runs_module = web_env
    from core.pattern_editor_db import DatabaseUnavailable

    assert client.get("/api/backtest/runs/does-not-exist").status_code == 404

    def unavailable(*_a, **_k):
        raise DatabaseUnavailable("PostgreSQL unavailable; check editor connection")

    monkeypatch.setattr(runs_module.backtest_runs, "catalog", unavailable)
    resp = client.get("/api/backtest/catalog")
    assert resp.status_code == 503
    assert "unavailable" in resp.json()["detail"].lower()


def test_web_cancellation_is_durable(web_env, monkeypatch):
    client, service, runs_module = web_env
    rows = _rows()
    monkeypatch.setattr(runs_module.BacktestRuns, "_start", lambda self, run_id: None)
    run_id = client.post("/api/backtest/runs", json=_body(service, rows)).json()["run_id"]
    assert client.get(f"/api/backtest/runs/{run_id}").json()["state"] == "queued"
    cancelled = client.post(f"/api/backtest/runs/{run_id}/cancel").json()
    assert cancelled["state"] == "cancelled"
    payload = client.get(f"/api/backtest/runs/{run_id}").json()
    assert payload["state"] == "cancelled"
    assert payload["result"] is None
    # a cancelled run cannot be started afterwards
    with pytest.raises(Exception):
        service.execute(run_id)


def test_web_preset_save_and_reuse(web_env):
    client, service, _ = web_env
    rows = _rows()
    settings = _body(service, rows)
    settings.pop("versions")
    created = client.post("/api/backtest/presets", json={"name": "smoke", "settings": settings})
    assert created.status_code == 200, created.text
    preset = created.json()["preset"]
    assert preset["name"] == "smoke"
    assert preset["generation"] == 0
    listed = client.get("/api/backtest/presets").json()["presets"]
    assert [p["preset_id"] for p in listed] == [preset["preset_id"]]

    stale = client.post("/api/backtest/presets", json={
        "name": "smoke", "preset_id": preset["preset_id"],
        "expected_generation": 0, "settings": settings})
    assert stale.status_code == 200
    assert stale.json()["preset"]["generation"] == 1
    conflict = client.post("/api/backtest/presets", json={
        "name": "smoke", "preset_id": preset["preset_id"],
        "expected_generation": 0, "settings": settings})
    assert conflict.status_code == 409


def test_desktop_and_web_build_the_same_request(published_pattern_catalog):
    """The desktop panel and the web endpoint share one request builder."""
    from core.backtest_params import REPLAY_PARAMS, settings_from_values
    from core.backtest_service import BacktestService, request_from_values
    from core.market import parse_extra_symbols

    service = BacktestService(published_pattern_catalog, dataset_root=BARCA)
    rows = _rows()

    # desktop: defaults from the shared REPLAY_PARAMS plus the main-form fields
    values = {key: (default[0] if ptype == "spin" else default)
              for key, _label, _desc, ptype, default, _choices in REPLAY_PARAMS}
    values.update({
        "market": "us", "timeframe": "1d", "symbols": parse_extra_symbols(SYMBOL),
        "universe": None, "mode": "historical-stream",
        "start_date": _date(rows, 30), "end_date": _date(rows, -1),
        "warmup_bars": 30, "initial_capital": 100_000.0, "position_notional": 10_000.0,
        "txn_cost_pct": 0.001, "slippage_pct": 0.0, "end_policy": "keep-open",
    })
    desktop = request_from_values(
        service, values, versions={PATTERN: next(
            p["active"] for p in service.versions.catalog() if p["id"] == PATTERN)},
        preset_name="desktop smoke")

    web_settings = settings_from_values({**_body(service, rows), "mode": "historical-stream"})
    assert desktop.preset.settings == web_settings
    assert isinstance(desktop, BacktestRequest)
    assert desktop.versions[0].pattern_id == PATTERN


def test_headless_web_imports_do_not_require_tkinter():
    import subprocess

    code = (
        "import sys, web.app, web.jobs, web.runs; "
        "print('tkinter' in sys.modules)"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         cwd=str(Path(__file__).resolve().parents[1]))
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "False"
