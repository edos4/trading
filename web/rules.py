"""Builder tab: visual indicator rules (CRUD, preview, replay)."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse

from web.auth import require_login

log = logging.getLogger("web.rules")

_ERROR_STATUS = {
    "invalid-request": 400,
    "conflict": 409,
    "migration-required": 503,
    "unavailable": 503,
    "server-error": 500,
}

OPERATORS = [
    {"id": "gt", "label": ">"},
    {"id": "gte", "label": ">="},
    {"id": "lt", "label": "<"},
    {"id": "lte", "label": "<="},
    {"id": "eq", "label": "="},
    {"id": "ne", "label": "!="},
    {"id": "crosses_above", "label": "crosses above"},
    {"id": "crosses_below", "label": "crosses below"},
]


def _err(code: str, detail: str) -> JSONResponse:
    return JSONResponse({"error_code": code, "detail": detail},
                        status_code=_ERROR_STATUS.get(code, 400))


def _failure(exc: Exception) -> JSONResponse:
    from core.rules_db import (
        RuleConflict, RulesDatabaseUnavailable, RulesError, RulesMigrationRequired,
    )

    if isinstance(exc, RulesMigrationRequired):
        return _err("migration-required",
                    "Rules storage is not migrated; run scripts/setup_rules_db.py")
    if isinstance(exc, RulesDatabaseUnavailable):
        return _err("unavailable",
                    "Rules storage is unavailable; check the PostgreSQL connection")
    if isinstance(exc, RuleConflict):
        return _err("conflict", str(exc))
    if isinstance(exc, (RulesError, ValueError)):
        return _err("invalid-request", str(exc))
    log.exception("Rules | unexpected error")
    return _err("server-error", "Unexpected rules error")


async def _json_body(request: Request) -> dict[str, Any]:
    try:
        data = await request.json()
    except Exception as exc:
        raise ValueError(f"Invalid JSON body: {exc}") from None
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError("JSON body must be an object")
    return data


async def _run(op):
    try:
        return await asyncio.to_thread(op), None
    except Exception as exc:  # noqa: BLE001 - mapped to a stable response
        return None, _failure(exc)


def _store():
    from core.rules_store import open_rules_store

    return open_rules_store()


def _rule_from_body(body: dict):
    from analysis.rule_catalog import validate_rule
    from core.rule_contracts import RulePayload

    nested = body.get("payload")
    payload = nested if isinstance(nested, dict) else body
    data = dict(payload)
    data["name"] = (body.get("name") or data.get("name") or "unsaved").strip()
    data.setdefault("schema_version", 1)
    rule = RulePayload.model_validate(data)
    validate_rule(rule)
    return rule


def _load_rule(rule_id: str):
    row = _store().get_rule(rule_id)
    if row is None:
        raise ValueError("Rule not found")
    return _rule_from_body({"name": row["name"], "payload": row["payload"]})


def register_rules_routes(app, templates, ctx) -> None:
    @app.get("/builder", response_class=HTMLResponse)
    async def builder_page(request: Request, _user: str = Depends(require_login)):
        error = ""
        try:
            await asyncio.to_thread(_store().ping)
        except Exception as exc:  # noqa: BLE001 - the page degrades, never blocks
            from core.rules_db import RulesError

            error = str(exc) if isinstance(exc, RulesError) else ""
            error = error or "Rules storage is unavailable; check the PostgreSQL connection."
        return templates.TemplateResponse(
            request, "builder.html",
            ctx(request, active="builder", rules_error=error),
        )

    @app.get("/api/rules/catalog")
    async def api_rules_catalog(_user: str = Depends(require_login)):
        from analysis.rule_catalog import GROUPS, catalog_payload
        from patterns_v2.patterns.common import PATTERN_NAMES

        return {
            "indicators": catalog_payload(),
            "operators": OPERATORS,
            "groups": list(GROUPS),
            "patternsv2_patterns": [
                {"id": name, "label": name.replace("_", " ").title()}
                for name in sorted(PATTERN_NAMES)
            ],
        }

    @app.get("/api/rules")
    async def api_rules_list(_user: str = Depends(require_login)):
        result, error = await _run(lambda: _store().list_rules())
        return error or {"rules": result}

    @app.post("/api/rules/validate")
    async def api_rules_validate(request: Request, _user: str = Depends(require_login)):
        try:
            body = await _json_body(request)
            rule = _rule_from_body(body)
        except Exception as exc:  # noqa: BLE001
            errors = getattr(exc, "errors", None)
            if callable(errors):
                return JSONResponse({
                    "ok": False,
                    "detail": "Validation failed",
                    "errors": [
                        {"loc": list(item.get("loc", ())), "msg": item.get("msg", "")}
                        for item in errors()
                    ],
                }, status_code=400)
            return JSONResponse({
                "ok": False,
                "detail": str(exc),
                "errors": [{"loc": [], "msg": str(exc)}],
            }, status_code=400)
        return {"ok": True, "rule": rule.model_dump(mode="json")}

    @app.post("/api/rules/preview")
    async def api_rules_preview(request: Request, _user: str = Depends(require_login)):
        from core.rule_backtest import preview_rule

        try:
            body = await _json_body(request)
            rule_id = (body.get("rule_id") or "").strip()
            if rule_id:
                rule = await asyncio.to_thread(_load_rule, rule_id)
            else:
                rule = _rule_from_body(body)
            symbol = (body.get("symbol") or "").strip().upper()
            if not symbol:
                raise ValueError("A symbol is required")
            market = (body.get("market") or "us").strip().lower()
            bars = int(body.get("bars") or 120)
            result = await asyncio.to_thread(
                preview_rule, rule, symbol, market=market, bars=bars)
            return result
        except Exception as exc:  # noqa: BLE001
            return _failure(exc)

    @app.post("/api/rules/{rule_id}/backtest")
    async def api_rules_backtest(rule_id: str, request: Request,
                                 _user: str = Depends(require_login)):
        from core.rule_backtest import run_rule_backtest

        try:
            body = await _json_body(request)
            raw_symbols = body.get("symbols") or []
            if isinstance(raw_symbols, str):
                raw_symbols = raw_symbols.split(",")
            symbols = []
            for item in raw_symbols:
                symbol = str(item or "").strip().upper()
                if symbol and symbol not in symbols:
                    symbols.append(symbol)
            if not symbols:
                raise ValueError("At least one symbol is required")
            if len(symbols) > 200:
                raise ValueError("At most 200 symbols per rule backtest")
            rule = await asyncio.to_thread(_load_rule, rule_id)
            result = await asyncio.to_thread(
                run_rule_backtest,
                rule,
                symbols,
                market=(body.get("market") or "us").strip().lower(),
                start_date=body.get("start_date") or None,
                end_date=body.get("end_date") or None,
                position_notional=float(body.get("position_notional") or 10_000.0),
                txn_cost_pct=(None if body.get("txn_cost_pct") in (None, "")
                              else float(body["txn_cost_pct"])),
                slippage_pct=float(body.get("slippage_pct") or 0.0005),
            )
            return result
        except Exception as exc:  # noqa: BLE001
            return _failure(exc)

    @app.get("/api/rules/{rule_id}")
    async def api_rules_get(rule_id: str, _user: str = Depends(require_login)):
        result, error = await _run(lambda: _store().get_rule(rule_id))
        if error:
            return error
        if result is None:
            return _err("invalid-request", "Rule not found")
        return result

    @app.post("/api/rules")
    async def api_rules_save(request: Request, _user: str = Depends(require_login)):
        try:
            body = await _json_body(request)
            payload = body.get("payload") if isinstance(body.get("payload"), dict) else dict(body)
            if not isinstance(payload, dict):
                raise ValueError("The rule payload must be a JSON object")
            payload = dict(payload)
            if not isinstance(body.get("payload"), dict):
                for key in ("rule_id", "expected_generation"):
                    payload.pop(key, None)
            name = body.get("name") or payload.get("name") or ""
            rule_id = (body.get("rule_id") or "").strip() or None
            expected = body.get("expected_generation")
            result = await asyncio.to_thread(
                _store().save_rule,
                name,
                payload,
                rule_id=rule_id,
                expected_generation=(None if expected is None else int(expected)),
            )
            return JSONResponse(result, status_code=201)
        except Exception as exc:  # noqa: BLE001
            return _failure(exc)

    @app.delete("/api/rules/{rule_id}")
    async def api_rules_delete(rule_id: str, _user: str = Depends(require_login)):
        result, error = await _run(lambda: _store().delete_rule(rule_id))
        if error:
            return error
        return {"ok": True}
