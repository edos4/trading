"""HTTP checks for dual-book paper API."""

from __future__ import annotations

import signal
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient


class WebPaperApiTests(unittest.TestCase):
    TIMEOUT_SECONDS = 60

    def _on_timeout(self, signum, frame) -> None:
        raise TimeoutError(f"paper API test exceeded {self.TIMEOUT_SECONDS}s")

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

    def test_combined_status_envelope(self) -> None:
        self._login()
        r = self.client.get("/api/paper/status")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertIn("clocks", data)
        self.assertIn("books", data)
        self.assertIn("us", data["books"])
        self.assertIn("ph", data["books"])
        self.assertEqual(data["books"]["us"]["market"], "us")
        self.assertEqual(data["books"]["ph"]["market"], "ph")
        self.assertEqual(data["books"]["us"]["currency_symbol"], "$")
        self.assertEqual(data["books"]["ph"]["currency_symbol"], "₱")

    def test_single_book_status_query(self) -> None:
        self._login()
        r = self.client.get("/api/paper/status?market=us")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["market"], "us")
        self.assertNotIn("books", data)

    def test_start_requires_market(self) -> None:
        self._login()
        r = self.client.post(
            "/api/paper/start",
            json={"n_symbols": 10, "use_stream": False},
        )
        self.assertEqual(r.status_code, 400)
        self.assertIn("market", str(r.json().get("detail", "")).lower())

    def test_chart_requires_market(self) -> None:
        self._login()
        r = self.client.get("/api/paper/chart?side=open&symbol=AAPL")
        self.assertEqual(r.status_code, 400)
        self.assertIn("market", str(r.json().get("detail", "")).lower())

    def test_start_us_then_ph(self) -> None:
        self._login()
        body = {
            "n_symbols": 10,
            "use_stream": False,
            "kronos_gate": False,
            "kronos_rank": False,
            "volume_gate": False,
            "pattern_only": True,
        }
        with patch("web.app.paper_books.start", return_value=None) as start:
            r1 = self.client.post("/api/paper/start", json={**body, "market": "us"})
            r2 = self.client.post("/api/paper/start", json={**body, "market": "ph"})
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r2.status_code, 200)
        self.assertEqual([c.args[0] for c in start.call_args_list], ["us", "ph"])
        for call in start.call_args_list:
            self.assertTrue(call.kwargs.get("pattern_only"))

    def test_export_envelope(self) -> None:
        self._login()
        r = self.client.get("/api/paper/export")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["purpose"], "paper_trade_evaluation")
        self.assertEqual(data["filter"], "all")
        self.assertIn("review_prompt", data)
        markets = [b["market"] for b in data["books"]]
        self.assertEqual(markets, ["us", "ph"])
        for book in data["books"]:
            self.assertIn("open_positions", book)
            self.assertIn("closed_trades", book)
            self.assertNotIn("equity_png_b64", book)

    def test_export_market_filter(self) -> None:
        self._login()
        r = self.client.get("/api/paper/export?market=ph")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["filter"], "ph")
        self.assertEqual([b["market"] for b in data["books"]], ["ph"])

    def test_export_bad_market(self) -> None:
        self._login()
        r = self.client.get("/api/paper/export?market=eu")
        self.assertEqual(r.status_code, 400)

    def test_chart_accepts_log_side(self) -> None:
        self._login()
        with patch("web.app.paper_books.chart", return_value={"title": "TSLA"}) as chart:
            r = self.client.get("/api/paper/chart?side=log&market=us&symbol=TSLA")
            self.assertEqual(r.status_code, 200)
            chart.assert_called_once_with("us", side="log", symbol="TSLA", index=None)

    def test_replay_chart_bad_market(self) -> None:
        self._login()
        r = self.client.post(
            "/api/replay/chart",
            json={"market": "eu", "symbol": "AAPL", "side": "open"},
        )
        self.assertEqual(r.status_code, 400)
        self.assertIn("market", str(r.json().get("detail", "")).lower())

    def test_replay_chart_builds_payload(self) -> None:
        self._login()
        import pandas as pd

        df = pd.DataFrame(
            {
                "open": [10.0, 11.0],
                "high": [12.0, 12.0],
                "low": [9.0, 10.0],
                "close": [11.0, 11.5],
                "volume": [1000, 1200],
            }
        )
        with patch(
            "data.history.load_daily_ohlcv_df", return_value=df,
        ) as loader, patch(
            "analysis.chart_renderer.build_trade_viewer_payload",
            return_value={"title": "DY 1D · SELL"},
        ) as builder:
            r = self.client.post(
                "/api/replay/chart",
                json={
                    "market": "us",
                    "symbol": "DY",
                    "side": "open",
                    "action": "SELL",
                    "pattern": "pattern_008_head_and_shoulders",
                    "entry": 310.75,
                    "stop": 348.04,
                    "target": 202.02,
                    "current": 295.56,
                    "entry_time": "2026-09-04T01:43:36+00:00",
                },
            )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["title"], "DY 1D · SELL")
        loader.assert_called_once_with("DY", tv_fallback=False, market="us")
        self.assertEqual(builder.call_args.kwargs["entry"], 310.75)
        self.assertEqual(builder.call_args.kwargs["exit_price"], None)
        self.assertEqual(builder.call_args.kwargs["current"], 295.56)

    def test_replay_chart_closed_side(self) -> None:
        self._login()
        import pandas as pd

        df = pd.DataFrame(
            {
                "open": [10.0, 11.0],
                "high": [12.0, 12.0],
                "low": [9.0, 10.0],
                "close": [11.0, 11.5],
                "volume": [1000, 1200],
            }
        )
        with patch(
            "data.history.load_daily_ohlcv_df", return_value=df,
        ), patch(
            "analysis.chart_renderer.build_trade_viewer_payload",
            return_value={"title": "SYNA 1D · BUY"},
        ) as builder:
            r = self.client.post(
                "/api/replay/chart",
                json={
                    "market": "us",
                    "symbol": "SYNA",
                    "side": "closed",
                    "action": "BUY",
                    "exit": 84.0,
                    "exit_reason": "trailing_stop",
                    "entry": 80.0,
                    "entry_time": "2026-09-01T00:00:00+00:00",
                    "exit_time": "2026-09-04T00:46:41+00:00",
                },
            )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(builder.call_args.kwargs["exit_price"], 84.0)
        self.assertEqual(builder.call_args.kwargs["exit_reason"], "trailing_stop")
        self.assertEqual(builder.call_args.kwargs["current"], None)

    def test_replay_upload_requires_books(self) -> None:
        self._login()
        r = self.client.post("/api/replay/upload", json={"foo": 1})
        self.assertEqual(r.status_code, 400)

    def test_replay_upload_load_clear_roundtrip(self) -> None:
        self._login()
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "replay.json"
            payload = {"books": [{"market": "us", "open_positions": [], "closed_trades": []}]}
            with patch("web.replay_store.REPLAY_PATH", path):
                up = self.client.post("/api/replay/upload", json=payload)
                self.assertEqual(up.status_code, 200)
                self.assertTrue(path.exists())

                ld = self.client.get("/api/replay/load")
                self.assertEqual(ld.status_code, 200)
                self.assertEqual(ld.json()["replay"]["books"][0]["market"], "us")

                cl = self.client.post("/api/replay/clear", json={})
                self.assertEqual(cl.status_code, 200)
                self.assertFalse(path.exists())

                ld2 = self.client.get("/api/replay/load")
                self.assertEqual(ld2.json()["replay"], None)

    def test_reset_logs_validates_market(self) -> None:
        self._login()
        with patch("web.app.paper_books.reset_logs") as reset_logs:
            bad = self.client.post("/api/paper/reset-logs", json={"market": "eu"})
            self.assertEqual(bad.status_code, 400)
            ok = self.client.post("/api/paper/reset-logs", json={"market": "ph"})
            self.assertEqual(ok.status_code, 200)
            reset_logs.assert_called_once_with("ph")
            both = self.client.post("/api/paper/reset-logs", json={"market": "all"})
            self.assertEqual(both.status_code, 200)
            reset_logs.assert_called_with("all")

    def test_lamps_status_is_light(self) -> None:
        self._login()
        r = self.client.get("/api/paper/status?lamps=1")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertIn("books", data)
        self.assertNotIn("clocks", data)
        self.assertEqual(set(data["books"]), {"us", "ph"})
        for book in data["books"].values():
            self.assertIn("running", book)
            self.assertNotIn("positions", book)
            self.assertNotIn("equity_png_b64", book)


if __name__ == "__main__":
    unittest.main()
