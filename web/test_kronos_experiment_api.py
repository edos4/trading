"""Experiment 2 endpoints, with the GPU and PostgreSQL layers mocked."""

from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

PAYLOAD = {
    "days": 5,
    "lookback": 400,
    "symbols": ["AAPL"],
    "results": [{
        "symbol": "AAPL",
        "days": 5,
        "lookback": 400,
        "anchor": {"time": "2026-10-14", "value": 250.0},
        "history": [{"time": "2026-10-13", "value": 245.0}],
        "base": [{"time": f"2026-10-{15 + i}", "value": 390.0 + i} for i in range(5)],
        "finetuned": [{"time": f"2026-10-{15 + i}", "value": 382.0 + i} for i in range(5)],
    }],
}

SUMMARY = {
    "saved": {"predictions": 2, "symbols": 1,
              "first_anchor": "2026-10-14", "last_anchor": "2026-10-14",
              "last_graded_anchor": "2026-10-14",
              "last_graded_at": "2026-10-16T21:00:00",
              "last_saved_at": "2026-10-14T21:00:00"},
    "graded": 2,
    "pending": 0,
    "horizons": {
        "1": {"base": {"graded": 1, "avg_accuracy_pct": 97.37},
              "finetuned": {"graded": 1, "avg_accuracy_pct": 99.47}},
        "5": {"base": {"graded": 1, "avg_accuracy_pct": 93.33},
              "finetuned": {"graded": 1, "avg_accuracy_pct": 99.73}},
    },
}

ROWS = [{
    "symbol": "AAPL", "anchor_date": "2026-10-14", "anchor_close": 250.0,
    "day_ahead": 1, "forecast_date": "2026-10-15",
    "base_close": 390.0, "finetuned_close": 382.0,
    "actual_date": "2026-10-15", "actual_close": 380.0,
    "base_accuracy_pct": 97.37, "finetuned_accuracy_pct": 99.47,
    "saved_by": "admin", "saved_at": "2026-10-14T21:00:00",
    "graded_at": "2026-10-16T21:00:00",
}]


class FakeExperimentStore:
    def __init__(self) -> None:
        self.saved: tuple[dict, str] | None = None
        self.graded = 0
        self.recent_limit = None

    def save_payload(self, payload: dict, *, saved_by: str) -> dict:
        self.saved = (payload, saved_by)
        return {"predictions": 2, "symbols": ["AAPL"],
                "anchor_dates": ["2026-10-14"], "horizons": [1, 5],
                "skipped": []}

    def grade(self) -> int:
        self.graded += 1
        return 2

    def recent(self, limit: int = 500) -> dict:
        self.recent_limit = limit
        return {"rows": ROWS[:limit], "total": 2, "limit": limit}

    def summary(self) -> dict:
        return SUMMARY


class WebKronosExperimentApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.history_settings = SimpleNamespace(
            kronos_symbols=["AAPL", "NVDA"], kronos_save_days=5)
        self.patches = [
            patch("web.auth.settings"),
            patch("web.app.settings"),
            patch("web.kronos_history.settings", self.history_settings),
        ]
        self.auth_s = self.patches[0].start()
        self.app_s = self.patches[1].start()
        self.patches[2].start()
        for s in (self.auth_s, self.app_s):
            s.web_ui_password = "correct-horse"
            s.web_ui_username = "admin"
            s.web_ui_secret_key = "test-secret-key"
            s.web_ui_https = False
            s.web_ui_session_hours = 12
            s.volume_gate_enabled = False
            s.hermes_dashboard_url = ""
            s.tasks_enabled = False
        self.store = FakeExperimentStore()
        from web.app import create_app

        self.client = TestClient(create_app())

    def tearDown(self) -> None:
        for p in self.patches:
            p.stop()

    def _login(self) -> None:
        ok = self.client.post(
            "/login",
            data={"username": "admin", "password": "correct-horse", "next": "/"},
            follow_redirects=False,
        )
        self.assertEqual(ok.status_code, 303)
        self.client.cookies.update(ok.cookies)

    def test_requires_login(self) -> None:
        self.assertEqual(self.client.get("/api/kronos/experiment2").status_code, 401)
        self.assertEqual(
            self.client.post("/api/kronos/experiment2/run").status_code, 401)
        self.assertEqual(
            self.client.post("/api/kronos/experiment2/refresh").status_code, 401)

    def test_run_forecasts_tracked_symbols_for_five_days(self) -> None:
        self._login()
        with patch("web.kronos.forecast", return_value=PAYLOAD) as forecast, \
                patch("web.kronos_history._experiment_store", return_value=self.store):
            r = self.client.post("/api/kronos/experiment2/run")
        self.assertEqual(r.status_code, 200)
        forecast.assert_called_once_with(["AAPL", "NVDA"], 5)
        self.assertEqual(self.store.saved[0], PAYLOAD)
        self.assertEqual(self.store.saved[1], "admin")
        self.assertEqual(r.json()["days"], 5)
        self.assertEqual(r.json()["saved"]["predictions"], 2)
        self.assertEqual(r.json()["graded_now"], 2)
        self.assertEqual(
            r.json()["summary"]["horizons"]["1"]["base"]["avg_accuracy_pct"], 97.37)
        self.assertEqual(
            r.json()["summary"]["horizons"]["5"]["finetuned"]["avg_accuracy_pct"],
            99.73)

    def test_run_requires_tracked_symbols(self) -> None:
        self._login()
        self.history_settings.kronos_symbols = []
        r = self.client.post("/api/kronos/experiment2/run")
        self.assertEqual(r.status_code, 400)
        self.assertIn("TRACKED", r.json()["detail"])

    def test_run_maps_forecast_busy(self) -> None:
        self._login()
        from web import kronos

        with patch("web.kronos.forecast", side_effect=kronos.KronosBusy("busy")):
            r = self.client.post("/api/kronos/experiment2/run")
        self.assertEqual(r.status_code, 409)

    def test_run_maps_unavailable_storage(self) -> None:
        self._login()
        from core.kronos_db import KronosDatabaseUnavailable

        with patch("web.kronos.forecast", return_value=PAYLOAD), \
                patch.object(self.store, "save_payload",
                             side_effect=KronosDatabaseUnavailable("down")), \
                patch("web.kronos_history._experiment_store", return_value=self.store):
            r = self.client.post("/api/kronos/experiment2/run")
        self.assertEqual(r.status_code, 503)
        self.assertIn("unavailable", r.json()["detail"])

    def test_get_returns_rows_and_summary(self) -> None:
        self._login()
        with patch("web.kronos_history._experiment_store", return_value=self.store):
            r = self.client.get("/api/kronos/experiment2?limit=10")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.store.recent_limit, 10)
        self.assertEqual(r.json()["total"], 2)
        self.assertEqual(r.json()["rows"][0]["symbol"], "AAPL")
        self.assertEqual(r.json()["summary"], SUMMARY)

    def test_get_rejects_bad_limit(self) -> None:
        self._login()
        for limit in (0, 2001, "x"):
            r = self.client.get(f"/api/kronos/experiment2?limit={limit}")
            self.assertEqual(r.status_code, 400, limit)

    def test_refresh_grades_and_returns_summary(self) -> None:
        self._login()
        with patch("web.kronos_history._experiment_store", return_value=self.store):
            r = self.client.post("/api/kronos/experiment2/refresh")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.store.graded, 1)
        self.assertEqual(r.json()["graded_now"], 2)
        self.assertEqual(r.json()["summary"], SUMMARY)


if __name__ == "__main__":
    unittest.main()
