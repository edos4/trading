"""MCP server settings helpers.

Thin accessors over ``config.settings`` so the tool modules do not read
``.env`` directly. Destructive/maintenance/deploy groups are default-deny and
each raises a clear ``PermissionError`` until its ``MCP_ALLOW_*`` flag is set.
"""

from __future__ import annotations

from config import settings

DEFAULT_API_URL = "http://127.0.0.1:8080"
DEFAULT_DEPLOY_URL = "http://127.0.0.1:8099"


def api_base_url() -> str:
    return (settings.mcp_api_url or DEFAULT_API_URL).rstrip("/")


def credentials() -> tuple[str, str]:
    """Basic-auth for the web UI /api/* — falls back to WEB_UI_*."""
    user = (settings.mcp_username or settings.web_ui_username or "").strip()
    password = settings.mcp_password or settings.web_ui_password or ""
    return user, password


def history_credentials() -> tuple[str, str]:
    """Basic-auth for ``/api/history/*`` — ``WEB_UI_USERNAME`` as user *and* password.

    That surface verifies ``username == password == WEB_UI_USERNAME`` (see
    ``web.auth.verify_history_credentials``), unlike the rest of ``/api/*`` which
    checks the password. One credential therefore cannot serve both, so the
    history calls carry their own.
    """
    user = (settings.web_ui_username or "").strip()
    return user, user


def timeout_seconds() -> float:
    return float(settings.mcp_timeout_seconds or 60.0)


def max_history_bars() -> int:
    return max(1, int(settings.mcp_max_history_bars or 2000))


def deploy_base_url() -> str:
    return (settings.mcp_deploy_url or DEFAULT_DEPLOY_URL).rstrip("/")


def deploy_token() -> str:
    return (settings.mcp_deploy_token or "").strip()


def require_destructive() -> None:
    if not settings.mcp_allow_destructive:
        raise PermissionError(
            "Destructive MCP tools are disabled. Set MCP_ALLOW_DESTRUCTIVE=true "
            "in .env to enable paper reset/reset-logs, pattern default/archive, "
            "and replay clear/upload."
        )


def require_maintenance() -> None:
    if not settings.mcp_allow_maintenance:
        raise PermissionError(
            "Maintenance MCP tools are disabled. Set MCP_ALLOW_MAINTENANCE=true "
            "in .env to enable db_check/db_update."
        )


def require_deploy() -> None:
    if not settings.mcp_allow_deploy:
        raise PermissionError(
            "Deploy MCP tools are disabled. Set MCP_ALLOW_DEPLOY=true in .env "
            "to enable deploy_status/deploy."
        )
