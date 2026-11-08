"""The DeepSeek Harness UI is served same-origin at /deepseek when configured.

Reached by URL: it has no navbar entry, by request.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from web.deepseek_proxy import (
    DeepSeekHarnessProxy,
    PREFIX,
    forwarded_ws_headers,
    is_document,
    proxyable,
    rewrite_location,
    rewrite_set_cookie,
    upstream_path,
)


class _Response:
    def __init__(self, status_code=200, content=b'{"ok": true}', headers=None):
        self.status_code = status_code
        self.content = content
        self.headers = httpx.Headers(headers or {"content-type": "application/json"})


class _Upstream:
    """Stands in for the cookie-less listener client; can answer with a sequence."""

    def __init__(self, response=None, exc=None, responses=None):
        self.response = response or _Response()
        self.exc = exc
        self.responses = list(responses or [])
        self.calls = []

    async def request(self, method, url, content=None, headers=None):
        self.calls.append((method, url, dict(headers or {})))
        if self.exc is not None:
            raise self.exc
        if self.responses:
            return self.responses.pop(0)
        return self.response


class DeepSeekProxyUnitTests(unittest.TestCase):
    def test_only_mounted_paths_are_proxied(self) -> None:
        assert proxyable("/deepseek/")
        assert proxyable("/deepseek/api/remote.mux")
        assert proxyable("/deepseek/index.html")
        # The bare path is our tab page, not the harness.
        assert not proxyable("/deepseek")
        assert not proxyable("/deepseekx")
        assert not proxyable("/deepseekly")
        assert not proxyable("/api/paper/status")
        assert not proxyable("/hermes/api/status")
        assert not proxyable("/")

    def test_prefix_is_stripped_before_forwarding(self) -> None:
        # The harness listener only ever serves origin-root routes.
        assert upstream_path("/deepseek/") == "/"
        assert upstream_path("/deepseek/index.html") == "/index.html"
        assert upstream_path("/deepseek/api/remote.mux", "clientId=x") == \
            "/api/remote.mux?clientId=x"

    def test_redirects_stay_inside_the_mount(self) -> None:
        assert rewrite_location("/login") == "/deepseek/login"
        assert rewrite_location("/") == "/deepseek/"
        # The launch exchange answers a document-relative "./": left alone so it
        # resolves against the mount, not the origin root.
        assert rewrite_location("./") == "./"
        assert rewrite_location("/deepseek/x") == "/deepseek/x"
        assert rewrite_location("https://portal.test/x") == "https://portal.test/x"
        assert rewrite_location("//evil.test/x") == "//evil.test/x"

    def test_cookie_is_scoped_to_the_mount(self) -> None:
        # Loopback HTTP listener: host-only Path=/ and no Secure, rewritten so
        # the cookie cannot ride every request this origin serves.
        rewritten = rewrite_set_cookie(
            "dsh-auth-abc=v1.payload; Max-Age=2592000; Path=/; HttpOnly; "
            "SameSite=Strict",
            secure=False,
        )
        assert "Path=/deepseek/" in rewritten
        assert rewritten.count("Path=") == 1
        assert "HttpOnly" in rewritten and "SameSite=Strict" in rewritten
        assert "Secure" not in rewritten

    def test_cookie_is_secure_on_the_https_leg(self) -> None:
        rewritten = rewrite_set_cookie(
            "dsh-auth-abc=v1.payload; Path=/; HttpOnly; SameSite=Strict",
            secure=True,
        )
        assert "Path=/deepseek/" in rewritten
        assert "Secure" in rewritten
        # Never doubled when the listener already marked it.
        already = rewrite_set_cookie("a=b; Path=/; Secure", secure=True)
        assert already.count("Secure") == 1

    def test_a_launch_token_is_only_for_documents(self) -> None:
        scope = {"type": "http", "method": "GET", "path": "/deepseek/",
                 "headers": [(b"accept", b"text/html,application/xhtml+xml")]}
        assert is_document(httpx.Request("GET", "http://x/", headers=dict(
            (k.decode(), v.decode()) for k, v in scope["headers"])))
        api = httpx.Request("GET", "http://x/api/remote",
                            headers={"accept": "application/json"})
        assert not is_document(api)

    def test_ws_handshake_headers_are_not_duplicated(self) -> None:
        raw = [
            (b"cookie", b"dsh-auth-x=y"),
            (b"origin", b"https://33ai.rootlabs.dev"),
            (b"upgrade", b"websocket"),
            (b"connection", b"Upgrade"),
            (b"host", b"33ai.rootlabs.dev"),
            (b"sec-websocket-key", b"abc=="),
            (b"sec-websocket-version", b"13"),
            (b"sec-websocket-protocol", b"dsh"),
        ]
        headers = dict(forwarded_ws_headers(raw))
        for dropped in ("upgrade", "connection", "host", "sec-websocket-key",
                        "sec-websocket-version", "sec-websocket-protocol"):
            assert dropped not in headers
        # Host is not forwardable (an explicit one is *appended*, and a duplicate
        # Host fails the harness fence with 403) — it travels as the URI
        # authority instead. The cookie and Origin must survive.
        assert headers["cookie"] == "dsh-auth-x=y"
        assert headers["origin"] == "https://33ai.rootlabs.dev"

    def test_ws_target_comes_from_the_listener_url(self) -> None:
        from config import settings

        with patch.object(settings, "deepseek_harness_url", "http://127.0.0.1:3080"):
            assert DeepSeekHarnessProxy._ws_target() == ("127.0.0.1", 3080)
        with patch.object(settings, "deepseek_harness_url", "https://dsh.internal"):
            assert DeepSeekHarnessProxy._ws_target() == ("dsh.internal", 443)


class DeepSeekProxyAppTests(unittest.TestCase):
    def setUp(self) -> None:
        from config import settings

        self.upstream = _Upstream()
        self._tmp = tempfile.TemporaryDirectory()
        self.token_file = Path(self._tmp.name) / "token"
        self.token_file.write_text("launch-token-123", encoding="utf-8")
        self._patches = [
            patch.object(settings, "web_ui_username", "admin"),
            patch.object(settings, "web_ui_password", "correct-horse"),
            patch.object(settings, "web_ui_secret_key", "test-secret-key"),
            patch.object(settings, "deepseek_harness_url", "http://dsh.test"),
            patch.object(settings, "deepseek_harness_token_file", str(self.token_file)),
            patch.object(DeepSeekHarnessProxy, "_upstream",
                         lambda _self: self.upstream),
        ]
        for item in self._patches:
            item.start()

    def tearDown(self) -> None:
        for item in self._patches:
            item.stop()
        self._tmp.cleanup()

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

    def test_harness_calls_are_forwarded_with_the_host_preserved(self) -> None:
        client = self._client()
        resp = client.get("/deepseek/api/remote.mux")
        assert resp.status_code == 200
        method, url, headers = self.upstream.calls[0]
        assert (method, url) == ("GET", "http://dsh.test/api/remote.mux")
        # The harness fences on Host/Origin, so the browser authority survives.
        assert headers["host"] == "testserver"
        assert headers["x-forwarded-prefix"] == PREFIX

    def test_query_string_is_preserved(self) -> None:
        client = self._client()
        client.get("/deepseek/api/remote.mux?clientId=abc&stream=$events")
        assert self.upstream.calls[0][1] == \
            "http://dsh.test/api/remote.mux?clientId=abc&stream=$events"

    def test_response_cookies_and_redirects_are_rewritten(self) -> None:
        self.upstream.response = _Response(
            status_code=303,
            content=b"",
            headers=[
                ("location", "/login"),
                ("set-cookie", "dsh-auth-a=b; Path=/; HttpOnly; SameSite=Strict"),
            ],
        )
        client = self._client()
        resp = client.get("/deepseek/api/status", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/deepseek/login"
        cookie = resp.headers["set-cookie"]
        assert "Path=/deepseek/" in cookie
        assert cookie.count("Path=") == 1

    def test_unsigned_browser_is_signed_in_with_the_launch_token(self) -> None:
        # The harness answers 401 until its cookie exists; the proxy replays the
        # launch token, and the harness (not the proxy) sets the cookie.
        self.upstream.responses = [
            _Response(status_code=401, content=b"", headers={}),
            _Response(
                status_code=303,
                content=b"",
                headers=[
                    ("location", "./"),
                    ("set-cookie", "dsh-auth-a=b; Path=/; HttpOnly; SameSite=Strict"),
                ],
            ),
        ]
        client = self._client()
        resp = client.get("/deepseek/", headers={"Accept": "text/html"},
                          follow_redirects=False)
        assert resp.status_code == 303
        assert "Path=/deepseek/" in resp.headers["set-cookie"]
        assert self.upstream.calls[1][1] == \
            "http://dsh.test/?token=launch-token-123"

    def test_no_launch_token_means_the_401_stands(self) -> None:
        from config import settings

        self.upstream.response = _Response(status_code=401, content=b"", headers={})
        with patch.object(settings, "deepseek_harness_token_file", ""):
            client = self._client()
            resp = client.get("/deepseek/", headers={"Accept": "text/html"},
                              follow_redirects=False)
        assert resp.status_code == 401
        assert len(self.upstream.calls) == 1

    def test_api_401_is_not_replayed_with_the_token(self) -> None:
        # The token is accepted on `GET /` only, so an API call must not be
        # retried against the document route.
        self.upstream.response = _Response(status_code=401, content=b"{}", headers={})
        client = self._client()
        resp = client.get("/deepseek/api/remote.mux",
                          headers={"Accept": "application/json"})
        assert resp.status_code == 401
        assert len(self.upstream.calls) == 1

    def test_anonymous_requests_are_never_forwarded(self) -> None:
        client = self._client(login=False)
        resp = client.get("/deepseek/api/remote.mux")
        assert resp.status_code == 401
        assert self.upstream.calls == []

    def test_anonymous_html_navigation_is_redirected_to_login(self) -> None:
        client = self._client(login=False)
        resp = client.get("/deepseek/", headers={"Accept": "text/html"},
                          follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"].startswith("/login")
        assert self.upstream.calls == []

    def test_upstream_outage_is_503(self) -> None:
        self.upstream.exc = httpx.ConnectError("refused")
        client = self._client()
        resp = client.get("/deepseek/api/remote.mux")
        assert resp.status_code == 503
        assert "unreachable" in resp.json()["detail"].lower()

    def test_inert_when_unconfigured(self) -> None:
        from config import settings

        with patch.object(settings, "deepseek_harness_url", ""):
            client = self._client()
            resp = client.get("/deepseek/api/remote.mux")
        # Falls through to the app, which has no such route.
        assert resp.status_code == 404
        assert self.upstream.calls == []

    def test_page_is_served_by_url_but_never_linked_from_the_navbar(self) -> None:
        # The page is reached by URL on purpose: no navbar entry, configured or
        # not (the operator asked for it to stay out of the way).
        client = self._client()
        page = client.get("/deepseek")
        assert page.status_code == 200
        assert 'id="deepseek-frame"' in page.text
        for name in ("/", "/explorer", "/system", "/settings"):
            assert 'href="/deepseek"' not in client.get(name).text

        from config import settings

        with patch.object(settings, "deepseek_harness_url", ""):
            assert client.get("/deepseek").status_code == 404
            assert 'href="/deepseek"' not in client.get("/").text


if __name__ == "__main__":
    unittest.main()
