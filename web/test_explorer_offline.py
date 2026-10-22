"""Explorer endpoints must not depend on the pattern-editor database."""

from __future__ import annotations

import signal
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient


class ExplorerOfflineTests(unittest.TestCase):
    TIMEOUT_SECONDS = 60

    def _on_timeout(self, signum, frame) -> None:
        raise TimeoutError(f"explorer offline test exceeded {self.TIMEOUT_SECONDS}s")

    def setUp(self) -> None:
        self._prev = signal.signal(signal.SIGALRM, self._on_timeout)
        signal.alarm(self.TIMEOUT_SECONDS)
        try:
            self.patches = [
                patch("web.auth.settings"), patch("web.app.settings"),
                # Module-level cache is shared across tests in a process.
                patch("web.services._explorer", None),
            ]
            for s in [p.start() for p in self.patches][:2]:
                s.web_ui_password = "correct-horse"
                s.web_ui_username = "admin"
                s.web_ui_secret_key = "test-secret-key"
                s.web_ui_https = False
                s.web_ui_session_hours = 12

            from web.app import create_app

            self.client = TestClient(create_app(), raise_server_exceptions=False)
            self.auth = ("admin", "correct-horse")
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

    def _unavailable(self):
        from core.pattern_editor_db import DatabaseUnavailable

        return DatabaseUnavailable("PostgreSQL unavailable or operation timed out")

    def test_symbols_work_while_editor_database_is_unavailable(self) -> None:
        rows = [("AAPL", "NASDAQ"), ("MSFT", "NASDAQ")]
        with patch("web.services.discover_patterns", side_effect=self._unavailable()) as resolve, \
             patch("data.tv_client.TVClient.fetch_universe_cached", return_value=rows):
            r = self.client.get("/api/symbols?n=50&market=us", auth=self.auth)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["symbols"][0], {"symbol": "AAPL", "exchange": "NASDAQ"})
        # Version resolution is deferred to the scan, never the constructor.
        self.assertFalse(resolve.called)

    def test_ph_symbol_list_uses_the_history_symbol(self) -> None:
        rows = [("BDO", "PSE"), ("ICT", "PSE")]
        with patch("data.tv_client.TVClient.fetch_universe_cached",
                   return_value=rows) as fetch:
            r = self.client.get("/api/symbols?n=30&market=ph", auth=self.auth)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["symbols"], [
            {"symbol": "BDO.PS", "exchange": "PSE"},
            {"symbol": "ICT.PS", "exchange": "PSE"},
        ])
        # Explorer must not apply the PH trading liquidity floor.
        fetch.assert_called_once_with(30, "ph", min_value=0)

    def test_ph_symbols_load_with_the_ph_market_suffix(self) -> None:
        from unittest.mock import ANY

        from web.services import ExplorerService

        # PSE screener rows are bare tickers (BDO); the history store keys them
        # as BDO.PS, which only happens when the market is passed through.
        with patch("web.services.fetch_ohlcv_candles", return_value=[]) as fetch:
            with self.assertRaises(ValueError):
                ExplorerService().load_symbol("BDO", "PSE", "1d", market="ph")
        fetch.assert_called_once_with(
            "BDO", "1d", exchange="PSE", tv_client=ANY,
            tv_fallback=False, market="ph",
        )

    def test_symbol_scan_maps_database_outage_to_503(self) -> None:
        outage = self._unavailable()

        class DeadExplorer:
            def load_symbol(self, *args, **kwargs):
                raise outage

        with patch("web.app.get_explorer", return_value=DeadExplorer()):
            r = self.client.post(
                "/api/symbol", auth=self.auth,
                json={"symbol": "AAPL", "exchange": "NASDAQ", "timeframe": "1d"},
            )
        self.assertEqual(r.status_code, 503)


if __name__ == "__main__":
    unittest.main()
