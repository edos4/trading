"""History API auth + JSON shape (mocked Postgres)."""

from __future__ import annotations

import signal
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient


class HistoryApiTests(unittest.TestCase):
    TIMEOUT_SECONDS = 60

    def _on_timeout(self, signum, frame) -> None:
        raise TimeoutError(f"history API test exceeded {self.TIMEOUT_SECONDS}s")

    def setUp(self) -> None:
        self._prev = signal.signal(signal.SIGALRM, self._on_timeout)
        signal.alarm(self.TIMEOUT_SECONDS)
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

            from web.app import create_app

            self.client = TestClient(create_app())
        except BaseException:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, self._prev)
            raise

    def tearDown(self) -> None:
        try:
            for p in self.patches:
                p.stop()
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, self._prev)

    def test_history_requires_auth_www_authenticate(self) -> None:
        r = self.client.get("/api/history/AAPL")
        self.assertEqual(r.status_code, 401)
        self.assertIn("Basic", r.headers.get("www-authenticate", ""))

    def test_history_basic_auth_and_payload(self) -> None:
        bars = [
            {
                "ts": 1704456000,
                "date": "2024-01-05",
                "open": 1.0,
                "high": 2.0,
                "low": 0.5,
                "close": 1.5,
                "volume": 100,
            }
        ]
        with patch("data.db.load_daily_ohlcv_rows", return_value=bars):
            r = self.client.get("/api/history/aapl", auth=("admin", "admin"))
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["symbol"], "AAPL")
        self.assertEqual(body["bars"][0]["close"], 1.5)

    def test_history_symbols_basic(self) -> None:
        rows = [
            {"symbol": "MSFT", "market": "us", "last_bar_ts": 1, "row_count": 9},
        ]
        with patch("data.db.get_conn", return_value=_Conn()), \
             patch("data.db.all_symbols", return_value=rows):
            r = self.client.get("/api/history/symbols", auth=("admin", "admin"))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["symbols"][0]["symbol"], "MSFT")

    def test_history_empty_404(self) -> None:
        with patch("data.db.load_daily_ohlcv_rows", return_value=[]):
            r = self.client.get("/api/history/NOPE", auth=("admin", "admin"))
        self.assertEqual(r.status_code, 404)

    def test_history_ph_market_query(self) -> None:
        bars = [
            {
                "ts": 1704456000,
                "date": "2024-01-05",
                "open": 1.0,
                "high": 2.0,
                "low": 0.5,
                "close": 126.0,
                "volume": 100,
            }
        ]
        with patch("data.db.load_daily_ohlcv_rows", return_value=bars) as load:
            r = self.client.get(
                "/api/history/BDO?market=ph", auth=("admin", "admin"),
            )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["symbol"], "BDO.PS")
        load.assert_called_once()
        self.assertEqual(load.call_args.kwargs.get("market"), "ph")

    def test_history_symbols_market_filter(self) -> None:
        rows = [
            {"symbol": "BDO.PS", "market": "ph", "last_bar_ts": 1, "row_count": 9},
        ]
        with patch("data.db.get_conn", return_value=_Conn()), \
             patch("data.db.all_symbols", return_value=rows) as all_sym:
            r = self.client.get(
                "/api/history/symbols?market=ph", auth=("admin", "admin"),
            )
        self.assertEqual(r.status_code, 200)
        all_sym.assert_called_once()
        self.assertEqual(all_sym.call_args.kwargs.get("market"), "ph")
        self.assertEqual(r.json()["symbols"][0]["symbol"], "BDO.PS")

    def test_history_rejects_dashboard_password(self) -> None:
        r = self.client.get("/api/history/AAPL", auth=("admin", "correct-horse"))
        self.assertEqual(r.status_code, 401)


class _Conn:
    def close(self) -> None:
        return None


if __name__ == "__main__":
    unittest.main()
