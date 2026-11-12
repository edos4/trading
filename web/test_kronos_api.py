"""Kronos tab: page + the GPU forecast endpoint, without ever loading the model."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

PAYLOAD = {
    "days": 5,
    "lookback": 400,
    "symbols": ["AAPL"],
    "results": [
        {
            "symbol": "AAPL",
            "days": 5,
            "lookback": 400,
            "anchor": {"time": "2026-10-14", "value": 250.0},
            "history": [{"time": "2026-10-13", "value": 245.0}],
            "base": [{"time": "2026-10-15", "value": 249.0}],
            "finetuned": [{"time": "2026-10-15", "value": 247.5}],
        },
    ],
}


class WebKronosApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.patches = [patch("web.auth.settings"), patch("web.app.settings")]
        self.auth_s = self.patches[0].start()
        self.app_s = self.patches[1].start()
        for s in (self.auth_s, self.app_s):
            s.web_ui_password = "correct-horse"
            s.web_ui_username = "admin"
            s.web_ui_secret_key = "test-secret-key"
            s.web_ui_https = False
            s.web_ui_session_hours = 12
            s.volume_gate_enabled = False
            s.hermes_dashboard_url = ""
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

    def test_page_requires_login(self) -> None:
        r = self.client.get(
            "/kronos", headers={"Accept": "text/html"}, follow_redirects=False,
        )
        self.assertEqual(r.status_code, 303)
        self.assertIn("/login", r.headers["location"])

    def test_page_and_nav_link(self) -> None:
        self._login()
        page = self.client.get("/kronos")
        self.assertEqual(page.status_code, 200)
        self.assertIn('id="kronos-results"', page.text)
        self.assertLess(page.text.index('id="kronos-experiment3"'), page.text.index('id="kronos-experiment4"'))
        self.assertIn('/static/kronos_experiment3.js?v=3', page.text)
        self.assertIn('/static/arrow_experiment4.js?v=1', page.text)
        from html.parser import HTMLParser
        class Elements(HTMLParser):
            def __init__(self):
                super().__init__(); self.ids = []
            def handle_starttag(self, tag, attrs):
                self.ids.extend(value for name, value in attrs if name == 'id')
        elements = Elements(); elements.feed(page.text)
        self.assertEqual(len(elements.ids), len(set(elements.ids)), 'experiment controls must have unique IDs')
        self.assertIn('href="/kronos"', self.client.get("/").text)

    def test_forecast_requires_login(self) -> None:
        r = self.client.post("/api/kronos/forecast", json={"symbols": ["AAPL"], "days": 5})
        self.assertEqual(r.status_code, 401)

    def test_experiment3_requires_login(self) -> None:
        self.assertEqual(self.client.get("/api/kronos/experiment3").status_code, 401)

    def test_experiment3_reads_saved_data_without_inference(self) -> None:
        self._login()
        saved = {"status": "collecting", "results": PAYLOAD["results"], "actual_count": 0}
        with patch("core.kronos_experiment3.read_experiment", return_value=saved) as read, \
             patch("web.kronos.forecast") as forecast:
            response = self.client.get("/api/kronos/experiment3")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), saved)
        read.assert_called_once_with()
        forecast.assert_not_called()

    def test_experiment3_database_failure_is_clear(self) -> None:
        self._login()
        with patch("core.kronos_experiment3.read_experiment", side_effect=RuntimeError("private DSN")):
            response = self.client.get("/api/kronos/experiment3")
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("private DSN", response.text)

    def test_experiment3_report_requires_login(self) -> None:
        self.assertEqual(self.client.get("/api/kronos/experiment3/report").status_code, 401)

    def test_experiment3_report_download_uses_saved_results(self) -> None:
        self._login()
        from core import kronos_experiment3 as exp
        from core.test_kronos_experiment3 import result

        row = result()
        row["actuals"] = [{"time": "2026-11-11", "value": 110.}]
        saved = {"status": "collecting", "results": [row], "scores": exp.score([row]), "actual_count": 1}
        with patch("core.kronos_experiment3.read_experiment", return_value=saved) as read, \
             patch("web.kronos.forecast") as forecast:
            response = self.client.get("/api/kronos/experiment3/report")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["content-type"].startswith("text/markdown"))
        self.assertIn('attachment; filename="kronos_experiment3_direction_report.md"', response.headers["content-disposition"])
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertIn("| 2026-11-11 | +1D | 1/1 | 0 | 1 | 0 | 100.0% | 0 | 1 | 0.0% |", response.text)
        self.assertIn("| 2026-11-12 | +2D | 0/1 | 1 | Pending", response.text)
        read.assert_called_once_with()
        forecast.assert_not_called()

    def test_experiment3_report_failure_does_not_expose_database_details(self) -> None:
        self._login()
        with patch("core.kronos_experiment3.read_experiment", side_effect=RuntimeError("private DSN")):
            response = self.client.get("/api/kronos/experiment3/report")
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("private DSN", response.text)

    def test_experiment4_requires_login(self) -> None:
        self.assertEqual(self.client.get("/api/kronos/experiment4").status_code, 401)

    def test_experiment4_reads_saved_data_without_inference(self) -> None:
        self._login()
        saved = {"status": "collecting", "results": PAYLOAD["results"], "actual_count": 0}
        with patch("core.arrow_experiment4.read_experiment", return_value=saved) as read, \
             patch("web.kronos.forecast") as forecast:
            response = self.client.get("/api/kronos/experiment4")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), saved)
        read.assert_called_once_with()
        forecast.assert_not_called()

    def test_experiment4_database_failure_is_clear(self) -> None:
        self._login()
        with patch("core.arrow_experiment4.read_experiment", side_effect=RuntimeError("private DSN")):
            response = self.client.get("/api/kronos/experiment4")
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("private DSN", response.text)

    def test_experiment4_report_requires_login(self) -> None:
        self.assertEqual(self.client.get("/api/kronos/experiment4/report").status_code, 401)

    def test_experiment4_report_download_uses_saved_results(self) -> None:
        self._login()
        from core import arrow_experiment4 as exp
        from core.test_arrow_experiment4 import result, metadata

        row = result()
        row["actuals"] = [{"time": "2026-11-12", "value": 110.}]
        saved = {"status": "collecting", "metadata": metadata(), "results": [row],
                 "direction_report": exp.score([row], metadata())["arrow"], "actual_count": 1}
        with patch("core.arrow_experiment4.read_experiment", return_value=saved) as read, \
             patch("web.kronos.forecast") as forecast:
            response = self.client.get("/api/kronos/experiment4/report")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["content-type"].startswith("text/markdown"))
        self.assertIn('attachment; filename="kronos_experiment4_direction_report.md"', response.headers["content-disposition"])
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertIn("| 2026-11-12 | +1D | 1/1 | 1 | 1 | 0 | 0 | 0 | 100.0% |", response.text)
        self.assertIn("| 2026-11-16 | +3D | 0/1 | 0 | 0 | 0 | 0 | 1 | Pending", response.text)
        read.assert_called_once_with()
        forecast.assert_not_called()

    def test_experiment4_report_failure_does_not_expose_database_details(self) -> None:
        self._login()
        with patch("core.arrow_experiment4.read_experiment", side_effect=RuntimeError("private DSN")):
            response = self.client.get("/api/kronos/experiment4/report")
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("private DSN", response.text)

    def test_forecast_rejects_bad_symbol(self) -> None:
        self._login()
        r = self.client.post("/api/kronos/forecast", json={"symbol": "a b;rm", "days": 5})
        self.assertEqual(r.status_code, 400)
        self.assertIn("symbol", r.json()["detail"].lower())

    def test_forecast_rejects_bad_symbol_in_list(self) -> None:
        self._login()
        r = self.client.post(
            "/api/kronos/forecast", json={"symbols": "AAPL, A B;RM", "days": 5},
        )
        self.assertEqual(r.status_code, 400)

    def test_forecast_rejects_bad_days(self) -> None:
        self._login()
        for days in (0, 61, "x"):
            r = self.client.post("/api/kronos/forecast", json={"symbols": ["AAPL"], "days": days})
            self.assertEqual(r.status_code, 400, days)

    def test_forecast_rejects_empty_symbols(self) -> None:
        self._login()
        for body in ("", "  , ", [], None):
            r = self.client.post("/api/kronos/forecast", json={"symbols": body, "days": 5})
            self.assertEqual(r.status_code, 400, body)
            self.assertIn("at least one", r.json()["detail"].lower())

    def test_forecast_rejects_too_many_symbols(self) -> None:
        self._login()
        from web import kronos

        many = [f"S{i}" for i in range(kronos.MAX_SYMBOLS + 1)]
        r = self.client.post("/api/kronos/forecast", json={"symbols": many, "days": 5})
        self.assertEqual(r.status_code, 400)
        self.assertIn("at most", r.json()["detail"].lower())

    def test_forecast_returns_payload(self) -> None:
        self._login()
        with patch("web.kronos.forecast", return_value=PAYLOAD) as called:
            r = self.client.post("/api/kronos/forecast", json={"symbol": "aapl", "days": 5})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["results"][0]["symbol"], "AAPL")
        called.assert_called_once_with(["AAPL"], 5)

    def test_forecast_parses_comma_separated_symbols(self) -> None:
        self._login()
        with patch("web.kronos.forecast", return_value=PAYLOAD) as called:
            r = self.client.post(
                "/api/kronos/forecast", json={"symbols": "aapl, nvda ,msft", "days": 5},
            )
        self.assertEqual(r.status_code, 200)
        called.assert_called_once_with(["AAPL", "NVDA", "MSFT"], 5)

    def test_forecast_accepts_list_and_dedupes(self) -> None:
        self._login()
        with patch("web.kronos.forecast", return_value=PAYLOAD) as called:
            r = self.client.post(
                "/api/kronos/forecast",
                json={"symbols": [" aapl ", "AAPL", "nvda"], "days": 5},
            )
        self.assertEqual(r.status_code, 200)
        called.assert_called_once_with(["AAPL", "NVDA"], 5)

    def test_top_resolves_nasdaq_and_forecasts(self) -> None:
        self._login()
        with patch("web.kronos.top_symbols", return_value=["AAPL", "NVDA"]) as top, \
                patch("web.kronos.forecast", return_value=PAYLOAD) as called:
            r = self.client.post("/api/kronos/forecast", json={"top": 2, "days": 5})
        self.assertEqual(r.status_code, 200)
        top.assert_called_once_with(2)
        called.assert_called_once_with(["AAPL", "NVDA"], 5)

    def test_top_rejects_bad_count(self) -> None:
        self._login()
        from web import kronos

        for top in (0, kronos.MAX_SYMBOLS + 1, "x"):
            r = self.client.post("/api/kronos/forecast", json={"top": top, "days": 5})
            self.assertEqual(r.status_code, 400, top)

    def test_top_resolution_failure_is_502(self) -> None:
        self._login()
        with patch("web.kronos.top_symbols", side_effect=RuntimeError("screener down")):
            r = self.client.post("/api/kronos/forecast", json={"top": 5, "days": 5})
        self.assertEqual(r.status_code, 502)
        self.assertIn("NASDAQ", r.json()["detail"])

    def test_explicit_symbols_ignore_top(self) -> None:
        self._login()
        with patch("web.kronos.top_symbols") as top, \
                patch("web.kronos.forecast", return_value=PAYLOAD) as called:
            r = self.client.post(
                "/api/kronos/forecast",
                json={"symbols": "AAPL", "top": 50, "days": 5},
            )
        self.assertEqual(r.status_code, 200)
        top.assert_not_called()
        called.assert_called_once_with(["AAPL"], 5)

    def test_unavailable_is_503(self) -> None:
        self._login()
        from web import kronos

        with patch("web.kronos.forecast", side_effect=kronos.KronosUnavailable("no venv")):
            r = self.client.post("/api/kronos/forecast", json={"symbols": ["AAPL"], "days": 5})
        self.assertEqual(r.status_code, 503)
        self.assertIn("no venv", r.json()["detail"])

    def test_busy_is_409(self) -> None:
        self._login()
        from web import kronos

        with patch("web.kronos.forecast", side_effect=kronos.KronosBusy()):
            r = self.client.post("/api/kronos/forecast", json={"symbols": ["AAPL"], "days": 5})
        self.assertEqual(r.status_code, 409)

    def test_model_failure_is_502(self) -> None:
        self._login()
        from web import kronos

        with patch("web.kronos.forecast", side_effect=kronos.KronosError("boom")):
            r = self.client.post("/api/kronos/forecast", json={"symbols": ["AAPL"], "days": 5})
        self.assertEqual(r.status_code, 502)


if __name__ == "__main__":
    unittest.main()
