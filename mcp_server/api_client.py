"""Authenticated HTTP client for the trading web UI's ``/api/*`` surface.

The MCP server is a thin typed client over the running ``python main.py --web``
process, so there is exactly one owner of paper books and backtest jobs. All
stateful tools go through this client; only read/status endpoints are used for
state, never a second in-process manager.
"""

from __future__ import annotations

from typing import Any

import httpx

from mcp_server import config as mcp_config


class TradingApiError(RuntimeError):
    """A non-2xx (or unreachable) response from the trading web service."""

    def __init__(self, status_code: int, detail: str, method: str, path: str):
        super().__init__(f"{method} {path} -> HTTP {status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail
        self.method = method
        self.path = path


def _detail(resp: httpx.Response) -> str:
    try:
        data = resp.json()
    except Exception:  # noqa: BLE001 - fall back to raw text
        return (resp.text or "").strip()[:1000] or resp.reason_phrase
    if isinstance(data, dict):
        return str(data.get("detail") or data.get("message") or data)[:2000]
    return str(data)[:2000]


class TradingApiClient:
    def __init__(
        self,
        *,
        base_url: str,
        username: str = "",
        password: str = "",
        timeout: float = 60.0,
        transport: httpx.BaseTransport | None = None,
    ):
        auth = (username, password) if username else None
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            auth=auth,
            timeout=timeout,
            transport=transport,
        )

    def request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: Any = None,
    ) -> Any:
        try:
            resp = self._client.request(method, path, json=json, params=params)
        except httpx.HTTPError as exc:
            raise TradingApiError(
                0,
                f"web service unreachable ({exc}). Is 'python main.py --web' running?",
                method,
                path,
            ) from exc
        if resp.status_code >= 400:
            raise TradingApiError(resp.status_code, _detail(resp), method, path)
        if not resp.content:
            return None
        return resp.json()

    def get(self, path: str, *, params: Any = None) -> Any:
        return self.request("GET", path, params=params)

    def post(self, path: str, *, json: Any = None, params: Any = None) -> Any:
        return self.request("POST", path, json=json, params=params)

    def close(self) -> None:
        self._client.close()


_client: TradingApiClient | None = None


def configure(client: TradingApiClient | None) -> TradingApiClient | None:
    """Install a client (used by tests / callers needing a custom transport)."""
    global _client
    _client = client
    return _client


def reset_client() -> None:
    global _client
    if _client is not None:
        _client.close()
    _client = None


def get_client() -> TradingApiClient:
    global _client
    if _client is None:
        user, password = mcp_config.credentials()
        _client = TradingApiClient(
            base_url=mcp_config.api_base_url(),
            username=user,
            password=password,
            timeout=mcp_config.timeout_seconds(),
        )
    return _client
