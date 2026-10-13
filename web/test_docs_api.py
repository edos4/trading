"""The Documentation tab renders README.md with a working, clickable TOC."""

from __future__ import annotations

import re
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from web.documentation import README_PATH, _slugify, build_document


class SlugTests(unittest.TestCase):
    def test_slugs_match_github_style(self) -> None:
        assert _slugify("Architecture") == "architecture"
        assert _slugify("Operate via MCP (AI agent)") == "operate-via-mcp-ai-agent"
        assert _slugify("Configuration (`.env`)") == "configuration-env"
        assert _slugify("Volume Confirm Gate") == "volume-confirm-gate"
        assert _slugify("Keep it running across restarts (systemd)") == \
            "keep-it-running-across-restarts-systemd"


class DocumentationRenderTests(unittest.TestCase):
    def test_headings_become_ids_and_toc(self) -> None:
        doc = build_document()
        self.assertGreater(doc["headings"], 10)
        self.assertTrue(doc["toc"])
        # GFM tables and fenced code survive the render.
        self.assertIn("<table>", doc["html"])
        self.assertIn("<pre>", doc["html"])

    def test_every_intra_document_link_has_a_target(self) -> None:
        """The README's own "#anchor" links must resolve to a rendered id."""
        doc = build_document()
        ids = set(re.findall(r'id="([^"]+)"', doc["html"]))
        links = set(re.findall(r"\]\(#([A-Za-z0-9_\-]+)\)",
                              README_PATH.read_text(encoding="utf-8")))
        self.assertTrue(links, "README should carry intra-document links")
        self.assertEqual(sorted(links - ids), [])

    def test_toc_is_nested_by_level(self) -> None:
        doc = build_document()
        self.assertEqual(doc["toc"][0]["level"], 1)
        self.assertTrue(any(node["children"] for node in doc["toc"]))


class DocumentationPageTests(unittest.TestCase):
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
        r = self.client.get("/docs")
        self.assertIn(r.status_code, (303, 401))

    def test_page_renders_toc_and_content(self) -> None:
        self._login()
        page = self.client.get("/docs")
        self.assertEqual(page.status_code, 200)
        self.assertIn('class="docs-toc"', page.text)
        self.assertIn('class="toc-link" href="#setup"', page.text)
        self.assertIn('id="docs-content"', page.text)
        self.assertIn('id="setup"', page.text)
        self.assertIn('href="/docs" class="active"', page.text)

    def test_nav_link_present_on_other_pages(self) -> None:
        self._login()
        self.assertIn('href="/docs"', self.client.get("/").text)

    def test_missing_markdown_support_degrades(self) -> None:
        self._login()
        with patch("web.app.build_document", side_effect=ImportError("no markdown-it")):
            page = self.client.get("/docs")
        self.assertEqual(page.status_code, 200)
        self.assertIn("markdown-it-py", page.text)


if __name__ == "__main__":
    unittest.main()
