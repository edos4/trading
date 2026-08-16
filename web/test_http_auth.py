"""HTTP-level auth checks for the web UI."""

from __future__ import annotations

import signal
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient


class WebHttpAuthTests(unittest.TestCase):
    # The full-suite run has historically hung inside TestClient(create_app())
    # (matplotlib/import interactions). Guard every test so CI fails fast
    # instead of stalling indefinitely on a broken auth path.
    TIMEOUT_SECONDS = 60

    def _on_timeout(self, signum, frame) -> None:
        raise TimeoutError(f"HTTP auth test exceeded {self.TIMEOUT_SECONDS}s")

    def _arm_timeout(self) -> None:
        self._prev_alarm_handler = signal.signal(signal.SIGALRM, self._on_timeout)
        signal.alarm(self.TIMEOUT_SECONDS)

    def _disarm_timeout(self) -> None:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, self._prev_alarm_handler)

    def setUp(self) -> None:
        self._arm_timeout()
        try:
            self.patches = [
                patch("web.auth.settings"),
                patch("web.app.settings"),
            ]
            self.auth_s = self.patches[0].start()
            self.app_s = self.patches[1].start()
            for s in (self.auth_s, self.app_s):
                s.web_ui_password = "correct-horse"
                s.web_ui_username = "admin"
                s.web_ui_secret_key = "test-secret-key"
                s.web_ui_https = False
                s.web_ui_session_hours = 12
                s.kronos_gate_enabled = True
                s.volume_gate_enabled = False

            from web.app import create_app

            self.client = TestClient(create_app())
        except BaseException:
            self._disarm_timeout()
            raise

    def tearDown(self) -> None:
        try:
            for p in self.patches:
                p.stop()
        finally:
            self._disarm_timeout()

    def test_health_open(self) -> None:
        r = self.client.get("/health")
        self.assertEqual(r.status_code, 200)

    def test_api_requires_auth(self) -> None:
        r = self.client.get("/api/symbols")
        self.assertEqual(r.status_code, 401)

    def test_page_redirects_to_login(self) -> None:
        r = self.client.get("/", follow_redirects=False)
        self.assertIn(r.status_code, (303, 307, 401))
        if r.status_code in (303, 307):
            self.assertTrue(r.headers["location"].startswith("/login"))

    def test_login_and_access(self) -> None:
        bad = self.client.post(
            "/login",
            data={"username": "admin", "password": "wrong", "next": "/"},
            follow_redirects=False,
        )
        self.assertEqual(bad.status_code, 401)

        ok = self.client.post(
            "/login",
            data={"username": "admin", "password": "correct-horse", "next": "/"},
            follow_redirects=False,
        )
        self.assertEqual(ok.status_code, 303)
        self.assertIn("tb_session", ok.cookies)

        # reuse cookie jar from client
        self.client.cookies.update(ok.cookies)
        r = self.client.get("/api/backtest/status")
        self.assertEqual(r.status_code, 200)
        self.assertIn("busy", r.json())


if __name__ == "__main__":
    unittest.main()
