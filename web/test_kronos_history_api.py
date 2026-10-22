"""Kronos forecast-history endpoints, with the GPU and PostgreSQL layers mocked."""

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
        "base": [{"time": "2026-10-15", "value": 249.0}],
        "finetuned": [{"time": "2026-10-15", "value": 247.5}],
    }],
}

STATS = {
    "saved": {"predictions": 2, "symbols": 1,
              "first_anchor": "2026-10-14", "last_anchor": "2026-10-14"},
    "pending": 0,
    "graded": 2,
    "models": {
        "base": {"overall": {"wins": 1, "n": 1, "win_rate_pct": 100.0},
                 "days": [{"day": 1, "wins": 1, "n": 1, "win_rate_pct": 100.0}]},
        "finetuned": {"overall": {"wins": 0, "n": 1, "win_rate_pct": 0.0},
                      "days": [{"day": 1, "wins": 0, "n": 1, "win_rate_pct": 0.0}]},
    },
}


class FakeStore:
    def __init__(self) -> None:
        self.saved: tuple[dict, str] | None = None
        self.graded = 0

    def save_payload(self, payload: dict, *, saved_by: str) -> dict:
        self.saved = (payload, saved_by)
        return {"predictions": 2, "symbols": ["AAPL"],
                "anchor_dates": ["2026-10-14"], "skipped": []}

    def grade(self) -> int:
        self.graded += 1
        return 2

    def stats(self) -> dict:
        return STATS

    def recent(self, limit: int = 500) -> dict:
        self.recent_limit = limit
        return {
            "rows": [{
                "symbol": "AAPL", "anchor_date": "2026-10-14",
                "anchor_close": 250.0, "model": "base", "day_ahead": 1,
                "forecast_date": "2026-10-15", "predicted_close": 249.0,
                "saved_by": "admin", "saved_at": "2026-10-14T20:00:00",
                "actual_date": "2026-10-15", "actual_close": 252.0,
                "correct": True, "graded_at": "2026-10-16T20:00:00",
            }],
            "total": 1,
            "limit": limit,
        }


class WebKronosHistoryApiTests(unittest.TestCase):
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
        self.store = FakeStore()
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
        self.assertEqual(self.client.post("/api/kronos/save", json={"days": 5}).status_code, 401)
        self.assertEqual(self.client.get("/api/kronos/winrate").status_code, 401)
        self.assertEqual(self.client.post("/api/kronos/winrate/refresh").status_code, 401)

    def test_save_uses_tracked_symbols(self) -> None:
        self._login()
        with patch("web.kronos.forecast", return_value=PAYLOAD) as forecast, \
                patch("web.kronos_history._store", return_value=self.store):
            r = self.client.post("/api/kronos/save", json={"days": 3})
        self.assertEqual(r.status_code, 200)
        forecast.assert_called_once_with(["AAPL", "NVDA"], 3)
        self.assertEqual(self.store.saved[0], PAYLOAD)
        self.assertEqual(self.store.saved[1], "admin")
        self.assertEqual(r.json()["saved"]["predictions"], 2)
        self.assertEqual(r.json()["symbols"], ["AAPL"])

    def test_save_defaults_to_configured_days(self) -> None:
        self._login()
        with patch("web.kronos.forecast", return_value=PAYLOAD) as forecast, \
                patch("web.kronos_history._store", return_value=self.store):
            r = self.client.post("/api/kronos/save", json={})
        self.assertEqual(r.status_code, 200)
        forecast.assert_called_once_with(["AAPL", "NVDA"], 5)

    def test_save_rejects_bad_days(self) -> None:
        self._login()
        for days in (0, 61, "x"):
            r = self.client.post("/api/kronos/save", json={"days": days})
            self.assertEqual(r.status_code, 400, days)

    def test_save_requires_tracked_symbols(self) -> None:
        self._login()
        self.history_settings.kronos_symbols = []
        r = self.client.post("/api/kronos/save", json={"days": 5})
        self.assertEqual(r.status_code, 400)
        self.assertIn("TRACKED", r.json()["detail"])

    def test_save_rejects_oversized_tracked_list(self) -> None:
        self._login()
        from web import kronos

        self.history_settings.kronos_symbols = [
            f"S{i}" for i in range(kronos.MAX_SYMBOLS + 1)]
        r = self.client.post("/api/kronos/save", json={"days": 5})
        self.assertEqual(r.status_code, 400)
        self.assertIn("limit", r.json()["detail"])

    def test_save_maps_forecast_busy(self) -> None:
        self._login()
        from web import kronos

        with patch("web.kronos.forecast", side_effect=kronos.KronosBusy("busy")), \
                patch("web.kronos_history._store", return_value=self.store):
            r = self.client.post("/api/kronos/save", json={"days": 5})
        self.assertEqual(r.status_code, 409)

    def test_save_maps_unmigrated_storage(self) -> None:
        self._login()
        from core.kronos_db import KronosMigrationRequired

        with patch("web.kronos.forecast", return_value=PAYLOAD), \
                patch.object(self.store, "save_payload",
                             side_effect=KronosMigrationRequired("missing")), \
                patch("web.kronos_history._store", return_value=self.store):
            r = self.client.post("/api/kronos/save", json={"days": 5})
        self.assertEqual(r.status_code, 503)
        self.assertIn("setup_kronos_db.py", r.json()["detail"])

    def test_predictions_returns_saved_rows(self) -> None:
        self._login()
        with patch("web.kronos_history._store", return_value=self.store):
            r = self.client.get("/api/kronos/predictions?limit=10")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.store.recent_limit, 10)
        self.assertEqual(r.json()["total"], 1)
        self.assertEqual(r.json()["rows"][0]["symbol"], "AAPL")
        self.assertTrue(r.json()["rows"][0]["correct"])

    def test_predictions_rejects_bad_limit(self) -> None:
        self._login()
        for limit in (0, 2001, "x"):
            r = self.client.get(f"/api/kronos/predictions?limit={limit}")
            self.assertEqual(r.status_code, 400, limit)

    def test_winrate_passthrough(self) -> None:
        self._login()
        with patch("web.kronos_history._store", return_value=self.store):
            r = self.client.get("/api/kronos/winrate")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), STATS)

    def test_refresh_grades_then_returns_stats(self) -> None:
        self._login()
        with patch("web.kronos_history._store", return_value=self.store):
            r = self.client.post("/api/kronos/winrate/refresh")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.store.graded, 1)
        self.assertEqual(r.json()["graded_now"], 2)
        self.assertEqual(r.json()["graded"], 2)

    def test_winrate_maps_unavailable_storage(self) -> None:
        self._login()
        from core.kronos_db import KronosDatabaseUnavailable

        with patch.object(self.store, "stats",
                          side_effect=KronosDatabaseUnavailable("down")), \
                patch("web.kronos_history._store", return_value=self.store):
            r = self.client.get("/api/kronos/winrate")
        self.assertEqual(r.status_code, 503)
        self.assertIn("unavailable", r.json()["detail"])


if __name__ == "__main__":
    unittest.main()
