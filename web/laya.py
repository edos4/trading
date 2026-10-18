"""Laya tab routes (docs/laya.md section 9).

Focused route module registered by web/app.py. All routes require login.
`GET /laya` renders even when the optional feature is disabled. Status polling
never downloads models. `/health` stays independent of model readiness.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse

from web.auth import require_login

_ERROR_TITLES = {
    "invalid-request": 422,
    "schema-error": 422,
    "not-found": 404,
    "conflict": 409,
    "queue-full": 429,
    "unavailable": 503,
    "missing-model": 503,
    "missing-dependency": 503,
    "disabled": 503,
    "timeout": 503,
    "inference-failed": 502,
}


def _err(code: str, detail: str) -> JSONResponse:
    return JSONResponse(
        {"error_code": code, "detail": detail},
        status_code=_ERROR_TITLES.get(code, 400),
    )


def _service():
    from core.laya_service import get_service

    return get_service()


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


def _request_matches_job(body: dict[str, Any], job: dict[str, Any]) -> bool:
    """True when a retry carries equivalent semantic input to the stored job."""
    import hashlib

    from core.laya_store import get_store

    row = get_store().get_assessment(job.get("assessment_id") or "")
    if row is None:
        return False
    try:
        stored = json.loads(row["input_json"])
    except (TypeError, ValueError):
        return False
    market = str(body.get("market") or "").strip().lower()
    symbol = str(body.get("symbol") or "").strip().upper()
    task_id = str(body.get("task_id") or "").strip()
    if not task_id:
        mode = str(body.get("input_mode") or "").strip().lower()
        task_id = "event-review-v1" if mode == "text" else "candidate-review-v1"
    if stored.get("market") != market or stored.get("symbol") != symbol:
        return False
    if stored.get("question_schema_id") != task_id:
        return False
    if task_id == "event-review-v1":
        text = str(body.get("source_text") or "").strip()
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        docs = stored.get("documents") or []
        if not docs or docs[0].get("text_sha256") != digest:
            return False
        return True
    try:
        index = int(body.get("candidate_index", 0) or 0)
    except (TypeError, ValueError):
        index = 0
    cand = stored.get("candidate") or {}
    # Candidate retries match on detector+direction; bar cutoffs advance.
    return bool(cand)


def _assessment_view(row: dict[str, Any], *, include_input: bool = True) -> dict[str, Any]:
    from core.laya_store import get_store

    view: dict[str, Any] = {
        "assessment_id": row["id"],
        "market": row["market"],
        "symbol": row["symbol"],
        "task_id": row["task_id"],
        "input_mode": row["input_mode"],
        "decision_at": row["decision_at"],
        "as_of": row["as_of"],
        "calibration_status": row.get("calibration_status") or "uncalibrated",
        "elapsed_ms": int(row.get("elapsed_ms") or 0),
        "truncated": bool(row.get("truncated")),
        "created_at": row["created_at"],
        "rerun_of": row.get("rerun_of"),
        "complete": bool(row.get("normalized_json")),
    }
    if include_input:
        try:
            view["input"] = json.loads(row["input_json"])
        except (TypeError, ValueError):
            view["input"] = None
    if row.get("normalized_json"):
        try:
            view["result"] = json.loads(row["normalized_json"])
        except (TypeError, ValueError):
            view["result"] = None
    else:
        view["result"] = None
    if row.get("raw_json"):
        try:
            view["raw"] = json.loads(row["raw_json"])
        except (TypeError, ValueError):
            view["raw"] = None
    try:
        view["warnings"] = json.loads(row.get("warnings_json") or "[]")
    except ValueError:
        view["warnings"] = []
    view["decisions"] = get_store().decisions_for(row["id"])
    return view


def register_laya_routes(app, templates, ctx) -> None:
    @app.get("/laya", response_class=HTMLResponse)
    async def laya_page(request: Request, _user: str = Depends(require_login)):
        return templates.TemplateResponse(
            request, "laya.html", ctx(request, active="laya"),
        )

    @app.get("/api/laya/status")
    async def api_laya_status(_user: str = Depends(require_login)):
        # No load side effects: never downloads models.
        return await asyncio.to_thread(_service().status)

    @app.get("/api/laya/tasks")
    async def api_laya_tasks(_user: str = Depends(require_login)):
        return {"tasks": await asyncio.to_thread(_service().tasks)}

    @app.post("/api/laya/assessments", status_code=202)
    async def api_laya_submit(request: Request, _user: str = Depends(require_login)):
        try:
            body = await _json_body(request)
        except ValueError as exc:
            return _err("invalid-request", str(exc))
        idempotency_key = str(body.get("idempotency_key") or body.get("client_request_id") or "").strip()
        if not idempotency_key:
            idempotency_key = f"laya-{uuid.uuid4().hex[:16]}"
        # Idempotency first: the same key with an equivalent request returns the
        # existing job without rebuilding wall-clock defaults (decision_at,
        # observed_at) that would otherwise make every retry look "different".
        # The same key with genuinely different input is a 409 conflict.
        try:
            prior = await asyncio.to_thread(_service().store.get_job_by_key, idempotency_key)
        except Exception:
            prior = None
        if prior is not None:
            try:
                match = await asyncio.to_thread(_request_matches_job, body, prior)
            except Exception:
                match = False
            if match:
                return JSONResponse(
                    {"job_id": prior["id"], "assessment_id": prior["assessment_id"],
                     "state": prior["state"], "idempotency_key": prior["idempotency_key"]},
                    status_code=202,
                )
            return _err("conflict", "idempotency key already used with different input")
        try:
            assessment = await asyncio.to_thread(_build_assessment, body)
        except ValueError as exc:
            return _err("schema-error", str(exc)[:500])
        except LookupError as exc:
            return _err("not-found", str(exc)[:500])
        status = await asyncio.to_thread(_service().status)
        if status.get("state") in ("disabled", "missing-dependency", "missing-model"):
            reason = status.get("reason") or status.get("state")
            code = "disabled" if status.get("state") == "disabled" else status["state"]
            return _err(code if code in _ERROR_TITLES else "unavailable", str(reason)[:300])
        try:
            job, _ = await asyncio.to_thread(
                _service().submit, assessment,
                idempotency_key=idempotency_key,
                rerun_of=body.get("rerun_of"),
            )
        except Exception as exc:  # noqa: BLE001 - mapped to stable codes
            from core.laya_service import ConflictError, QueueFullError

            if isinstance(exc, ConflictError):
                return _err("conflict", str(exc)[:300])
            if isinstance(exc, QueueFullError):
                return _err("queue-full", str(exc)[:300])
            if "saturated" in str(exc):
                return _err("queue-full", str(exc)[:300])
            return _err("unavailable", "Laya assessment unavailable")
        code = 202
        return JSONResponse(
            {"job_id": job["id"], "assessment_id": job["assessment_id"],
             "state": job["state"], "idempotency_key": job["idempotency_key"]},
            status_code=code,
        )

    @app.get("/api/laya/jobs/{job_id}")
    async def api_laya_job(job_id: str, _user: str = Depends(require_login)):
        job = await asyncio.to_thread(_service().store.get_job, job_id)
        if job is None:
            return _err("not-found", "Unknown Laya job")
        payload: dict[str, Any] = {
            "job_id": job["id"], "assessment_id": job["assessment_id"],
            "state": job["state"], "error_code": job.get("error_code"),
            "error_detail": job.get("error_detail"),
            "created_at": job.get("created_at"), "updated_at": job.get("updated_at"),
        }
        if job.get("state") == "succeeded":
            row = await asyncio.to_thread(
                _service().store.get_assessment, job["assessment_id"])
            if row is not None:
                payload["assessment"] = _assessment_view(row, include_input=False)
        return payload

    @app.post("/api/laya/jobs/{job_id}/cancel")
    async def api_laya_cancel(job_id: str, _user: str = Depends(require_login)):
        job = await asyncio.to_thread(_service().cancel, job_id)
        if job is None:
            return _err("not-found", "Unknown Laya job")
        return {"job_id": job["id"], "state": job["state"]}

    @app.get("/api/laya/assessments")
    async def api_laya_list(request: Request, _user: str = Depends(require_login)):
        q = request.query_params
        try:
            limit = max(1, min(int(q.get("limit") or 20), 100))
            offset = max(0, int(q.get("offset") or 0))
        except ValueError:
            return _err("invalid-request", "limit/offset must be integers")
        rows, total = await asyncio.to_thread(
            _service().store.list_assessments,
            market=(q.get("market") or "").strip().lower(),
            symbol=(q.get("symbol") or "").strip(),
            task_id=(q.get("task_id") or "").strip(),
            limit=limit, offset=offset,
        )
        return {
            "total": total, "limit": limit, "offset": offset,
            "assessments": [_assessment_view(r, include_input=False) for r in rows],
        }

    @app.get("/api/laya/assessments/{assessment_id}")
    async def api_laya_detail(assessment_id: str, _user: str = Depends(require_login)):
        row = await asyncio.to_thread(_service().store.get_assessment, assessment_id)
        if row is None:
            return _err("not-found", "Unknown Laya assessment")
        return _assessment_view(row)

    @app.get("/api/laya/assessments/{assessment_id}/export")
    async def api_laya_export(assessment_id: str, _user: str = Depends(require_login)):
        row = await asyncio.to_thread(_service().store.get_assessment, assessment_id)
        if row is None:
            return _err("not-found", "Unknown Laya assessment")
        view = _assessment_view(row)
        return {
            "export_version": "laya-export-v1",
            "exported_at": datetime.now(timezone.utc).isoformat(),
            **view,
        }

    @app.post("/api/laya/decisions")
    async def api_laya_decide(request: Request, _user: str = Depends(require_login)):
        try:
            body = await _json_body(request)
        except ValueError as exc:
            return _err("invalid-request", str(exc))
        assessment_id = str(body.get("assessment_id") or "").strip()
        policy_id = str(body.get("policy_id") or "laya-candidate-v1-default").strip()
        mode = str(body.get("mode") or "research").strip()
        if not assessment_id:
            return _err("invalid-request", "assessment_id is required")
        if mode not in ("off", "research", "shadow", "filter"):
            return _err("invalid-request", "mode must be off/research/shadow/filter")
        try:
            decision = await asyncio.to_thread(
                _service().evaluate, assessment_id, policy_id,
                mode=mode, candidate_id=body.get("candidate_id"),
                book_id=body.get("book_id"), run_id=body.get("run_id"),
            )
        except ValueError as exc:
            return _err("schema-error", str(exc)[:300])
        return {"decision": decision}


def _build_assessment(body: dict[str, Any]):
    """Validate text or a server-resolved candidate reference.

    Text mode needs pasted source text. Candidate mode loads existing evidence
    via the Explorer loaders and selects one candidate snapshot — never
    fabricating a signal when none exists.
    """
    from core.laya_context import build_candidate_input, build_text_input
    from core.laya_contracts import LayaCandidate, LayaRuntime

    market = str(body.get("market") or "").strip().lower()
    if market not in ("us", "ph"):
        raise ValueError("market must be us or ph")
    symbol = str(body.get("symbol") or "").strip().upper()
    if not symbol:
        raise ValueError("symbol is required")
    exchange = str(body.get("exchange") or ("PSE" if market == "ph" else "NASDAQ")).strip().upper()
    timeframe = str(body.get("timeframe") or "1d").strip()
    if timeframe not in ("1d", "1W"):
        raise ValueError("timeframe must be 1d or 1W")
    task_id = str(body.get("task_id") or "").strip()
    input_mode = str(body.get("input_mode") or "").strip().lower()
    if not task_id:
        task_id = "event-review-v1" if input_mode == "text" else "candidate-review-v1"
    assessment_id = str(body.get("assessment_id") or f"asm-{uuid.uuid4().hex[:16]}")
    runtime = LayaRuntime(
        package_version="laya-0.4.7",
        model_id=str(body.get("model_id") or "pinned"),
        backend="cpu",
    )

    if task_id == "event-review-v1" or input_mode == "text":
        assessment, _doc = build_text_input(
            assessment_id=assessment_id, market=market, symbol=symbol,
            exchange=exchange, timeframe=timeframe, task_id="event-review-v1",
            source_text=str(body.get("source_text") or ""),
            source_title=str(body.get("source_title") or ""),
            publisher=str(body.get("publisher") or ""),
            language=str(body.get("language") or "en"),
            publication_time=body.get("publication_time"),
            available_at=body.get("available_at"),
            decision_at=body.get("decision_at"),
            bar_close_at=body.get("bar_close_at"),
            runtime=runtime,
        )
        return assessment

    # Candidate mode: resolve the live candidate server-side.
    if task_id != "candidate-review-v1":
        raise ValueError(f"Unknown Laya task: {task_id}")
    snapshot = _resolve_candidate_snapshot(
        market=market, symbol=symbol, exchange=exchange, timeframe=timeframe,
        candidate_index=body.get("candidate_index", 0),
    )
    candidate = LayaCandidate(
        direction=snapshot["direction"],
        detector=snapshot["detector"],
        pattern_version_id=snapshot.get("pattern_version_id"),
        detector_confidence=snapshot["detector_confidence"],
        entry=snapshot.get("entry"),
        stop=snapshot.get("stop"),
        target=snapshot.get("target"),
        reward_risk=snapshot.get("reward_risk"),
        engine_eligibility=snapshot.get("engine_eligibility") or "unknown",
    )
    return build_candidate_input(
        assessment_id=assessment_id, market=market, symbol=symbol,
        exchange=exchange, timeframe=timeframe, task_id=task_id,
        candidate=candidate, closes=snapshot.get("closes"),
        volumes=snapshot.get("volumes"),
        decision_at=body.get("decision_at"), bar_close_at=body.get("bar_close_at"),
        currency="PHP" if market == "ph" else "USD", runtime=runtime,
    )


def _resolve_candidate_snapshot(
    *, market: str, symbol: str, exchange: str, timeframe: str,
    candidate_index: Any,
) -> dict[str, Any]:
    """Load existing evidence and select one candidate snapshot.

    Uses the Explorer loaders but copies an isolated snapshot so a concurrent
    market switch on the shared service cannot mutate the evidence mid-build.
    """
    try:
        index = int(candidate_index or 0)
    except (TypeError, ValueError):
        raise ValueError("candidate_index must be an integer") from None
    from web.services import get_explorer

    try:
        result = get_explorer().load_symbol(
            symbol, exchange, timeframe, run_patterns=True, market=market,
        )
    except ValueError as exc:
        raise LookupError(str(exc)) from exc
    signals = list(result.get("signals") or [])
    if not signals:
        raise LookupError(f"No candidate for {symbol}: loader found no signals")
    if index < 0 or index >= len(signals):
        raise ValueError(f"candidate_index {index} out of range ({len(signals)} candidates)")
    sig = dict(signals[index])
    action = str(sig.get("action") or "").upper()
    if action not in ("BUY", "SELL"):
        raise ValueError(f"Candidate action {action!r} is not tradeable")
    try:
        confidence = float(sig.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))
    price = sig.get("price")
    try:
        entry = float(price) if price is not None else None
    except (TypeError, ValueError):
        entry = None
    # Reward/risk + bars are computed facts, versioned by the context builder.
    closes: list[float] = []
    volumes: list[float] = []
    try:
        import csv
        import io as _io

        reader = csv.DictReader(_io.StringIO(result.get("csv") or ""))
        for row in reader:
            try:
                closes.append(float(row.get("close") or 0))
                volumes.append(float(row.get("volume") or row.get("Volume") or 0))
            except (TypeError, ValueError):
                continue
    except Exception:
        closes, volumes = [], []
    return {
        "direction": action,
        "detector": str(sig.get("pattern") or "unknown"),
        "pattern_version_id": None,
        "detector_confidence": confidence,
        "entry": entry,
        "stop": None,
        "target": None,
        "reward_risk": None,
        "engine_eligibility": "passed",
        "closes": closes[-60:] if closes else None,
        "volumes": volumes[-60:] if volumes else None,
    }
