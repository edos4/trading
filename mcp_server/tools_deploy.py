"""Deploy-gateway tools (optional, token-gated, default-deny).

These talk to the separate deploy gateway app (web.deploy_gateway), not the web
UI. Gated behind MCP_ALLOW_DEPLOY; deploy() also needs MCP_DEPLOY_TOKEN.
"""

from __future__ import annotations

from typing import Any, Optional

import httpx

from mcp_server import config as mcp_config
from mcp_server.api_client import TradingApiError


def _request(
    method: str,
    path: str,
    *,
    json: Any = None,
    token: Optional[str] = None,
) -> Any:
    base = mcp_config.deploy_base_url()
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        resp = httpx.request(
            method,
            f"{base}{path}",
            json=json,
            headers=headers,
            timeout=mcp_config.timeout_seconds(),
        )
    except httpx.HTTPError as exc:
        raise TradingApiError(
            0, f"deploy gateway unreachable ({exc}) — is run_deploy.sh running?", method, path,
        ) from exc
    if resp.status_code >= 400:
        try:
            detail = resp.json().get("detail") or resp.text
        except Exception:  # noqa: BLE001
            detail = resp.text or resp.reason_phrase
        raise TradingApiError(resp.status_code, str(detail)[:2000], method, path)
    return resp.json() if resp.content else None


def deploy_status() -> dict:
    """Report the deploy gateway's repo/branch/commit/dirty state."""
    mcp_config.require_deploy()
    return _request("GET", "/health")


def deploy(ref: str, commit: Optional[str] = None, force: bool = False) -> dict:
    """Deploy a git ref via the gateway (destructive: checks out + restarts units)."""
    mcp_config.require_deploy()
    token = mcp_config.deploy_token()
    if not token:
        raise PermissionError(
            "MCP_DEPLOY_TOKEN is empty; set it to the gateway's DEPLOY_GATEWAY_TOKEN."
        )
    body: dict[str, Any] = {"ref": ref, "force": force}
    if commit is not None:
        body["commit"] = commit
    return _request("POST", "/deploy", json=body, token=token)
