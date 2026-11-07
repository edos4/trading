"""Settings tab: grouped catalog, safe secret handling and .env persistence."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from config import Settings
from web import settings_store


class WebSettingsApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.env_path = Path(self.tmp.name) / ".env"
        self.env_path.write_text(
            "# keep this comment\n"
            "WATCHLIST=AAPL,MSFT\n"
            "TV_SCREENER=america\n"
            "TV_EXCHANGE=NASDAQ\n"
            "SCAN_INTERVAL_SECONDS=3600\n"
            "ALPACA_API_KEY=super-secret-key\n"
            "WEB_UI_USERNAME=admin\n"
            "WEB_UI_PASSWORD=correct-horse\n"
            "WEB_UI_SECRET_KEY=test-secret-key\n",
            encoding="utf-8",
        )
        self.settings = Settings(
            _env_file=None,
            watchlist="AAPL,MSFT",
            tv_screener="america",
            tv_exchange="NASDAQ",
            scan_interval_seconds=3600,
            alpaca_api_key="super-secret-key",
            web_ui_username="admin",
            web_ui_password="correct-horse",
            web_ui_secret_key="test-secret-key",
        )
        self.patches = [
            patch("web.auth.settings", self.settings),
            patch("web.app.settings", self.settings),
            patch("web.settings_store.settings", self.settings),
            patch("web.settings_store.ENV_PATH", self.env_path),
        ]
        for item in self.patches:
            item.start()
        from web.app import create_app

        self.client = TestClient(create_app())

    def tearDown(self) -> None:
        for item in self.patches:
            item.stop()
        self.tmp.cleanup()

    def _login(self) -> None:
        ok = self.client.post(
            "/login",
            data={"username": "admin", "password": "correct-horse", "next": "/"},
            follow_redirects=False,
        )
        self.assertEqual(ok.status_code, 303)
        self.client.cookies.update(ok.cookies)

    def _env_text(self) -> str:
        return self.env_path.read_text(encoding="utf-8")

    # ── Catalog ──────────────────────────────────────────────────────────

    def test_every_setting_is_grouped_and_documented(self) -> None:
        grouped: list[str] = []
        for _gid, _title, _blurb, names in settings_store.GROUPS:
            grouped.extend(names)
        self.assertEqual(len(grouped), len(set(grouped)), "a field appears in two groups")
        self.assertEqual(set(grouped), set(Settings.model_fields))
        self.assertEqual(set(settings_store.INFO), set(Settings.model_fields))

    def test_login_required(self) -> None:
        page = self.client.get(
            "/settings", headers={"Accept": "text/html"}, follow_redirects=False
        )
        self.assertEqual(page.status_code, 303)
        api = self.client.get("/api/settings", headers={"Accept": "application/json"})
        self.assertEqual(api.status_code, 401)
        save = self.client.post("/api/settings", json={"values": {}})
        self.assertEqual(save.status_code, 401)

    def test_page_and_nav_link(self) -> None:
        self._login()
        page = self.client.get("/settings")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Trading &amp; risk", page.text)
        self.assertIn("scan_interval_seconds", page.text)
        self.assertIn("Seconds between scan-loop passes", page.text)
        self.assertIn('id="settings-form"', page.text)
        self.assertIn('href="/settings"', self.client.get("/").text)

    def test_api_returns_groups_without_secret_values(self) -> None:
        self._login()
        response = self.client.get("/api/settings")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertNotIn("super-secret-key", response.text)

        fields = {
            field["name"]: field
            for group in data["groups"]
            for field in group["fields"]
        }
        self.assertEqual(set(fields), set(Settings.model_fields))
        self.assertEqual(fields["alpaca_api_key"]["kind"], "secret")
        self.assertTrue(fields["alpaca_api_key"]["configured"])
        self.assertEqual(fields["alpaca_api_key"]["value"], "")
        self.assertEqual(fields["scan_interval_seconds"]["value"], 3600)
        self.assertEqual(fields["trading_mode"]["options"], ["paper", "live"])

    # ── Save ─────────────────────────────────────────────────────────────

    def test_saves_and_applies_non_secret_value(self) -> None:
        self._login()
        response = self.client.post(
            "/api/settings", json={"values": {"scan_interval_seconds": 900}}
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.settings.scan_interval_seconds, 900)
        self.assertEqual(response.json()["values"]["scan_interval_seconds"], 900)
        text = self._env_text()
        self.assertIn("SCAN_INTERVAL_SECONDS=900", text)
        self.assertIn("# keep this comment", text)
        self.assertIn("WATCHLIST=AAPL,MSFT", text)

    def test_rejects_unknown_setting(self) -> None:
        self._login()
        response = self.client.post(
            "/api/settings", json={"values": {"not_a_setting": 1}}
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("Unknown setting", response.json()["detail"])

    def test_rejects_invalid_value(self) -> None:
        self._login()
        response = self.client.post(
            "/api/settings", json={"values": {"scan_interval_seconds": "soon"}}
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("scan_interval_seconds", response.json()["detail"])

    def test_blank_secret_keeps_current_value(self) -> None:
        self._login()
        response = self.client.post(
            "/api/settings", json={"values": {"scan_interval_seconds": 1200}}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.settings.alpaca_api_key, "super-secret-key")
        self.assertIn("ALPACA_API_KEY=super-secret-key", self._env_text())

    def test_explicit_empty_secret_clears_it(self) -> None:
        self._login()
        response = self.client.post(
            "/api/settings", json={"values": {"alpaca_api_key": ""}}
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.settings.alpaca_api_key, "")
        self.assertFalse(response.json()["configured"]["alpaca_api_key"])
        self.assertIn('ALPACA_API_KEY=""\n', self._env_text())

    def test_restart_only_credential_is_saved_not_applied(self) -> None:
        self._login()
        response = self.client.post(
            "/api/settings", json={"values": {"web_ui_password": "new-horse"}}
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("web_ui_password", response.json()["restart_required"])
        self.assertEqual(self.settings.web_ui_password, "correct-horse")
        self.assertIn("WEB_UI_PASSWORD=new-horse", self._env_text())

    def test_empty_web_password_is_refused(self) -> None:
        self._login()
        response = self.client.post(
            "/api/settings", json={"values": {"web_ui_password": ""}}
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("cannot be empty", response.json()["detail"])

    def test_env_only_variables_are_editable(self) -> None:
        with self.env_path.open("a", encoding="utf-8") as handle:
            handle.write("CLOUDFLARE_TUNNEL_TOKEN=tunnel-secret\n")
            handle.write("KRONOS_SAMPLE_COUNT=5\n")
        self._login()
        data = self.client.get("/api/settings").json()
        extras = {
            field["name"]: field
            for group in data["groups"]
            if group["id"] == "extras"
            for field in group["fields"]
        }
        self.assertEqual(extras["kronos_sample_count"]["value"], 5)
        self.assertEqual(extras["cloudflare_tunnel_token"]["kind"], "secret")
        self.assertTrue(extras["cloudflare_tunnel_token"]["configured"])
        self.assertNotIn("tunnel-secret", self.client.get("/api/settings").text)

        response = self.client.post(
            "/api/settings",
            json={"values": {"kronos_sample_count": 7}},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("KRONOS_SAMPLE_COUNT=7", self._env_text())
        self.assertIn("kronos_sample_count", response.json()["restart_required"])

    def test_quotes_values_that_dotenv_could_split(self) -> None:
        self._login()
        response = self.client.post(
            "/api/settings",
            json={"values": {"tv_exchange_overrides": "SPY:AMEX, BRK.B:NYSE#x"}},
        )
        self.assertEqual(response.status_code, 200, response.text)
        line = next(
            row for row in self._env_text().splitlines()
            if row.startswith("TV_EXCHANGE_OVERRIDES=")
        )
        self.assertEqual(
            line,
            'TV_EXCHANGE_OVERRIDES="SPY:AMEX, BRK.B:NYSE#x"',
        )
        self.assertEqual(
            json.loads('"' + line.split("=", 1)[1][1:-1] + '"'),
            "SPY:AMEX, BRK.B:NYSE#x",
        )


if __name__ == "__main__":
    unittest.main()
