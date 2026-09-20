"""UI-independent backtest service: presets, durable runs, offline execution.

Web, desktop and the editor's automatic evaluation share this one typed
contract. Slow work (bar loading, replay, pattern analysis) never runs inside a
database transaction; the durable ``jobs``/``backtest_runs``/``results`` rows are
the authority, and an in-process cancellation event is only a fast path.
"""
from __future__ import annotations

import asyncio
import json
import threading
from datetime import datetime, timezone
from pathlib import Path

from psycopg.types.json import Jsonb

from core.backtest_jobs import BacktestJobStore
from core.backtest_params import effective_parameters, resolve_symbols
from core.market import get_market
from core.pattern_edit_store import Conflict, EditError, canonical, digest, uid
from core.pattern_editor_contracts import (
    BacktestMetrics, BacktestPreset, BacktestRequest, BacktestResult, ContentRef,
    DomainError, ErrorCode, FrozenRunInputs, JobProgress, JobState, Parameter,
    RuntimeIdentity, SourceFile,
)
from core.pattern_provenance import engine_hash
from core.pattern_versions import PatternVersions
from core.stream_backtest import ReplayCancelled, StreamReplay


class Cancelled(EditError):
    """Raised inside a runner when the durable cancel flag is observed."""


def run_coro(coro):
    """Run a coroutine even when called from inside a live event loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    box: dict = {}

    def _target() -> None:
        try:
            box["value"] = asyncio.run(coro)
        except BaseException as exc:  # noqa: BLE001 - re-raised below
            box["error"] = exc

    thread = threading.Thread(target=_target, daemon=True)
    thread.start()
    thread.join()
    if "error" in box:
        raise box["error"]
    return box["value"]


def runtime_identity(root) -> RuntimeIdentity:
    from core.pattern_versions import runtime_manifest

    manifest = runtime_manifest(root)
    root = Path(root)
    files = []
    for path, sha in sorted(manifest["files"].items()):
        location = root / path
        size = location.stat().st_size if location.is_file() else 0
        files.append(SourceFile(
            path=path,
            content=ContentRef(sha256=sha, size_bytes=size, media_type="text/x-python"),
            role="trusted-runtime",
        ))
    packages = tuple(Parameter(name=str(name), value=str(version))
                     for name, version in sorted(manifest["packages"].items()))
    return RuntimeIdentity(
        python_tag=str(manifest.get("cache_tag") or manifest.get("python", ""))[:200],
        engine_sha256=engine_hash(),
        packages=packages,
        trusted_files=tuple(files),
    )


class BacktestService:
    """One typed, durable service contract for every backtest consumer."""

    def __init__(self, store=None, *, job_store=None, versions=None,
                 dataset_root: str | Path | None = None):
        from config import settings
        from core.pattern_edit_store import EditStore

        self.store = store or EditStore()
        self.versions = versions or PatternVersions(self.store)
        self.jobs = job_store or BacktestJobStore(self.store)
        self.dataset_root = Path(
            dataset_root or settings.backtest_dataset_dir or "data/barcache")

    # ── presets ──────────────────────────────────────────────────────────
    def save_preset(self, name: str, settings, *, preset_id: str | None = None,
                    expected_generation: int | None = None) -> BacktestPreset:
        if not name or not str(name).strip():
            raise EditError("A preset needs a name")
        settings = self._validate_settings(settings)
        with self.store.transaction() as con:
            if preset_id is None:
                preset = BacktestPreset(preset_id=uid(), generation=0, name=name,
                                        settings=settings)
                con.execute("INSERT INTO presets(id,generation,payload) VALUES(%s,0,%s)",
                            (preset.preset_id, Jsonb(preset.model_dump(mode="json"))))
                return preset
            row = con.execute("SELECT * FROM presets WHERE id=%s FOR UPDATE",
                              (preset_id,)).fetchone()
            if row is None:
                raise EditError("Preset not found")
            if expected_generation is not None and row["generation"] != expected_generation:
                raise Conflict("Preset changed; reload before retrying")
            preset = BacktestPreset(preset_id=preset_id, generation=row["generation"] + 1,
                                    name=name, settings=settings)
            con.execute("UPDATE presets SET generation=%s, payload=%s, "
                        "updated_at=clock_timestamp() WHERE id=%s",
                        (preset.generation, Jsonb(preset.model_dump(mode="json")), preset_id))
            return preset

    def list_presets(self) -> list[BacktestPreset]:
        with self.store.connect() as con:
            rows = con.execute("SELECT payload FROM presets ORDER BY updated_at, id").fetchall()
        return [BacktestPreset.model_validate(row["payload"]) for row in rows]

    def get_preset(self, preset_id: str) -> BacktestPreset:
        with self.store.connect() as con:
            row = con.execute("SELECT payload FROM presets WHERE id=%s", (preset_id,)).fetchone()
        if row is None:
            raise EditError("Preset not found")
        return BacktestPreset.model_validate(row["payload"])

    # ── request preparation ──────────────────────────────────────────────
    def _validate_settings(self, settings):
        if settings.timeframe != "1d":
            raise EditError("Only the 1d timeframe is supported")
        return settings

    def _pin_versions(self, request: BacktestRequest) -> dict[str, str]:
        selected = {v.pattern_id: v.version_id for v in request.versions}
        return self.versions.resolve(selected=selected)

    def prepare(self, request: BacktestRequest) -> dict:
        """Validate a request without touching bars; returns the frozen plan."""
        request = _coerce_request(request)
        settings = self.validate_settings(request.preset.settings)
        pinned = self._pin_versions(request)
        symbols = resolve_symbols(settings)
        return {"request": request, "settings": settings, "versions": pinned,
                "symbols": symbols,
                "request_sha256": digest(canonical(request.model_dump(mode="json")))}

    def validate_settings(self, settings):
        """Reject unsupported or contradictory execution settings."""
        settings = self._validate_settings(settings)
        unsupported = [name for name, flag in (
            ("kronos_gate", settings.execution.kronos_gate),
            ("kronos_rank", settings.execution.kronos_rank),
            ("collect_first", settings.execution.collect_first),
        ) if flag]
        if unsupported:
            raise EditError("Unsupported in this release: " + ", ".join(unsupported))
        if settings.mode == "offline" and settings.execution.volume_gate:
            raise EditError("The volume gate is only available for historical-stream runs")
        if (settings.mode == "historical-stream"
                and settings.execution.sizing_mode != "fixed-notional"):
            raise EditError("Only fixed-notional sizing is supported for historical-stream runs")
        return settings

    def store_config(self) -> dict:
        """Connection config only — never a live connection across a process boundary."""
        return {"root": str(self.store.root), "dsn": self.store._dsn,
                "schema": self.store.schema}

    # ── submission ───────────────────────────────────────────────────────
    def submit(self, request: BacktestRequest) -> dict:
        request = _coerce_request(request)
        plan = self.prepare(request)
        payload = {"request": request.model_dump(mode="json"), "progress": {}}
        job = self.jobs.create(
            request_sha256=plan["request_sha256"],
            idempotency_key=request.idempotency_key,
            payload=payload,
            preset_id=request.preset.preset_id,
        )
        return self._job_view(job)

    def submit_and_run(self, request: BacktestRequest, *, worker_id: str | None = None) -> dict:
        job = self.submit(request)
        if job["state"] == "queued":
            self.execute(job["id"], worker_id=worker_id)
        return self.status(job["id"])

    # ── execution ────────────────────────────────────────────────────────
    def execute(self, job_id: str, *, worker_id: str | None = None,
                cancel_event: threading.Event | None = None) -> dict:
        """Run one already-submitted job, writing durable inputs and results."""
        worker_id = worker_id or f"inline-{uid()}"
        with self.store.transaction() as con:
            self.jobs.register_worker(worker_id, con)
            row = con.execute(
                "UPDATE jobs SET owner=%s, lease_token=%s, "
                "lease_expires_at=clock_timestamp() + interval '5 minutes', "
                "state='backtesting', updated_at=clock_timestamp() "
                "WHERE id=%s AND state='queued' RETURNING *", (worker_id, uid(), job_id)
            ).fetchone()
        if row is None:
            raise EditError("Job is not queued; it may already be running or finished")
        return self._run_claimed(row, worker_id, cancel_event=cancel_event)

    def run_once(self, worker_id: str | None = None, *, lease_seconds: int = 300) -> dict | None:
        """Claim and run the oldest queued job. None when nothing is due."""
        worker_id = worker_id or f"runner-{uid()}"
        self.jobs.expire_leases()
        row = self.jobs.claim(worker_id, lease_seconds=lease_seconds)
        if row is None:
            return None
        return self._run_claimed(row, worker_id)

    def _run_claimed(self, row: dict, owner: str,
                     cancel_event: threading.Event | None = None) -> dict:
        job_id = row["id"]
        event = cancel_event or _register_cancel(job_id)
        try:
            request = _coerce_request(row["payload"]["request"])
            plan = self.prepare(request)
            if self.jobs.cancel_requested(job_id):
                raise Cancelled("Job cancelled before execution")
            inputs = self.freeze_inputs(request, plan["symbols"])
            self._record_run(job_id, inputs)
            stream = plan["settings"].mode == "historical-stream"
            unit = "sessions" if stream else "symbols"
            self.jobs.touch(job_id, owner, state="backtesting",
                            progress={"completed_units": 0,
                                      "total_units": len(plan["symbols"]),
                                      "unit": unit})
            if stream:
                result, artifacts = self._execute_stream(job_id, inputs, event, owner=owner)
            else:
                result, artifacts = self._execute_offline(job_id, inputs, event, owner=owner)
            self._record_result(job_id, result)
            total = (artifacts["trades"]["meta"]["sessions"] if stream
                     else len(plan["symbols"]))
            return self.jobs.finish(
                job_id, owner, JobState.COMPLETED.value,
                progress={"completed_units": total, "total_units": total, "unit": unit})
        except (Cancelled, ReplayCancelled):
            return self.jobs.finish(job_id, owner, JobState.CANCELLED.value,
                                    error=_error(ErrorCode.EXECUTION_FAILED,
                                                 "Run cancelled", retryable=True))
        except EditError as exc:
            return self.jobs.finish(job_id, owner, JobState.FAILED.value,
                                    error=_error(ErrorCode.EXECUTION_FAILED, str(exc)))
        except Exception:  # noqa: BLE001 - never leak stderr/DSNs to callers
            from utils.logger import log
            log.exception(f"BacktestService | job {job_id} failed")
            return self.jobs.finish(job_id, owner, JobState.FAILED.value,
                                    error=_error(ErrorCode.EXECUTION_FAILED,
                                                 "Backtest failed; see server logs"))
        finally:
            _release_cancel(job_id)

    # ── frozen inputs / datasets ─────────────────────────────────────────
    def dataset_ref(self, settings, symbols: list[str]) -> dict:
        """Freeze the daily tape once; the returned ContentRef is the authority."""
        settings = self.validate_settings(settings)
        rows = self._load_frozen_rows(settings, symbols)
        if not rows:
            raise EditError("No frozen daily history available for the selected symbols")
        return self.store.blob(canonical(rows), "application/json")

    def freeze_inputs(self, request: BacktestRequest, symbols: list[str]) -> FrozenRunInputs:
        settings = request.preset.settings
        dataset = self.dataset_ref(settings, symbols)
        effective = effective_parameters(settings, {"barcache_dir": str(self.dataset_root)})
        runtime = runtime_identity(self.store.root)
        # Identity is the frozen inputs themselves: versions, symbols, dataset,
        # runtime and every effective setting. Transport fields (idempotency
        # key, retry pointer, preset id/name) are deliberately excluded so an
        # identical run can reuse a prior result.
        inputs_sha = digest(canonical({
            "versions": [v.model_dump(mode="json") for v in request.versions],
            "symbols": list(symbols),
            "dataset": {"sha256": dataset["sha256"], "size_bytes": dataset["size_bytes"]},
            "runtime": runtime.model_dump(mode="json"),
            "effective": [p.model_dump(mode="json") for p in effective],
        }))
        return FrozenRunInputs(request=request, resolved_symbols=tuple(symbols),
                               dataset=dataset, runtime=runtime,
                               effective_parameters=effective, inputs_sha256=inputs_sha)

    def _load_frozen_rows(self, settings, symbols: list[str]) -> dict[str, list[list]]:
        from zoneinfo import ZoneInfo

        from data.barcache import load as load_barcache

        session_tz = get_market(settings.market).session_tz
        if isinstance(session_tz, str):
            session_tz = ZoneInfo(session_tz)

        rows: dict[str, list[list]] = {}
        for symbol in symbols:
            candles = load_barcache(settings.market, symbol, root=self.dataset_root)
            if not candles:
                continue
            # One bar per session: vendor history can return both a
            # midnight-stamped row and the real session bar for the same day,
            # which the causal replay rejects as duplicate timestamps.
            unique: dict[str, object] = {}
            for candle in candles:
                if candle.timestamp is None:
                    continue
                unique[candle.timestamp.astimezone(session_tz).date().isoformat()] = candle
            ordered = sorted(unique.values(), key=lambda candle: candle.timestamp)
            rows[symbol] = [
                [c.timestamp.isoformat(), float(c.open), float(c.high),
                 float(c.low), float(c.close), float(c.volume or 0.0)]
                for c in ordered
            ]
        return rows

    def _record_run(self, run_id: str, inputs: FrozenRunInputs) -> None:
        with self.store.transaction() as con:
            con.execute(
                "INSERT INTO backtest_runs(id,job,inputs_sha256,payload) VALUES(%s,%s,%s,%s) "
                "ON CONFLICT(id) DO NOTHING",
                (run_id, run_id, inputs.inputs_sha256, Jsonb(inputs.model_dump(mode="json"))))
            for selection in inputs.request.versions:
                con.execute("INSERT INTO run_versions(run,pattern,version) VALUES(%s,%s,%s) "
                            "ON CONFLICT(run,pattern) DO NOTHING",
                            (run_id, selection.pattern_id, selection.version_id))

    # ── offline adapter ──────────────────────────────────────────────────
    def _execute_offline(self, job_id: str, inputs: FrozenRunInputs,
                         event: threading.Event, *, owner: str) -> tuple[BacktestResult, dict]:
        from core.backtester import Backtester

        settings = inputs.request.preset.settings
        version_set = {v.pattern_id: v.version_id for v in inputs.request.versions}
        total = max(1, len(inputs.resolved_symbols))

        def _tick(completed: int, _total: int) -> None:
            if event.is_set():
                raise Cancelled("Run cancelled")
            self.jobs.touch(job_id, owner,
                            progress={"completed_units": int(completed),
                                      "total_units": total, "unit": "symbols"})

        backtester = Backtester(
            list(inputs.resolved_symbols),
            barcache_dir=str(self.dataset_root),
            market=settings.market,
            txn_cost_pct=settings.execution.txn_cost_pct,
            position_notional=settings.execution.position_notional,
            version_set=version_set,
            store_config=self.store_config(),
            progress_callback=_tick,
        )
        result = run_coro(backtester.run())
        if event.is_set():
            raise Cancelled("Run cancelled")
        artifacts = self._offline_artifacts(result, settings)
        contract = self._build_result(job_id, inputs, artifacts, artifacts["metrics"], settings)
        return contract, artifacts

    def _execute_stream(self, job_id: str, inputs: FrozenRunInputs,
                        event: threading.Event, *, owner: str) -> tuple[BacktestResult, dict]:
        from core.pattern_loader import discover

        settings = inputs.request.preset.settings
        profile = get_market(settings.market)
        pinned = {v.pattern_id: v.version_id for v in inputs.request.versions}
        patterns = discover(disabled=(), version_set=pinned, store=self.store)
        # The frozen dataset blob is the authority: the replay cannot see a
        # different tape than the one recorded in the run's inputs hash.
        dataset = self._read_json(inputs.dataset)

        def _tick(completed: int, total: int) -> None:
            if event.is_set():
                raise Cancelled("Run cancelled")
            self.jobs.touch(job_id, owner,
                            progress={"completed_units": int(completed),
                                      "total_units": int(total), "unit": "sessions"})

        replay = StreamReplay(dataset=dataset, patterns=patterns, settings=settings,
                              session_tz=profile.session_tz, cancel=event, progress=_tick)
        artifacts = replay.run()
        if event.is_set():
            raise Cancelled("Run cancelled")
        contract = self._build_result(job_id, inputs, artifacts, artifacts["metrics"], settings)
        return contract, artifacts

    def _offline_artifacts(self, result, settings) -> dict:
        ordered = sorted(result.trades, key=lambda t: t.exit_date)
        equity: list[dict] = []
        running = 0.0
        peak = 0.0
        max_dd = 0.0
        for trade in ordered:
            running += trade.pnl_usd
            peak = max(peak, running)
            max_dd = max(max_dd, peak - running)
            equity.append({"date": trade.exit_date.isoformat(),
                           "realized_pnl": round(running, 6),
                           "unrealized_pnl": 0.0,
                           "equity": round(settings.execution.initial_capital + running, 6)})
        drawdown_pct = (max_dd / settings.execution.initial_capital * 100.0
                        if settings.execution.initial_capital > 0 else 0.0)
        cost = settings.execution.txn_cost_pct
        fees = sum((t.entry_price + t.exit_price) * cost * (t.qty or 0.0)
                   for t in result.trades)
        realized = sum(t.pnl_usd for t in result.trades)
        return {
            "trades": result.to_dict(),
            "signals": {"total_signals": result.total_signals,
                        "blocked": list(result.blocked), "filtered": list(result.filtered)},
            "equity": equity,
            "open_positions": [],
            "logs": {"version": result.version},
            "metrics": {
                "trade_count": len(result.trades),
                "wins": result.win_count,
                "realized_pnl": round(realized, 6),
                "unrealized_pnl": 0.0,
                "open_count": 0,
                "fees": round(fees, 6),
                "drawdown_pct": round(drawdown_pct, 6),
            },
        }

    def _build_result(self, run_id: str, inputs: FrozenRunInputs, artifacts: dict,
                      metrics: dict, settings) -> BacktestResult:
        profile = get_market(settings.market)
        count = int(metrics["trade_count"])
        metrics_model = BacktestMetrics(
            trade_count=count,
            win_rate=(int(metrics["wins"]) / count) if count else None,
            realized_pnl=round(float(metrics["realized_pnl"]), 4),
            unrealized_pnl=round(float(metrics["unrealized_pnl"]), 4),
            net_pnl=round(float(metrics["realized_pnl"]) + float(metrics["unrealized_pnl"]), 4),
            fees=round(float(metrics["fees"]), 6),
            max_drawdown_pct=round(float(metrics["drawdown_pct"]), 6),
            open_position_count=int(metrics["open_count"]),
            currency=profile.currency,
        )
        return BacktestResult(
            run_id=run_id,
            completed_at=datetime.now(timezone.utc),
            inputs=inputs,
            metrics=metrics_model,
            trades=self._ref(artifacts["trades"]),
            signals=self._ref(artifacts["signals"]),
            equity_curve=self._ref(artifacts["equity"]),
            open_positions=self._ref(artifacts["open_positions"]),
            logs=self._ref(artifacts["logs"]),
            base_comparison_run_id=None,
        )

    def _ref(self, payload) -> ContentRef:
        return ContentRef.model_validate(
            self.store.blob(canonical(_json_safe(payload)), "application/json"))

    def _record_result(self, run_id: str, result: BacktestResult) -> None:
        with self.store.transaction() as con:
            con.execute("INSERT INTO results(id,run,payload) VALUES(%s,%s,%s) "
                        "ON CONFLICT(run) DO NOTHING",
                        (uid(), run_id, Jsonb(result.model_dump(mode="json"))))
            for selection in result.inputs.request.versions:
                con.execute("UPDATE version_lifecycle SET successful_run_id=%s "
                            "WHERE version=%s AND successful_run_id IS DISTINCT FROM %s",
                            (run_id, selection.version_id, run_id))

    # ── status / results ─────────────────────────────────────────────────
    def _job_view(self, row: dict) -> dict:
        payload = row.get("payload") or {}
        return {"id": row["id"], "state": row["state"], "attempt": row["attempt"],
                "retry_of": row.get("retry_of"), "preset": row.get("preset"),
                "progress": payload.get("progress", {}), "error": payload.get("error"),
                "cancel_requested": bool(payload.get("cancel_requested"))}

    def status(self, run_id: str) -> dict:
        view = self._job_view(self.jobs.find(run_id))
        view["run_id"] = run_id
        return view

    def progress(self, run_id: str) -> JobProgress:
        row = self.jobs.find(run_id)
        payload = row.get("payload") or {}
        stored = payload.get("progress") or {}
        error = payload.get("error")
        return JobProgress(
            job_id=run_id,
            attempt=int(row["attempt"]),
            state=JobState(row["state"]),
            completed_units=int(stored.get("completed_units") or 0),
            total_units=stored.get("total_units"),
            unit=stored.get("unit") or "symbols",
            updated_at=row["updated_at"],
            version_id=stored.get("version_id"),
            run_id=run_id,
            error=DomainError.model_validate(error) if error else None,
        )

    def result(self, run_id: str) -> dict | None:
        with self.store.connect() as con:
            row = con.execute("SELECT payload FROM results WHERE run=%s", (run_id,)).fetchone()
            if row is None:
                return None
            run = con.execute("SELECT payload FROM backtest_runs WHERE id=%s",
                              (run_id,)).fetchone()
        result = BacktestResult.model_validate(row["payload"])
        return {
            "result": result,
            "inputs": FrozenRunInputs.model_validate(run["payload"]) if run else None,
            "trades": self._read_json(result.trades),
            "signals": self._read_json(result.signals),
            "equity": self._read_json(result.equity_curve),
            "open_positions": self._read_json(result.open_positions),
            "logs": self._read_json(result.logs),
        }

    def _read_json(self, ref: ContentRef):
        return json.loads(self.store.read_blob(ref.model_dump(mode="json")))

    def find_reusable_run(self, inputs_sha256: str) -> str | None:
        with self.store.connect() as con:
            row = con.execute(
                "SELECT r.run FROM results r JOIN backtest_runs b ON b.id=r.run "
                "WHERE b.inputs_sha256=%s LIMIT 1", (inputs_sha256,)).fetchone()
        return row["run"] if row else None

    def cancel(self, run_id: str, *, actor: str = "user") -> dict:
        event = _cancel_event(run_id)
        if event is not None:
            event.set()
        return self._job_view(self.jobs.request_cancel(run_id, actor=actor))

    def retry(self, run_id: str) -> dict:
        return self._job_view(self.jobs.retry(run_id))

    def runs_for_version(self, version_id: str) -> list[dict]:
        with self.store.connect() as con:
            rows = con.execute(
                "SELECT b.id, j.state, r.payload AS result FROM run_versions v "
                "JOIN backtest_runs b ON b.id=v.run JOIN jobs j ON j.id=b.job "
                "LEFT JOIN results r ON r.run=b.id WHERE v.version=%s ORDER BY b.created_at",
                (version_id,)).fetchall()
        return [dict(r) for r in rows]


def _coerce_request(request) -> BacktestRequest:
    if isinstance(request, BacktestRequest):
        return request
    if isinstance(request, dict):
        return BacktestRequest.model_validate(request)
    raise EditError("Unsupported backtest request payload")


def request_from_values(service: "BacktestService", values: dict,
                        *, versions: dict | None = None,
                        preset_id: str | None = None,
                        preset_name: str | None = None,
                        idempotency_key: str | None = None) -> BacktestRequest:
    """Build one durable request from raw form values (shared by both frontends).

    ``preset_id`` reuses a saved preset's frozen settings; otherwise the values
    are validated and persisted as a new named preset. Version defaults come
    from the published catalog when the caller does not pin them.
    """
    from core.backtest_params import settings_from_values
    from core.pattern_editor_contracts import BacktestPreset, VersionSelection

    if preset_id:
        preset = service.get_preset(preset_id)
    else:
        settings = settings_from_values(values)
        name = (preset_name or "").strip() or "backtest run"
        # Find-or-create by (name, settings) so an identical duplicate Submit
        # maps to the same frozen preset and therefore the same durable request.
        preset = next((p for p in service.list_presets()
                       if p.name == name and p.settings == settings), None)
        if preset is None:
            preset = service.save_preset(name, settings)

    selected = dict(versions) if versions else {
        row["id"]: row["active"] for row in service.versions.catalog()
        if row["enabled"] and row["active"]
    }
    selections = tuple(VersionSelection(pattern_id=pattern, version_id=version)
                       for pattern, version in sorted(selected.items()) if version)
    if not selections:
        raise EditError("No pattern versions selected")
    key = (idempotency_key or "").strip() or f"run-{uid()}"
    return BacktestRequest(idempotency_key=key, versions=selections, preset=preset)


def _error(code: ErrorCode, message: str, *, retryable: bool = False) -> dict:
    return DomainError(code=code, message=message, retryable=retryable).model_dump(mode="json")


def _json_safe(value):
    """Replace non-finite floats (e.g. an infinite profit factor) with null."""
    import math

    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


# ── in-process cancellation registry ─────────────────────────────────────
_CANCEL_LOCK = threading.Lock()
_CANCEL_EVENTS: dict[str, threading.Event] = {}


def _cancel_event(run_id: str) -> threading.Event | None:
    with _CANCEL_LOCK:
        return _CANCEL_EVENTS.get(run_id)


def _register_cancel(job_id: str) -> threading.Event:
    event = threading.Event()
    with _CANCEL_LOCK:
        _CANCEL_EVENTS[job_id] = event
    return event


def _release_cancel(job_id: str) -> None:
    with _CANCEL_LOCK:
        _CANCEL_EVENTS.pop(job_id, None)
