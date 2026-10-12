"""browser-use end-to-end smoke test for the public web UI.

Drives the `browser-use` CLI (the browser_harness library) against a Chrome
DevTools endpoint: it launches a headless Chromium when none is supplied, logs
in over the public HTTPS URL, and checks the Explorer renders.

Prerequisites (kept out of the project venv — browser-use pins conflict with
the trading app's deps, so it lives in its own venv):

    python3 -m venv .venv-browseruse
    .venv-browseruse/bin/pip install browser-use playwright
    .venv-browseruse/bin/python -m playwright install chromium

Run (with run_web.sh up in another shell). Defaults to the local server; pass
the public URL when the Cloudflare Tunnel is in front of it:

    .venv-browseruse/bin/python web/test_e2e_browseruse.py
    .venv-browseruse/bin/python web/test_e2e_browseruse.py --base-url https://<public-host>

Exit code 0 on success, 1 on failure.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

BROWSER_USE_PY = REPO / ".venv-browseruse" / "bin" / "python"
BROWSER_USE_CLI = REPO / ".venv-browseruse" / "bin" / "browser-use"


def _creds() -> tuple[str, str]:
    from config import settings

    return settings.web_ui_username, settings.web_ui_password


def _browser_use_cli() -> str:
    if BROWSER_USE_CLI.exists():
        return str(BROWSER_USE_CLI)
    found = shutil.which("browser-use")
    if not found:
        raise SystemExit(
            "browser-use CLI not found. Create .venv-browseruse and install it:\n"
            f"  python3 -m venv {REPO / '.venv-browseruse'}\n"
            f"  {BROWSER_USE_PY} -m pip install browser-use playwright\n"
            f"  {BROWSER_USE_PY} -m playwright install chromium"
        )
    return found


def _chromium_binary() -> str:
    override = os.environ.get("BROWSER_USE_CHROME") or os.environ.get("CHROME_PATH")
    if override and Path(override).is_file():
        return override
    cache = Path.home() / ".cache" / "ms-playwright"
    for path in sorted(cache.glob("chromium-*/chrome-linux64/chrome"), reverse=True):
        if path.is_file():
            return str(path)
    raise SystemExit(
        "No Chromium found. Install one:\n"
        f"  {BROWSER_USE_PY} -m playwright install chromium\n"
        "(or set BROWSER_USE_CHROME=/path/to/chrome)"
    )


def _wait_cdp(port: int, timeout_s: float = 20.0) -> bool:
    deadline = time.time() + timeout_s
    url = f"http://127.0.0.1:{port}/json/version"
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.0) as r:
                if r.status == 200:
                    return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.3)
    return False


def _login_program(base: str, user: str, password: str) -> str:
    """browser-use CLI program (helpers are pre-imported by the harness)."""
    fill_js = (
        "(() => {"
        "  const u = document.querySelector('input[name=username]');"
        "  const p = document.querySelector('input[name=password]');"
        "  if (!u || !p) return 'no-form';"
        f"  u.value = {json.dumps(user)};"
        f"  p.value = {json.dumps(password)};"
        "  u.form.submit();"
        "  return 'submitted';"
        "})()"
    )
    return f"""
base = {json.dumps(base.rstrip('/'))}
tab = new_tab(base + "/login")
wait_for_load()
filled = js({json.dumps(fill_js)})
assert filled == "submitted", f"login form not found: {{filled}}"
wait_for_load()
url = current_tab()["url"]
title = current_tab()["title"]
body = js("document.body.innerText")
assert url.rstrip("/") == base, f"login did not reach dashboard: {{url}}"
assert "Explorer" in body, f"explorer page did not render (title={{title!r}})"
print("AFTER_LOGIN_URL:", url)
print("TITLE:", title)
print("BROWSERUSE_E2E_OK")
"""


def run(base_url: str, port: int = 9222) -> None:
    user, password = _creds()
    cli = _browser_use_cli()

    chrome = None
    cdp_url = os.environ.get("BU_CDP_URL", "").strip()
    if not cdp_url:
        cdp_url = f"http://127.0.0.1:{port}"
        profile = Path("/tmp") / f"bu-chrome-{os.getpid()}"
        shutil.rmtree(profile, ignore_errors=True)
        chrome = subprocess.Popen(
            [
                _chromium_binary(),
                "--headless=new",
                "--no-sandbox",
                "--disable-gpu",
                "--disable-dev-shm-usage",
                f"--remote-debugging-port={port}",
                "--remote-allow-origins=*",
                f"--user-data-dir={profile}",
                "about:blank",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if not _wait_cdp(port):
            chrome.terminate()
            raise SystemExit(f"Chromium CDP endpoint never came up on port {port}")

    env = os.environ.copy()
    env["BU_CDP_URL"] = cdp_url
    try:
        proc = subprocess.run(
            [cli],
            input=_login_program(base_url, user, password),
            text=True,
            capture_output=True,
            env=env,
            cwd=str(REPO),
            timeout=180,
        )
        sys.stdout.write(proc.stdout)
        if proc.stderr.strip():
            sys.stderr.write(proc.stderr)
        if proc.returncode != 0 or "BROWSERUSE_E2E_OK" not in proc.stdout:
            raise SystemExit(f"browser-use e2e failed (exit {proc.returncode})")
    finally:
        # Stop the harness daemon so it releases the CDP connection.
        subprocess.run([cli, "--reload"], env=env, capture_output=True, cwd=str(REPO))
        if chrome is not None:
            chrome.terminate()
            try:
                chrome.wait(timeout=5)
            except subprocess.TimeoutExpired:
                chrome.kill()
    print("PASS: browser-use reached, logged in, and rendered the Explorer at", base_url)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://localhost:8080")
    ap.add_argument("--cdp-port", type=int, default=9222)
    args = ap.parse_args()
    run(args.base_url, args.cdp_port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
