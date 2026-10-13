"""Indicators tab API: session, stacks, preview, and paper start."""

from __future__ import annotations

import signal
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient


class IndicatorsApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self._prev = signal.signal(
            signal.SIGALRM,
            lambda *_: (_ for _ in ()).throw(TimeoutError("indicators API")),
        )
        signal.alarm(60)
        self.patches = [
            patch("web.auth.settings"),
            patch("web.app.settings"),
        ]
        try:
            for started in (item.start() for item in self.patches):
                started.web_ui_password = "correct-horse"
                started.web_ui_username = "admin"
                started.web_ui_secret_key = "test-secret-key"
                started.web_ui_https = False
                started.web_ui_session_hours = 12
            from web.app import create_app

            self.client = TestClient(create_app())
            login = self.client.post(
                "/login",
                data={"username": "admin", "password": "correct-horse", "next": "/"},
                follow_redirects=False,
            )
            self.assertEqual(login.status_code, 303)
            self.client.cookies.update(login.cookies)
        except BaseException:
            signal.alarm(0)
            raise

    def tearDown(self) -> None:
        try:
            for item in self.patches:
                item.stop()
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, self._prev)

    def test_page_has_indicators_nav(self) -> None:
        page = self.client.get("/indicators")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Indicators", page.text)
        self.assertIn("ind-session", page.text)
        self.assertIn('href="/indicators"', page.text)

    def test_session_disabled(self) -> None:
        fake = type("Fake", (), {"session": staticmethod(lambda: {"ok": False, "reason": "disabled"})})()
        with patch("data.tv_studies.get_client", return_value=fake):
            response = self.client.get("/api/indicators/session")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": False, "reason": "disabled"})

    def test_stack_crud_cap_and_running_delete(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "stacks.json"
        fake = type("Fake", (), {"study_limit": 1})()
        body = {
            "name": "sma-of-rsi",
            "timeframe": "1d",
            "studies": [
                {"id": "STD;RSI", "source": {"type": "price", "series": "close"}},
                {"id": "STD;SMA", "source": {"type": "study", "study_index": 0, "plot": "plot_0"}},
            ],
            "signal": {
                "kind": "threshold", "plot": "plot_0", "study_index": 1,
                "level": 50, "direction": "above",
            },
            "stop": {"kind": "percent", "percent": 2},
            "exit_on_opposite": True,
        }
        with patch("data.tv_stacks.STACKS_PATH", path), patch("data.tv_stacks.get_client", return_value=fake):
            saved = self.client.post("/api/indicators/stacks", json=body)
            self.assertEqual(saved.status_code, 200, saved.text)
            stack_id = saved.json()["id"]
            listed = self.client.get("/api/indicators/stacks")
            self.assertEqual(listed.json()["stacks"][0]["id"], stack_id)
            too_many = {
                **body,
                "studies": body["studies"] + [
                    {"id": "STD;EMA", "source": {"type": "study", "study_index": 1, "plot": "plot_0"}},
                ],
            }
            rejected = self.client.post("/api/indicators/stacks", json=too_many)
            self.assertEqual(rejected.status_code, 400)
            from core.paper_books import paper_books

            book = paper_books.books["us"]
            book.running = True
            book.tv_stack_id = stack_id
            try:
                blocked = self.client.delete(f"/api/indicators/stacks/{stack_id}")
            finally:
                book.running = False
                book.tv_stack_id = ""
            self.assertEqual(blocked.status_code, 409)
            removed = self.client.delete(f"/api/indicators/stacks/{stack_id}")
            self.assertEqual(removed.status_code, 200)

    def test_paper_start_uses_the_stack(self) -> None:
        with patch("web.app.paper_books.start", return_value=None) as start:
            response = self.client.post("/api/paper/start", json={
                "market": "us",
                "n_symbols": 10,
                "use_stream": False,
                "pattern_only": True,
                "patterns_v2": True,
                "tv_stack_id": "stack-1",
            })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(start.call_args.kwargs["tv_stack_id"], "stack-1")
        self.assertTrue(start.call_args.kwargs["indicators_only"])
        self.assertFalse(start.call_args.kwargs["pattern_only"])
        self.assertFalse(start.call_args.kwargs["patterns_v2"])

    def test_preview_reports_the_signal_without_a_socket(self) -> None:
        fetched = {
            "studies": [{
                "id": "STD;SMA",
                "plots": [
                    {"time": 1_704_153_600, "plot_0": 40},
                    {"time": 1_704_240_000, "plot_0": 60},
                ],
            }],
            "bars": [],
        }
        stack = {
            "id": "stack-1",
            "name": "sma-of-rsi",
            "timeframe": "1d",
            "studies": [{"id": "STD;SMA"}],
            "signal": {
                "kind": "threshold", "plot": "plot_0", "study_index": 0,
                "level": 50, "direction": "above", "other": None, "other_study_index": None,
            },
            "stop": {"kind": "percent", "percent": 2, "plot": "plot_0", "study_index": None, "atr_period": 14},
            "exit_on_opposite": True,
        }

        class Fake:
            def fetch(self, symbol, exchange, timeframe, studies):
                return fetched

        with patch("data.tv_studies.get_client", return_value=Fake()), \
                patch("data.tv_stacks.get_stack", return_value=stack):
            response = self.client.post("/api/indicators/preview", json={
                "stack_id": "stack-1", "symbol": "AAPL", "exchange": "NASDAQ",
            })
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["signal"]["action"], "BUY")
        self.assertNotIn("frame", response.text)
