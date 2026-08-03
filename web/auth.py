"""Session login for the web UI.

Requires WEB_UI_PASSWORD in .env. Passwords compared with secrets.compare_digest.
Session cookie is HttpOnly + SameSite=Lax (+ Secure when WEB_UI_HTTPS=true).
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from typing import Optional

from fastapi import HTTPException, Request, status
from fastapi.responses import RedirectResponse
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from config import settings

SESSION_COOKIE = "tb_session"
SESSION_MAX_AGE = max(3600, int(settings.web_ui_session_hours) * 3600)


def _secret_key() -> str:
    raw = (settings.web_ui_secret_key or "").strip()
    if raw:
        return raw
    # Deterministic fallback so restarts don't invalidate every session when
    # the operator only set a password. Prefer an explicit WEB_UI_SECRET_KEY.
    material = f"tb-web|{settings.web_ui_username}|{settings.web_ui_password}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def require_password_configured() -> None:
    if not (settings.web_ui_password or "").strip():
        raise RuntimeError(
            "WEB_UI_PASSWORD is empty. Set a strong password in .env before "
            "starting the web UI (refusing to bind an unauthenticated dashboard)."
        )


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(_secret_key(), salt="tb-web-session-v1")


def create_session_token(username: str) -> str:
    return _serializer().dumps({"u": username})


def read_session_token(token: str) -> Optional[str]:
    try:
        data = _serializer().loads(token, max_age=SESSION_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None
    if not isinstance(data, dict):
        return None
    username = data.get("u")
    return username if isinstance(username, str) and username else None


def verify_credentials(username: str, password: str) -> bool:
    expected_user = settings.web_ui_username
    expected_pass = settings.web_ui_password
    user_ok = hmac.compare_digest(username.encode("utf-8"), expected_user.encode("utf-8"))
    pass_ok = hmac.compare_digest(password.encode("utf-8"), expected_pass.encode("utf-8"))
    return user_ok and pass_ok


def set_session_cookie(response: RedirectResponse, username: str) -> None:
    response.set_cookie(
        key=SESSION_COOKIE,
        value=create_session_token(username),
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=bool(settings.web_ui_https),
        path="/",
    )


def clear_session_cookie(response: RedirectResponse) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")


def current_username(request: Request) -> Optional[str]:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    return read_session_token(token)


async def require_login(request: Request) -> str:
    username = current_username(request)
    if username:
        return username
    # HTML navigations → redirect to login; API/XHR → 401 JSON.
    accept = request.headers.get("accept", "")
    is_html = "text/html" in accept and "application/json" not in accept
    if is_html and request.method in ("GET", "HEAD"):
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            headers={"Location": f"/login?next={request.url.path}"},
        )
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Not authenticated",
    )
