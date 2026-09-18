"""Web holder for the shared Pattern Editor facade.

The facade is created lazily so importing ``web.app`` (or running the app before
PostgreSQL is configured) never opens a database connection. There is no file or
in-memory fallback: a database error is surfaced to the caller.
"""
from __future__ import annotations

from fastapi.responses import JSONResponse

from core.pattern_editor_api import PatternEditor
from utils.logger import log


class PatternEdits:
    def __init__(self):
        self._editor: PatternEditor | None = None

    def editor(self) -> PatternEditor:
        if self._editor is None:
            self._editor = PatternEditor()
        return self._editor


pattern_edits = PatternEdits()


def patterns_error(exc: Exception) -> JSONResponse:
    """Map a domain error to a safe status; never leak DSNs or stack traces."""
    from core.backtest_jobs import UnknownJob
    from core.pattern_edit_store import Conflict, EditError
    from core.pattern_editor_db import DatabaseUnavailable, MigrationRequired

    if isinstance(exc, UnknownJob):
        return JSONResponse({"detail": str(exc)}, status_code=404)
    if isinstance(exc, Conflict):
        return JSONResponse({"detail": str(exc)}, status_code=409)
    if isinstance(exc, (DatabaseUnavailable, MigrationRequired)):
        return JSONResponse({"detail": str(exc)}, status_code=503)
    if isinstance(exc, (EditError, ValueError)):
        return JSONResponse({"detail": str(exc)}, status_code=400)
    log.exception("Web patterns | unexpected failure")
    return JSONResponse({"detail": "Pattern editor failed."}, status_code=500)
