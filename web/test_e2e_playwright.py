"""
Playwright e2e for `python main.py --web` (pass 2 — broader coverage).

    WEB_UI_PORT=18080 .venv/bin/python main.py --web
    .venv/bin/python web/test_e2e_playwright.py --base-url http://127.0.0.1:18080
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

PNG_1X1 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8"
    "z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def _creds() -> tuple[str, str]:
    from config import settings

    return settings.web_ui_username, settings.web_ui_password


def _wait_health(base: str, timeout_s: float = 25.0) -> None:
    deadline = time.time() + timeout_s
    last = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{base}/health", timeout=1.5) as r:
                if r.status == 200:
                    return
        except Exception as exc:  # noqa: BLE001
            last = exc
        time.sleep(0.25)
    raise RuntimeError(f"Server not healthy at {base}/health: {last}")


def _opener(jar: CookieJar):
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


def _api_login(base: str, user: str, password: str) -> CookieJar:
    jar = CookieJar()
    data = urllib.parse.urlencode(
        {"username": user, "password": password, "next": "/"}
    ).encode()
    req = urllib.request.Request(f"{base}/login", data=data, method="POST")
    try:
        _opener(jar).open(req)
    except urllib.error.HTTPError as exc:
        if exc.code not in (301, 302, 303, 307, 308):
            raise
    return jar


def _api_json(
    base: str,
    jar: CookieJar,
    method: str,
    path: str,
    payload: dict | None = None,
) -> tuple[int, object]:
    body = None
    headers: dict[str, str] = {}
    if payload is not None:
        body = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(
        f"{base}{path}", data=body, headers=headers, method=method,
    )
    try:
        with _opener(jar).open(req) as resp:
            raw = resp.read().decode()
            return resp.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        try:
            data = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            data = raw
        return exc.code, data


def _stub_status(running: bool = False, status: str = "Idle (e2e)") -> dict:
    return {
        "running": running,
        "status": status,
        "error": None,
        "use_stream": False,
        "cash": 100000,
        "equity": 100000,
        "open_count": 0,
        "closed_count": 0,
        "exposure": {"long_pct": 0, "short_pct": 0, "net_pct": 0},
        "scan_stats": None,
        "positions": [],
        "closed": [],
        "signal_logs": [],
        "summary": "No closed trades yet.",
        "equity_png_b64": None,
        "defaults": {"kronos_gate": True, "volume_gate": False, "n_symbols": 100},
    }


def run_e2e(base_url: str, headed: bool = False) -> None:
    user, password = _creds()
    if not password:
        raise RuntimeError("WEB_UI_PASSWORD empty — cannot login")

    base = base_url.rstrip("/")
    _wait_health(base)
    checks: list[str] = []

    # ── API smoke ───────────────────────────────────────────────────────
    code, _ = _api_json(base, CookieJar(), "GET", "/api/paper/status")
    assert code == 401, f"unauth paper status expected 401, got {code}"
    checks.append("api_unauth_401")

    jar = _api_login(base, user, password)
    code, body = _api_json(
        base,
        jar,
        "POST",
        "/api/paper/start",
        {
            "n_symbols": 1000,
            "use_stream": False,
            "kronos_gate": False,
            "volume_gate": False,
            "stream_start": None,
        },
    )
    assert code in (200, 409), f"paper/start n=1000 failed: {code} {body}"
    detail = str(body.get("detail", "")) if isinstance(body, dict) else str(body)
    assert "less_than_equal" not in detail, body
    assert "validation" not in detail.lower(), body
    checks.append("api_paper_start_1000")
    _api_json(base, jar, "POST", "/api/paper/stop")
    checks.append("api_paper_stop")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed)
        page = browser.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda err: errors.append(str(err)))

        # Login
        page.goto(f"{base}/login", wait_until="domcontentloaded")
        page.fill('input[name="username"]', user)
        page.fill('input[name="password"]', "wrong-password")
        page.click('button[type="submit"]')
        expect(page.locator(".error")).to_contain_text("Invalid", timeout=5000)
        checks.append("login_reject")

        page.fill('input[name="username"]', user)
        page.fill('input[name="password"]', password)
        with page.expect_navigation(wait_until="domcontentloaded", timeout=15000):
            page.click('button[type="submit"]')
        expect(page.locator("header.topbar")).to_be_visible(timeout=10000)
        checks.append("login_ok")

        # Nav
        page.click('a[href="/backtest"]')
        expect(page.locator("#bt-form")).to_be_visible(timeout=10000)
        page.click('a[href="/paper"]')
        expect(page.locator("#paper-start")).to_be_visible(timeout=10000)
        page.click('a[href="/"]')
        expect(page.locator("#symbol-list")).to_be_visible(timeout=10000)
        checks.append("nav_all")

        # Explorer stubs
        page.route(
            "**/api/symbols*",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(
                    {
                        "symbols": [
                            {"symbol": "AAPL", "exchange": "NASDAQ"},
                            {"symbol": "MSFT", "exchange": "NASDAQ"},
                            {"symbol": "NVDA", "exchange": "NASDAQ"},
                        ]
                    }
                ),
            ),
        )
        page.route(
            "**/api/symbol",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(
                    {
                        "symbol": "AAPL",
                        "exchange": "NASDAQ",
                        "timeframe": "1d",
                        "bars": 2,
                        "ohlc": {
                            "open": 1.0,
                            "high": 2.0,
                            "low": 0.5,
                            "close": 1.5,
                            "change": 0.1,
                            "change_pct": 7.0,
                        },
                        "signals": [
                            {
                                "pattern": "demo",
                                "action": "BUY",
                                "timeframe": "1d",
                                "confidence": 0.9,
                                "price": 1.5,
                                "notes": "ok",
                            }
                        ],
                        "chart_png_b64": PNG_1X1,
                        "csv": "open,high,low,close\n1,2,0.5,1.5\n",
                    }
                ),
            ),
        )
        page.goto(f"{base}/", wait_until="networkidle")
        page.wait_for_function(
            "() => document.querySelectorAll('#symbol-list li').length >= 3",
            timeout=10000,
        )
        page.fill("#filter", "MS")
        page.wait_for_function(
            "() => document.querySelectorAll('#symbol-list li').length === 1",
            timeout=5000,
        )
        page.fill("#filter", "")
        page.click("#refresh-symbols")
        page.wait_for_function(
            "() => document.querySelectorAll('#symbol-list li').length >= 3",
            timeout=10000,
        )
        page.check("#kronos-gate")
        page.check("#volume-gate")
        page.select_option("#timeframe", "1W")
        page.click("#symbol-list li >> nth=0")
        expect(page.locator("#header")).to_contain_text("AAPL", timeout=10000)
        expect(page.locator("#signals tbody tr")).to_have_count(1)
        expect(page.locator("#save-png")).to_be_enabled()
        expect(page.locator("#save-csv")).to_be_enabled()
        checks.append("explorer")

        # Backtest
        page.unroute("**/api/symbols*")
        page.unroute("**/api/symbol")
        captured: dict = {}

        def handle_bt_run(route):
            captured["run"] = route.request.post_data_json
            route.fulfill(status=200, content_type="application/json", body='{"ok":true}')

        def handle_bt_ab(route):
            captured["ab"] = route.request.post_data_json
            route.fulfill(status=200, content_type="application/json", body='{"ok":true}')

        page.route("**/api/backtest/run", handle_bt_run)
        page.route("**/api/backtest/ab", handle_bt_ab)
        page.route(
            "**/api/backtest/status",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(
                    {
                        "busy": False,
                        "mode": "run",
                        "status": "Done (e2e stub)",
                        "completed": 1,
                        "total": 1,
                        "pct": 100,
                        "elapsed_s": 1.0,
                        "eta_s": None,
                        "error": None,
                        "result": {
                            "summary": "e2e summary",
                            "win_rate": 0.5,
                            "win_count": 1,
                            "loss_count": 1,
                            "trade_count": 2,
                            "trades": [
                                {
                                    "date": "2026-01-02",
                                    "action": "BUY",
                                    "symbol": "AAPL",
                                    "tf": "1d",
                                    "entry": 1.0,
                                    "exit": 1.1,
                                    "pnl_pct": 10.0,
                                    "reason": "tp",
                                    "pattern": "demo",
                                }
                            ],
                        },
                        "ab": {"off": {"trades": 1}, "on": {"trades": 0}},
                    }
                ),
            ),
        )
        page.goto(f"{base}/backtest", wait_until="networkidle")
        page.fill("#p-n_symbols", "1000")
        page.click("#bt-run")
        page.wait_for_timeout(400)
        assert captured.get("run") is not None
        assert int(float(captured["run"]["n_symbols"])) == 1000
        expect(page.locator("#bt-trades tbody tr")).to_have_count(1, timeout=5000)
        page.click("#bt-ab")
        page.wait_for_timeout(400)
        assert captured.get("ab") is not None
        assert int(float(captured["ab"]["n_symbols"])) == 1000
        checks.append("backtest_run_ab")

        # Paper
        paper_hits: dict = {"start": None, "stop": 0, "reset": 0}
        running = {"v": False}

        def handle_paper_start(route):
            paper_hits["start"] = route.request.post_data_json
            running["v"] = True
            route.fulfill(status=200, content_type="application/json", body='{"ok":true}')

        def handle_paper_stop(route):
            paper_hits["stop"] += 1
            running["v"] = False
            route.fulfill(status=200, content_type="application/json", body='{"ok":true}')

        def handle_paper_reset(route):
            paper_hits["reset"] += 1
            route.fulfill(status=200, content_type="application/json", body='{"ok":true}')

        def handle_paper_status(route):
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(
                    _stub_status(
                        running=running["v"],
                        status="Running (e2e)" if running["v"] else "Idle (e2e)",
                    )
                ),
            )

        page.route("**/api/paper/start", handle_paper_start)
        page.route("**/api/paper/stop", handle_paper_stop)
        page.route("**/api/paper/reset", handle_paper_reset)
        page.route("**/api/paper/status", handle_paper_status)

        page.goto(f"{base}/paper", wait_until="networkidle")
        expect(page.locator("#stream-date-wrap")).to_be_hidden()
        page.check("#paper-stream")
        expect(page.locator("#stream-date-wrap")).to_be_visible()
        page.uncheck("#paper-stream")
        page.fill("#paper-n", "1000")
        page.check("#paper-kronos")
        page.uncheck("#paper-volume")
        page.click("#paper-start")
        page.wait_for_timeout(600)
        assert paper_hits["start"] is not None, "paper/start not called"
        assert paper_hits["start"]["n_symbols"] == 1000, paper_hits["start"]
        assert paper_hits["start"]["kronos_gate"] is True
        assert paper_hits["start"]["volume_gate"] is False
        status_text = page.locator("#paper-status").inner_text()
        assert "less_than_equal" not in status_text
        assert "validation error" not in status_text.lower()
        expect(page.locator("#paper-stop")).to_be_enabled(timeout=8000)
        page.click("#paper-stop")
        page.wait_for_timeout(400)
        assert paper_hits["stop"] >= 1
        page.once("dialog", lambda d: d.accept())
        page.click("#paper-reset")
        page.wait_for_timeout(400)
        assert paper_hits["reset"] >= 1
        checks.append("paper_flow")

        page.click('form[action="/logout"] button')
        page.wait_for_url("**/login**", timeout=10000)
        checks.append("logout")

        browser.close()
        if errors:
            raise RuntimeError("JS page errors:\n" + "\n".join(errors))

    print("E2E PASS2 OK:", ", ".join(checks))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://127.0.0.1:8080")
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--start-server", action="store_true")
    args = ap.parse_args()

    proc = None
    if args.start_server:
        proc = subprocess.Popen(
            [sys.executable, "main.py", "--web"],
            cwd=str(REPO),
            env=os.environ.copy(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        try:
            run_e2e(args.base_url, headed=args.headed)
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
    else:
        run_e2e(args.base_url, headed=args.headed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
