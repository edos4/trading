"""Tasks (Kanban) board routes.

Self-contained route module registered by ``web/app.py`` next to the Laya
routes. Every route requires login. The board is PostgreSQL-backed; when the
database is unavailable or unmigrated the page renders a readable error naming
``scripts/setup_tasks_db.py`` and every other tab keeps serving.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse

from web.auth import require_login

log = logging.getLogger("web.tasks")

_ERROR_TITLES = {
    "invalid-request": 422,
    "schema-error": 422,
    "not-found": 404,
    "conflict": 409,
    "migration-required": 503,
    "unavailable": 503,
    "server-error": 500,
}


def _err(code: str, detail: str) -> JSONResponse:
    return JSONResponse({"error_code": code, "detail": detail},
                        status_code=_ERROR_TITLES.get(code, 400))


def _store():
    from core.task_store import open_task_store

    return open_task_store()


async def _json_body(request: Request) -> dict[str, Any]:
    try:
        data = await request.json()
    except Exception as exc:
        raise ValueError(f"Invalid JSON body: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError("JSON body must be an object")
    return data


def _failure(exc: Exception) -> JSONResponse:
    from core.task_db import (TaskConflict, TaskDatabaseUnavailable, TaskError,
                              TaskMigrationRequired, TaskNotFound)

    if isinstance(exc, TaskMigrationRequired):
        return _err("migration-required",
                    "Tasks storage is not migrated; run scripts/setup_tasks_db.py")
    if isinstance(exc, TaskDatabaseUnavailable):
        return _err("unavailable",
                    "Tasks storage is unavailable; check the PostgreSQL connection")
    if isinstance(exc, TaskNotFound):
        return _err("not-found", str(exc))
    if isinstance(exc, TaskConflict):
        return _err("conflict", str(exc))
    if isinstance(exc, TaskError):
        return _err("invalid-request", str(exc))
    log.exception("Tasks | unexpected error")
    return _err("server-error", "Unexpected Tasks error")


async def _run(op):
    """Run a blocking store operation; return ``(result, error_response)``."""
    try:
        return await asyncio.to_thread(op), None
    except Exception as exc:  # noqa: BLE001 - mapped to a stable error response
        return None, _failure(exc)


def register_task_routes(app, templates, ctx) -> None:
    @app.get("/tasks", response_class=HTMLResponse)
    async def tasks_page(request: Request, _user: str = Depends(require_login)):
        error = ""
        try:
            await asyncio.to_thread(_store().ping)
        except Exception as exc:  # noqa: BLE001 - the page degrades, never blocks
            from core.task_db import TaskError

            error = str(exc) if isinstance(exc, TaskError) else ""
            error = error or "Tasks storage is unavailable; check the PostgreSQL connection."
        return templates.TemplateResponse(
            request, "tasks.html",
            ctx(request, active="tasks", tasks_error=error),
        )

    @app.get("/api/tasks/board")
    async def api_board(request: Request, _user: str = Depends(require_login)):
        include = (request.query_params.get("include_archived") or "").lower() in (
            "1", "true", "yes", "on")
        result, error = await _run(lambda: _store().board(include_archived=include))
        return error or result

    @app.get("/api/tasks/export")
    async def api_export(_user: str = Depends(require_login)):
        result, error = await _run(lambda: _store().export())
        return error or result

    # ── lists ──────────────────────────────────────────────────────────────
    @app.post("/api/tasks/lists", status_code=201)
    async def api_create_list(request: Request, _user: str = Depends(require_login)):
        try:
            body = await _json_body(request)
        except ValueError as exc:
            return _err("invalid-request", str(exc))
        result, error = await _run(lambda: _store().create_list(body.get("title")))
        return error or result

    @app.patch("/api/tasks/lists/{list_id}")
    async def api_update_list(list_id: str, request: Request,
                              _user: str = Depends(require_login)):
        try:
            body = await _json_body(request)
        except ValueError as exc:
            return _err("invalid-request", str(exc))
        kwargs: dict[str, Any] = {}
        if "title" in body:
            kwargs["title"] = body.get("title")
        if "archived" in body:
            kwargs["archived"] = body.get("archived")
        result, error = await _run(lambda: _store().update_list(list_id, **kwargs))
        return error or result

    @app.post("/api/tasks/lists/reorder")
    async def api_reorder_list(request: Request, _user: str = Depends(require_login)):
        try:
            body = await _json_body(request)
        except ValueError as exc:
            return _err("invalid-request", str(exc))
        list_id = (body.get("list_id") or "").strip()
        if not list_id:
            return _err("invalid-request", "list_id is required")
        result, error = await _run(lambda: _store().reorder_list(
            list_id, before_id=body.get("before_id"), after_id=body.get("after_id")))
        return error or result

    @app.delete("/api/tasks/lists/{list_id}")
    async def api_delete_list(list_id: str, _user: str = Depends(require_login)):
        _result, error = await _run(lambda: _store().delete_list(list_id))
        return error or {"ok": True}

    # ── cards ──────────────────────────────────────────────────────────────
    @app.post("/api/tasks/cards", status_code=201)
    async def api_create_card(request: Request, _user: str = Depends(require_login)):
        try:
            body = await _json_body(request)
        except ValueError as exc:
            return _err("invalid-request", str(exc))
        list_id = (body.get("list_id") or "").strip()
        if not list_id:
            return _err("invalid-request", "list_id is required")
        result, error = await _run(lambda: _store().create_card(
            list_id, body.get("title"), body.get("description", ""), _user))
        return error or result

    @app.patch("/api/tasks/cards/{card_id}")
    async def api_update_card(card_id: str, request: Request,
                              _user: str = Depends(require_login)):
        try:
            body = await _json_body(request)
        except ValueError as exc:
            return _err("invalid-request", str(exc))
        kwargs: dict[str, Any] = {}
        for key in ("title", "description", "archived"):
            if key in body:
                kwargs[key] = body.get(key)
        result, error = await _run(lambda: _store().update_card(card_id, **kwargs))
        return error or result

    @app.post("/api/tasks/cards/{card_id}/move")
    async def api_move_card(card_id: str, request: Request,
                            _user: str = Depends(require_login)):
        try:
            body = await _json_body(request)
        except ValueError as exc:
            return _err("invalid-request", str(exc))
        list_id = (body.get("list_id") or "").strip()
        if not list_id:
            return _err("invalid-request", "list_id is required")
        result, error = await _run(lambda: _store().move_card(
            card_id, list_id, before_id=body.get("before_id"), after_id=body.get("after_id")))
        return error or result

    @app.delete("/api/tasks/cards/{card_id}")
    async def api_delete_card(card_id: str, _user: str = Depends(require_login)):
        _result, error = await _run(lambda: _store().delete_card(card_id))
        return error or {"ok": True}

    # ── subtasks ───────────────────────────────────────────────────────────
    @app.post("/api/tasks/cards/{card_id}/subtasks", status_code=201)
    async def api_add_subtask(card_id: str, request: Request,
                              _user: str = Depends(require_login)):
        try:
            body = await _json_body(request)
        except ValueError as exc:
            return _err("invalid-request", str(exc))
        result, error = await _run(lambda: _store().add_subtask(card_id, body.get("text")))
        return error or result

    @app.patch("/api/tasks/subtasks/{subtask_id}")
    async def api_update_subtask(subtask_id: str, request: Request,
                                 _user: str = Depends(require_login)):
        try:
            body = await _json_body(request)
        except ValueError as exc:
            return _err("invalid-request", str(exc))
        kwargs: dict[str, Any] = {}
        if "text" in body:
            kwargs["text"] = body.get("text")
        if "done" in body:
            kwargs["done"] = body.get("done")
        result, error = await _run(lambda: _store().update_subtask(subtask_id, **kwargs))
        return error or result

    @app.delete("/api/tasks/subtasks/{subtask_id}")
    async def api_delete_subtask(subtask_id: str, _user: str = Depends(require_login)):
        _result, error = await _run(lambda: _store().delete_subtask(subtask_id))
        return error or {"ok": True}

    # ── comments ───────────────────────────────────────────────────────────
    @app.get("/api/tasks/cards/{card_id}/comments")
    async def api_list_comments(card_id: str, _user: str = Depends(require_login)):
        result, error = await _run(lambda: _store().list_comments(card_id))
        return error or {"comments": result}

    @app.post("/api/tasks/cards/{card_id}/comments", status_code=201)
    async def api_add_comment(card_id: str, request: Request,
                              _user: str = Depends(require_login)):
        try:
            body = await _json_body(request)
        except ValueError as exc:
            return _err("invalid-request", str(exc))
        parent_id = body.get("parent_id") or None
        result, error = await _run(lambda: _store().add_comment(
            card_id, _user, body.get("body"), parent_id=parent_id))
        return error or result

    @app.patch("/api/tasks/comments/{comment_id}")
    async def api_update_comment(comment_id: str, request: Request,
                                 _user: str = Depends(require_login)):
        try:
            body = await _json_body(request)
        except ValueError as exc:
            return _err("invalid-request", str(exc))
        result, error = await _run(lambda: _store().update_comment(comment_id, body.get("body")))
        return error or result

    @app.delete("/api/tasks/comments/{comment_id}")
    async def api_delete_comment(comment_id: str, _user: str = Depends(require_login)):
        _result, error = await _run(lambda: _store().delete_comment(comment_id))
        return error or {"ok": True}
