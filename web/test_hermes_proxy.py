"""The Hermes dashboard is proxied same-origin at /hermes when configured."""

from __future__ import annotations

import unittest
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from web.hermes_proxy import (
    HermesDashboardProxy,
    PREFIX,
    forwarded_ws_headers,
    proxyable,
    rewrite_location,
    upstream_path,
)


class _Response:
    def __init__(self, status_code=200, content=b'{"ok": true}', headers=None):
        self.status_code = status_code
        self.content = content
        self.headers = httpx.Headers(headers or {"content-type": "application/json"})


class _Upstream:
    def __init__(self, response=None, exc=None):
        self.response = response or _Response()
        self.exc = exc
        self.calls = []

    async def request(self, method, url, content=None, headers=None):
        self.calls.append((method, url, dict(headers or {})))
        if self.exc is not None:
            raise self.exc
        return self.response


class HermesProxyUnitTests(unittest.TestCase):
    def test_only_mounted_paths_are_proxied(self) -> None:
        assert proxyable("/hermes/")
        assert proxyable("/hermes/assets/index.js")
        assert proxyable("/hermes/api/status")
        # Vite-built chunks/CSS are requested from the origin root, not the
        # prefix, so those must be carried too (the Chat tab's lazy chunk).
        assert proxyable("/assets/ChatPage-abc.js")
        assert proxyable("/assets/xterm-def.css")
        # The bare path is the UI tab page, not the dashboard.
        assert not proxyable("/hermes")
        assert not proxyable("/hermesx")
        assert not proxyable("/api/paper/status")
        assert not proxyable("/static/app.css")
        assert not proxyable("/")

    def test_prefix_is_stripped_before_forwarding(self) -> None:
        assert upstream_path("/hermes/") == "/"
        assert upstream_path("/hermes/assets/app.js") == "/assets/app.js"
        assert upstream_path("/hermes/api/status", "profile=worker") == \
            "/api/status?profile=worker"
        # Root asset paths are already in upstream coordinates.
        assert upstream_path("/assets/app.js") == "/assets/app.js"

    def test_redirects_stay_inside_the_mount(self) -> None:
        assert rewrite_location("/login") == "/hermes/login"
        assert rewrite_location("/") == "/hermes/"
        # Already mounted, absolute, or protocol-relative: left alone.
        assert rewrite_location("/hermes/login") == "/hermes/login"
        assert rewrite_location("https://portal.test/x") == "https://portal.test/x"
        assert rewrite_location("//evil.test/x") == "//evil.test/x"

    def test_ws_handshake_headers_are_not_duplicated(self) -> None:
        raw = [
            (b"cookie", b"tb_session=x"),
            (b"origin", b"http://localhost:8080"),
            (b"upgrade", b"websocket"),
            (b"connection", b"Upgrade"),
            (b"host", b"localhost:8080"),
            (b"sec-websocket-key", b"abc=="),
            (b"sec-websocket-version", b"13"),
            (b"sec-websocket-protocol", b"hermes"),
        ]
        headers = dict(forwarded_ws_headers(raw))
        # The client library regenerates these; forwarding them is an HTTP 400.
        for dropped in ("upgrade", "connection", "host", "sec-websocket-key",
                        "sec-websocket-version", "sec-websocket-protocol"):
            assert dropped not in headers
        # The upstream still needs the session and the mount prefix.
        assert headers["cookie"] == "tb_session=x"
        assert headers["origin"] == "http://localhost:8080"
        assert headers["x-forwarded-prefix"] == PREFIX


class HermesProxyAppTests(unittest.TestCase):
    def setUp(self) -> None:
        from config import settings

        self.upstream = _Upstream()
        self._patches = [
            patch.object(settings, "web_ui_username", "admin"),
            patch.object(settings, "web_ui_password", "correct-horse"),
            patch.object(settings, "web_ui_secret_key", "test-secret-key"),
            patch.object(settings, "hermes_dashboard_url", "http://hermes.test"),
            patch.object(HermesDashboardProxy, "_async_client",
                         lambda _self: self.upstream),
        ]
        for item in self._patches:
            item.start()

    def tearDown(self) -> None:
        for item in self._patches:
            item.stop()

    def _client(self, *, login: bool = True) -> TestClient:
        from web.app import create_app

        client = TestClient(create_app(), raise_server_exceptions=False)
        if login:
            resp = client.post("/login", data={
                "username": "admin", "password": "correct-horse", "next": "/"},
                follow_redirects=False)
            assert resp.status_code == 303
            client.cookies.update(resp.cookies)
        return client

    def test_dashboard_calls_are_forwarded_with_the_prefix(self) -> None:
        client = self._client()
        resp = client.get("/hermes/api/status")
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}
        method, url, headers = self.upstream.calls[0]
        assert (method, url) == ("GET", "/api/status")
        assert headers["x-forwarded-prefix"] == PREFIX

    def test_query_string_is_preserved(self) -> None:
        client = self._client()
        client.get("/hermes/api/tools?profile=worker&x=1")
        assert self.upstream.calls[0][1] == "/api/tools?profile=worker&x=1"

    def test_response_headers_and_redirects_pass_through(self) -> None:
        self.upstream.response = _Response(
            status_code=303,
            content=b"",
            headers=[
                ("location", "/login"),
                ("set-cookie", "hermes_session_at=a; Path=/"),
                ("set-cookie", "hermes_session_rt=b; Path=/"),
            ],
        )
        client = self._client()
        resp = client.get("/hermes/api/status", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/hermes/login"
        assert len(resp.headers.get_list("set-cookie")) == 2

    def test_anonymous_requests_are_never_forwarded(self) -> None:
        client = self._client(login=False)
        resp = client.get("/hermes/api/status")
        assert resp.status_code == 401
        assert self.upstream.calls == []

    def test_anonymous_html_navigation_is_redirected_to_login(self) -> None:
        client = self._client(login=False)
        resp = client.get("/hermes/", headers={"Accept": "text/html"},
                          follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"].startswith("/login")
        assert self.upstream.calls == []

    def test_upstream_outage_is_503(self) -> None:
        self.upstream.exc = httpx.ConnectError("refused")
        client = self._client()
        resp = client.get("/hermes/api/status")
        assert resp.status_code == 503
        assert "unreachable" in resp.json()["detail"].lower()

    def test_inert_when_unconfigured(self) -> None:
        from config import settings

        with patch.object(settings, "hermes_dashboard_url", ""):
            client = self._client()
            resp = client.get("/hermes/api/status")
        # Falls through to the app, which has no such route.
        assert resp.status_code == 404
        assert self.upstream.calls == []

    def test_tab_page_and_nav_link_follow_the_setting(self) -> None:
        client = self._client()
        page = client.get("/hermes")
        assert page.status_code == 200
        assert 'id="hermes-frame"' in page.text
        assert 'href="/hermes"' in client.get("/").text

        from config import settings

        with patch.object(settings, "hermes_dashboard_url", ""):
            assert client.get("/hermes").status_code == 404
            assert 'href="/hermes"' not in client.get("/").text


if __name__ == "__main__":
    unittest.main()
