"""Standalone Playwright e2e for the pattern editor (P9.2/P9.3 browser half).

Requires a running authenticated web server with real paper data:

    .venv/bin/python main.py --web
    .venv/bin/python web/test_pattern_edit_e2e.py --base-url http://127.0.0.1:8080

It opens a closed paper trade, confirms the native editor panel mounts, submits
an instruction, and confirms Generate Preview fails closed with an actionable
provider error while the draft is preserved. It never calls Apply.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

SCREENSHOT = REPO / "web" / "pattern_editor_e2e.png"


def _creds() -> tuple[str, str]:
    from config import settings

    return settings.web_ui_username, settings.web_ui_password


def _open_editor(page) -> None:
    """Double-click closed rows until one yields an editable chart."""
    rows = page.locator("#paper-closed tbody tr")
    expect(rows.first).to_be_visible(timeout=20000)
    count = min(rows.count(), 8)
    for i in range(count):
        rows.nth(i).dblclick()
        try:
            page.locator("#pattern-editor").wait_for(state="visible", timeout=20000)
            if "Edit Pattern" in page.locator("#pattern-editor").inner_text():
                return
        except Exception:
            pass
        page.locator("#paper-chart-close").click()
    raise AssertionError("editor panel did not mount for any closed row")


def run(base_url: str, headed: bool = False) -> None:
    user, password = _creds()
    if not password:
        raise RuntimeError("WEB_UI_PASSWORD empty — cannot login")
    base = base_url.rstrip("/")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed)
        page = browser.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda err: errors.append(str(err)))

        page.goto(f"{base}/login", wait_until="domcontentloaded")
        page.fill('input[name="username"]', user)
        page.fill('input[name="password"]', password)
        with page.expect_navigation(wait_until="domcontentloaded", timeout=20000):
            page.click('button[type="submit"]')

        page.goto(f"{base}/paper?market=us", wait_until="domcontentloaded")
        _open_editor(page)

        panel = page.locator("#pattern-editor")
        for selector in ("#edit-chat", "#edit-send", "#edit-preview", "#edit-apply", "#edit-role", "#edit-snap"):
            expect(panel.locator(selector)).to_be_visible(timeout=10000)
        assert "How to edit" in panel.inner_text()

        page.fill("#edit-chat", "Prefer this later confirmed peak as the right shoulder")
        page.click("#edit-send")
        expect(page.locator("#edit-status")).to_contain_text("pattern_", timeout=30000)

        # Without a configured provider the preview must fail closed, not crash.
        page.click("#edit-preview")
        expect(page.locator("#edit-review")).to_contain_text("DEEPSEEK_API_KEY", timeout=45000)
        assert "Apply" in panel.inner_text()
        expect(page.locator("#edit-apply")).to_be_disabled()

        page.screenshot(path=str(SCREENSHOT), full_page=True)
        browser.close()
    assert not errors, f"page errors: {errors}"
    print(f"pattern-editor e2e OK — screenshot {SCREENSHOT}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    parser.add_argument("--headed", action="store_true")
    args = parser.parse_args()
    run(args.base_url, headed=args.headed)


if __name__ == "__main__":
    main()
