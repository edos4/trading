"""Registry paths are served by the owning host when there is no local database."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from web.remote_proxy import RemotePatternProxy, proxyable


class _Response:
    def __init__(self, status_code=200, content=b'{"patterns": []}'):
        self.status_code = status_code
        self.content = content
        self.headers = {"content-type": "application/json"}


class _Upstream:
    def __init__(self, response=None, exc=None):
        self.response = response or _Response()
        self.exc = exc
        self.calls = []

    async def request(self, method, path, params=None, content=None, headers=None):
        self.calls.append((method, path, params, content))
        if self.exc is not None:
            raise self.exc
        return self.response


class RemoteProxyTests(unittest.TestCase):
    def setUp(self) -> None:
        from config import settings

        self.upstream = _Upstream()
        self._patches = [
            patch.object(settings, "pattern_api_url", "https://registry.test"),
            patch.object(settings, "pattern_api_owner", False),
            patch.object(RemotePatternProxy, "_async_client",
                         lambda _self: self.upstream),
            patch("web.auth.settings"),
            patch("web.app.settings"),
        ]
        mocks = [p.start() for p in self._patches]
        for mock in mocks[3:]:
            mock.web_ui_password = "correct-horse"
            mock.web_ui_username = "admin"
            mock.web_ui_secret_key = "test-secret-key"
            mock.web_ui_https = False
            mock.web_ui_session_hours = 12

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

    def test_proxyable_paths_are_the_registry_surface(self) -> None:
        assert proxyable("/api/patterns")
        assert proxyable("/api/patterns/versions/x/source")
        assert proxyable("/api/backtest/catalog")
        assert proxyable("/api/backtest/catalog/p1/versions")
        assert proxyable("/api/backtest/presets")
        # A run executed here is submitted and tracked locally.
        assert not proxyable("/api/backtest/runs")
        assert not proxyable("/api/backtest/runs/abc")
        assert not proxyable("/api/paper/status")

    def test_registry_calls_are_forwarded(self) -> None:
        client = self._client()
        resp = client.get("/api/patterns")
        assert resp.status_code == 200
        assert resp.json() == {"patterns": []}
        assert self.upstream.calls[0][:2] == ("GET", "/api/patterns")

    def test_query_string_and_method_are_preserved(self) -> None:
        client = self._client()
        client.get("/api/backtest/catalog/p1/versions?include_archived=true")
        method, path, params, _ = self.upstream.calls[0]
        assert (method, path) == ("GET", "/api/backtest/catalog/p1/versions")
        assert params == {"include_archived": "true"}

    def test_anonymous_requests_are_never_forwarded(self) -> None:
        client = self._client(login=False)
        resp = client.get("/api/patterns")
        assert resp.status_code == 401
        assert self.upstream.calls == []

    def test_upstream_outage_is_503(self) -> None:
        import httpx

        self.upstream.exc = httpx.ConnectError("refused")
        client = self._client()
        resp = client.get("/api/patterns")
        assert resp.status_code == 503
        assert "unreachable" in resp.json()["detail"].lower()

    def test_local_route_serves_the_path_when_no_url_is_configured(self) -> None:
        from config import settings

        with patch.object(settings, "pattern_api_url", ""):
            client = self._client()
            client.get("/api/patterns")
        assert self.upstream.calls == []


if __name__ == "__main__":
    unittest.main()
