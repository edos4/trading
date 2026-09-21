"""Forward pattern-registry HTTP calls to the host that owns the database.

A client with no editor database still serves the Patterns tab and the
backtest version picker. Those endpoints exist unchanged on the owning host, so
the request is forwarded as-is instead of being rebuilt here: paths, query
strings, bodies and status codes all stay identical, and there is no second
copy of the response shapes to keep in step.

Only reads and registry writes are forwarded. Backtest *submission* stays
local, because a run executed here is recorded on the owning host afterwards.
"""
from __future__ import annotations

import threading

import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from utils.logger import log

#: Path prefixes whose handler is the host that owns the pattern registry.
PROXY_PREFIXES = (
    "/api/patterns",
    "/api/backtest/catalog",
    "/api/backtest/presets",
)

_PROXY_TIMEOUT = 60.0


def proxyable(path: str) -> bool:
    """True when this path must be served by the owning host."""
    stripped = path.rstrip("/") or "/"
    return any(stripped == prefix or stripped.startswith(prefix + "/")
               for prefix in PROXY_PREFIXES)


class RemotePatternProxy:
    """ASGI middleware: forward registry calls, or fall through to the app."""

    def __init__(self, app):
        self.app = app
        self._lock = threading.Lock()
        self._client: httpx.AsyncClient | None = None
        self._key: tuple[str, str, str] | None = None

    async def __call__(self, scope, receive, send) -> None:
        if not self._should_proxy(scope):
            return await self.app(scope, receive, send)
        request = Request(scope, receive)
        if not self._authenticated(request):
            # Never forward anonymously: fall through so the local routes
            # produce their own 401 exactly as they do with a local database.
            return await self.app(scope, receive, send)
        response = await self._forward(request)
        await response(scope, receive, send)

    # ── internals ────────────────────────────────────────────────────────
    def _should_proxy(self, scope) -> bool:
        from core.remote_pattern_store import remote_patterns_enabled

        if scope.get("type") != "http" or not proxyable(scope.get("path", "")):
            return False
        return remote_patterns_enabled()

    @staticmethod
    def _authenticated(request: Request) -> bool:
        """The same decision ``require_login`` makes, without the dependency."""
        from web.auth import current_username, username_from_basic_auth

        return bool(current_username(request) or username_from_basic_auth(request))

    def _async_client(self) -> httpx.AsyncClient:
        import data.pattern_client as pattern_client
        from config import settings

        origin = pattern_client.pattern_api_origin()
        user, password = settings.pattern_api_auth
        key = (origin, user, password)
        with self._lock:
            if self._client is not None and self._key == key:
                return self._client
            self._client = httpx.AsyncClient(
                base_url=origin,
                auth=(user, password) if user or password else None,
                timeout=_PROXY_TIMEOUT,
                headers={"Accept": "application/json"},
            )
            self._key = key
            return self._client

    async def _forward(self, request: Request) -> Response:
        path = request.url.path
        body = await request.body()
        headers = {}
        content_type = request.headers.get("content-type")
        if body and content_type:
            headers["content-type"] = content_type
        try:
            client = self._async_client()
            upstream = await client.request(
                request.method, path,
                params=dict(request.query_params), content=body or None,
                headers=headers or None,
            )
        except Exception as exc:
            from data.pattern_client import _is_transport_error

            if _is_transport_error(exc):
                log.warning(f"Pattern API | proxy {path} failed: {type(exc).__name__}: {exc}")
            else:
                log.exception(f"Pattern API | proxy {path} failed")
            return JSONResponse(
                {"detail": "Pattern registry API unreachable; check PATTERN_API_URL and the server"},
                status_code=503,
            )
        return Response(
            content=upstream.content,
            status_code=upstream.status_code,
            media_type=upstream.headers.get("content-type", "application/json"),
        )
