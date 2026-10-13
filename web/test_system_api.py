"""The System tab reports host CPU/memory/disk and degrades without psutil."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient


class WebSystemApiTests(unittest.TestCase):
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

    def test_snapshot_requires_login(self) -> None:
        r = self.client.get("/api/system", headers={"Accept": "application/json"})
        self.assertEqual(r.status_code, 401)

    def test_snapshot_reports_resources(self) -> None:
        self._login()
        r = self.client.get("/api/system")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertIn("host", data)
        self.assertIn("cpu", data)
        self.assertIn("memory", data)
        self.assertIn("disks", data)
        self.assertIn("processes", data)
        self.assertIsInstance(data["cpu"]["percent"], (int, float))
        self.assertGreater(data["memory"]["total"], 0)
        self.assertTrue(data["disks"])
        self.assertLessEqual(len(data["processes"]["by_cpu"]), 6)
        # No command lines: argv can carry tokens and is never surfaced.
        for row in data["processes"]["by_cpu"]:
            self.assertNotIn("cmdline", row)
            self.assertIn("pid", row)

    def test_top_is_clamped(self) -> None:
        self._login()
        r = self.client.get("/api/system?top=999")
        self.assertEqual(r.status_code, 200)
        self.assertLessEqual(len(r.json()["processes"]["by_cpu"]), 20)

    def test_missing_psutil_is_a_503_not_a_crash(self) -> None:
        self._login()
        with patch("web.system_stats._psutil", side_effect=ImportError("no psutil")):
            r = self.client.get("/api/system")
        self.assertEqual(r.status_code, 503)
        self.assertIn("psutil", r.json()["detail"].lower())

    def test_page_and_nav_link(self) -> None:
        self._login()
        page = self.client.get("/system")
        self.assertEqual(page.status_code, 200)
        self.assertIn('id="sys-cpu"', page.text)
        self.assertIn('href="/system"', self.client.get("/").text)


if __name__ == "__main__":
    unittest.main()
