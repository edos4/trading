"""HTTP checks for the Kronos predict page/API."""

from __future__ import annotations

import signal
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient


class WebKronosApiTests(unittest.TestCase):
    TIMEOUT_SECONDS = 60

    def _on_timeout(self, signum, frame) -> None:
        raise TimeoutError(f"kronos API test exceeded {self.TIMEOUT_SECONDS}s")

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

    def _login(self) -> None:
        ok = self.client.post(
            "/login",
            data={"username": "admin", "password": "correct-horse", "next": "/"},
            follow_redirects=False,
        )
        self.assertEqual(ok.status_code, 303)
        self.client.cookies.update(ok.cookies)

    def test_page_requires_login(self) -> None:
        r = self.client.get("/kronos", follow_redirects=False)
        self.assertIn(r.status_code, (303, 307, 401))

    def test_page_ok_after_login(self) -> None:
        self._login()
        r = self.client.get("/kronos")
        self.assertEqual(r.status_code, 200)
        self.assertIn("kronos-symbol", r.text)
        self.assertIn("kronos-days", r.text)
        self.assertIn("kronos-chart-host", r.text)

    def test_predict_rejects_bad_symbol(self) -> None:
        self._login()
        r = self.client.post("/api/kronos/predict", json={"symbol": "", "days": 5})
        self.assertEqual(r.status_code, 400)

    def test_predict_returns_chart_payload(self) -> None:
        self._login()
        fake = {
            "title": "AAPL 1D · Kronos 5d",
            "symbol": "AAPL",
            "candles": [{"time": "2024-01-02", "open": 1, "high": 2, "low": 1, "close": 2}],
            "pred_candles": [
                {"time": "2024-01-03", "open": 2, "high": 3, "low": 2, "close": 2.5, "predicted": True}
            ],
            "forecast": [
                {"time": "2024-01-02", "value": 2},
                {"time": "2024-01-03", "value": 2.5},
            ],
            "pred": {
                "days": 5,
                "origin": "2024-01-02",
                "last_close": 2.0,
                "pred_close": 2.5,
                "pred_return_pct": 25.0,
            },
        }
        with patch("core.kronos_forecast.forecast_symbol", return_value=fake):
            r = self.client.post(
                "/api/kronos/predict",
                json={"symbol": "AAPL", "days": 5, "market": "us"},
            )
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["symbol"], "AAPL")
        self.assertTrue(data["pred_candles"])
        self.assertTrue(data["forecast"])


if __name__ == "__main__":
    unittest.main()
