"""Authenticated Experiments page and isolated batch APIs."""
from __future__ import annotations

import asyncio
import json
from datetime import date, timedelta

from fastapi import Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from core.experiments.contracts import BatchRequest
from core.experiments.service import get_service
from core.experiments.store import Conflict
from core.market import default_market, get_market
from web.auth import require_login
from utils.logger import log


KINDS = {"signals", "trades", "positions", "outcomes", "logs"}


class URLRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(min_length=8, max_length=2048)


def _failure(exc: Exception) -> JSONResponse:
    if isinstance(exc, KeyError):
        return JSONResponse({"detail": str(exc).strip("'")}, status_code=404)
    if isinstance(exc, Conflict):
        return JSONResponse({"detail": str(exc)}, status_code=409)
    if isinstance(exc, ValidationError):
        return JSONResponse({"detail": "; ".join(error["msg"] for error in exc.errors())}, status_code=400)
    if isinstance(exc, ValueError):
        return JSONResponse({"detail": str(exc)}, status_code=400)
    log.exception("Experiments | request failed")
    return JSONResponse({"detail": "Experiment storage/service unavailable; check server logs"}, status_code=503)


def register_experiment_routes(app, templates, ctx) -> None:
    @app.get("/experiments", response_class=HTMLResponse)
    async def page(request: Request, _user: str = Depends(require_login)):
        from datetime import datetime
        from zoneinfo import ZoneInfo

        today = datetime.now(ZoneInfo("Asia/Manila")).date()
        market = default_market()
        return templates.TemplateResponse(request, "experiments.html", ctx(
            request, active="experiments", default_market=market.id,
            default_n_symbols=market.default_n_symbols,
            us_n_symbols=get_market("us").default_n_symbols,
            ph_n_symbols=get_market("ph").default_n_symbols,
            start_default=(today - timedelta(days=365)).isoformat(),
            end_default=today.isoformat()))

    @app.get("/api/experiments/catalog")
    async def api_catalog(_user: str = Depends(require_login)):
        try:
            return {"sources": await asyncio.to_thread(get_service().catalog)}
        except Exception as exc:
            return _failure(exc)

    @app.get("/api/experiments/sources/{source_id}")
    async def api_source(source_id: str, _user: str = Depends(require_login)):
        try:
            return await asyncio.to_thread(get_service().source, source_id)
        except Exception as exc:
            return _failure(exc)

    @app.get("/api/experiments/sources/{source_id}/video")
    async def api_source_video(source_id: str, _user: str = Depends(require_login)):
        try:
            path = await asyncio.to_thread(get_service().source_video, source_id)
            media = {".mp4": "video/mp4", ".webm": "video/webm", ".mkv": "video/x-matroska"}
            return FileResponse(path, filename=path.name, media_type=media.get(path.suffix, "application/octet-stream"),
                                content_disposition_type="inline")
        except Exception as exc:
            return _failure(exc)

    @app.get("/api/experiments/imports")
    async def api_imports(_user: str = Depends(require_login)):
        try:
            service = get_service().imports
            return {"imports":await asyncio.to_thread(service.jobs.listing),
                    "capabilities":await asyncio.to_thread(service.capabilities)}
        except Exception as exc:
            return _failure(exc)

    @app.post("/api/experiments/imports", status_code=202)
    async def api_import_url(request: Request, _user: str = Depends(require_login)):
        try:
            submitted = URLRequest.model_validate(await request.json())
            return await asyncio.to_thread(get_service().imports.submit, submitted.url)
        except Exception as exc:
            return _failure(exc)

    @app.get("/api/experiments/imports/{identity}")
    async def api_import_detail(identity: str, _user: str = Depends(require_login)):
        try:
            return await asyncio.to_thread(get_service().imports.detail, identity)
        except Exception as exc:
            return _failure(exc)

    @app.post("/api/experiments/imports/{identity}/retry", status_code=202)
    async def api_import_retry(identity: str, _user: str = Depends(require_login)):
        try:
            return await asyncio.to_thread(get_service().imports.retry, identity)
        except Exception as exc:
            return _failure(exc)

    @app.post("/api/experiments/imports/{identity}/cancel")
    async def api_import_cancel(identity: str, _user: str = Depends(require_login)):
        try:
            return await asyncio.to_thread(get_service().imports.jobs.cancel, identity)
        except Exception as exc:
            return _failure(exc)

    @app.get("/api/experiments/imports/{identity}/files/{name:path}")
    async def api_import_file(identity: str, name: str, _user: str = Depends(require_login)):
        try:
            path = await asyncio.to_thread(get_service().imports.artifact, identity, name)
            return FileResponse(path, filename=path.name,
                                content_disposition_type="inline" if path.suffix in {".mp4", ".webm", ".jpg"} else "attachment")
        except Exception as exc:
            return _failure(exc)

    @app.get("/api/experiments/runs")
    async def api_batches(_user: str = Depends(require_login)):
        try:
            return {"batches": await asyncio.to_thread(get_service().store.listing)}
        except Exception as exc:
            return _failure(exc)

    @app.post("/api/experiments/runs", status_code=202)
    async def api_start(request: Request, _user: str = Depends(require_login)):
        try:
            payload = await request.json()
            if not isinstance(payload, dict):
                raise ValueError("Expected a JSON object")
            validated = BatchRequest.model_validate(payload)
            return await asyncio.to_thread(get_service().submit, validated)
        except Exception as exc:
            return _failure(exc)

    @app.get("/api/experiments/runs/{batch_id}")
    async def api_status(batch_id: str, _user: str = Depends(require_login)):
        try:
            return await asyncio.to_thread(get_service().snapshot, batch_id)
        except Exception as exc:
            return _failure(exc)

    @app.post("/api/experiments/runs/{batch_id}/cancel")
    async def api_cancel(batch_id: str, _user: str = Depends(require_login)):
        try:
            await asyncio.to_thread(get_service().store.cancel, batch_id)
            return await asyncio.to_thread(get_service().snapshot, batch_id)
        except Exception as exc:
            return _failure(exc)

    @app.get("/api/experiments/runs/{batch_id}/folders/{source_id}")
    async def api_detail(batch_id: str, source_id: str, _user: str = Depends(require_login)):
        try:
            return await asyncio.to_thread(get_service().detail, batch_id, source_id)
        except Exception as exc:
            return _failure(exc)

    @app.get("/api/experiments/runs/{batch_id}/folders/{source_id}/records")
    async def api_records(batch_id: str, source_id: str, kind: str = "signals",
                          after: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=500),
                          symbol: str | None = None, chronological: bool = False, _user: str = Depends(require_login)):
        try:
            if kind.removeprefix("holdout-") not in KINDS:
                raise ValueError("Unknown result table")
            await asyncio.to_thread(get_service().detail, batch_id, source_id)
            return await asyncio.to_thread(get_service().store.evidence, batch_id, source_id,
                                           kind, after=after, limit=limit, symbol=symbol, chronological=chronological)
        except Exception as exc:
            return _failure(exc)

    @app.get("/api/experiments/runs/{batch_id}/folders/{source_id}/chart")
    async def api_chart(batch_id: str, source_id: str, symbol: str,
                        record: int | None = Query(None, ge=1),
                        _user: str = Depends(require_login)):
        def build():
            import pandas as pd
            from analysis.chart_renderer import build_trade_viewer_payload

            detail = get_service().detail(batch_id, source_id)
            evidence = get_service().store.record(batch_id, source_id, record) if record else {}
            if evidence and evidence.get("symbol") != symbol:
                raise ValueError("Chart record belongs to a different symbol")
            rows = get_service().store.chart(batch_id, symbol)
            if len(rows) < 2:
                raise ValueError("Not enough frozen bars to chart")
            frame = pd.DataFrame(rows)
            market = detail["request"]["market"]
            profile = get_market(market)
            frame.index = pd.to_datetime(frame.pop("date")).dt.tz_localize(profile.session_tz)
            payload = build_trade_viewer_payload(frame, symbol=symbol, market=market,
                                                session_tz=profile.session_tz, trendlines=False,
                                                action=None,
                                                entry=evidence.get("entry_price"),
                                                stop=None if evidence.get("planned_risk") else evidence.get("stop_loss"), target=evidence.get("take_profit"),
                                                exit_price=evidence.get("exit_price"),
                                                exit_reason=evidence.get("exit_reason"),
                                                current=evidence.get("current"),
                                                entry_time=evidence.get("entry_date", evidence.get("date")),
                                                exit_time=evidence.get("exit_date"))
            candle_dates = {c["time"] for c in payload["candles"]}
            payload["markers"] = []
            def marker(day, label, color, shape="circle"):
                if day in candle_dates:
                    payload["markers"].append({"time": day, "text": label, "color": color,
                                               "shape": shape, "position": "aboveBar"})
            if evidence.get("record_kind", "").removeprefix("holdout-") in {"trades", "positions"}:
                marker(evidence.get("signal_date"), "Decision", "#e3a539")
                marker(evidence.get("entry_date"), "Filled " + evidence["action"], "#409ded",
                       "arrowUp" if evidence["action"] == "BUY" else "arrowDown")
                trigger = evidence.get("exit_trigger", {})
                marker(trigger.get("date"), trigger.get("reason"), "#ee9955")
                marker(evidence.get("exit_date"), "Exit fill", "#cd6f7b")
                trail = [{"time": p["date"], "value": p["active_stop"]} for p in evidence.get("trail_history", [])
                         if p["date"] in candle_dates]
                if trail:
                    payload["segments"].append({"data": trail, "color":"#da895d", "label":"Active ATR stop","step":True})
            elif evidence:
                status = evidence.get("status", "observation")
                marker(evidence.get("date"), status.replace("-", " "), "#409ded" if status == "filled" else "#e3a539")
            payload["markers"].sort(key=lambda m: m["time"])
            payload["event_evidence"] = evidence.get("evidence") or evidence.get("exit_trigger")
            payload["replay_id"] = batch_id
            return payload
        try:
            return await asyncio.to_thread(build)
        except Exception as exc:
            return _failure(exc)

    @app.get("/api/experiments/runs/{batch_id}/folders/{source_id}/export")
    async def api_export(batch_id: str, source_id: str, _user: str = Depends(require_login)):
        try:
            detail = await asyncio.to_thread(get_service().detail, batch_id, source_id)
            if detail["run"]["state"] in {"queued","processing"}:
                raise Conflict("Wait for this folder to finish before exporting its complete results")
        except Exception as exc:
            return _failure(exc)

        def lines():
            yield json.dumps({"kind": "manifest", **detail}, allow_nan=False) + "\n"
            for row in get_service().store.quarantined(batch_id):
                yield json.dumps({"kind": "quarantine", **row}, allow_nan=False) + "\n"
            kinds = sorted(KINDS)
            if detail["request"].get("holdout_start"):
                kinds.extend(["holdout-" + kind for kind in sorted(KINDS)])
            for kind in kinds:
                after = 0
                while True:
                    page = get_service().store.evidence(batch_id, source_id, kind, after=after, limit=500)
                    for row in page["rows"]:
                        yield json.dumps({"kind": kind, **row}, allow_nan=False) + "\n"
                    if page["next"] is None:
                        break
                    after = page["next"]
        return StreamingResponse(lines(), media_type="application/x-ndjson",
                                 headers={"Content-Disposition": f'attachment; filename="experiment-{source_id}-{batch_id}.jsonl"'})
