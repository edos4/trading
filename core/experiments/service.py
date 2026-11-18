"""Background all-folder batches with frozen history and independent results."""
from __future__ import annotations

import hashlib
import threading
from importlib.metadata import version

import numpy as np
from collections import defaultdict
from pathlib import Path

from core.experiments.catalog import catalog, sources
from core.experiments.contracts import BatchRequest
from core.experiments.history import available_symbols, frame, normalize_bars, raw_symbol
from core.experiments.replay import Cancelled, replay
from core.experiments.store import ExperimentStore
from core.market import get_market
from utils.logger import log


class ExperimentService:
    def __init__(self, store: ExperimentStore, *, enumerate_symbols=None, loader=None):
        self.store = store
        self.enumerate_symbols = enumerate_symbols or available_symbols
        self.loader = loader or raw_symbol
        self._threads: dict[str, threading.Thread] = {}
        self._lock = threading.Lock()

    def submit(self, request: BatchRequest, *, background: bool = True) -> dict:
        manifest = []
        originals = {s["id"]: s for s in sources()}
        for entry in catalog():
            entry = {**originals[entry["id"]], **entry}
            if entry["availability"] == "ready" and request.market not in entry["markets"]:
                entry = {**entry, "availability": "blocked", "blocker": "Source universe/contract is unsupported in this market"}
            if entry.get("requires_borrow") and request.market == "us" and request.short_policy == "exclude":
                entry = {**entry, "availability": "blocked", "blocker": "Short-only study requires the assumed-borrow research setting"}
            manifest.append(entry)
        batch_id, created = self.store.create(request.model_dump(mode="json"), manifest)
        if created:
            if background:
                thread = threading.Thread(target=self.execute, args=(batch_id,),
                                          name=f"experiments-{batch_id[:8]}", daemon=True)
                with self._lock:
                    self._threads[batch_id] = thread
                thread.start()
            else:
                self.execute(batch_id)
        return self.snapshot(batch_id)

    def snapshot(self, batch_id: str) -> dict:
        batch = self.store.batch(batch_id)
        batch["runs"] = self.store.runs(batch_id)
        counts = defaultdict(int)
        for run in batch["runs"]:
            counts[run["state"]] += 1
        batch["counts"] = dict(counts)
        return batch

    def detail(self, batch_id: str, source_id: str) -> dict:
        batch = self.store.batch(batch_id, manifest=True)
        entry = next((s for s in batch["manifest"] if s["id"] == source_id), None)
        run = next((r for r in self.store.runs(batch_id) if r["source"] == source_id), None)
        if entry is None or run is None:
            raise KeyError("Experiment folder not found in this batch")
        return {"batch_id": batch_id, "request": batch["request"], "source": entry, "run": run,
                "provenance": batch["progress"]}

    def execute(self, batch_id: str) -> None:
        stop = threading.Event()
        # Network loading and a single expensive adapter can outlive a polling
        # interval; heartbeat the lease independently of that work.
        progress = {"stage": "loading", "completed_symbols": 0, "total_symbols": None,
                    "tape_version": 2, "quarantined_rows": 0, "data_errors": [], "engine_sha256": _engine_hash(),
                    "universe_membership": "current snapshot; historical membership/delistings unverified",
                    "corporate_actions": "provider adjustments unverified",
                    "price_basis": "provider OHLC for signals and fills; adjustment status unknown",
                    "security_type": "unverified", "delistings": "no delisting or recovery feed",
                    "distributions": "no dividend or split entitlement ledger"}

        def beat() -> None:
            while not stop.wait(15):
                try:
                    stage = "loading" if progress["stage"] == "loading" else "processing"
                    if not self.store.heartbeat(batch_id, stage, dict(progress)):
                        return
                except Exception:
                    log.exception("Experiments | heartbeat failed")

        heartbeat = threading.Thread(target=beat, daemon=True, name=f"experiment-lease-{batch_id[:8]}")
        heartbeat.start()
        try:
            batch = self.store.batch(batch_id, manifest=True)
            request = BatchRequest.model_validate(batch["request"])
            active = [s for s in batch["manifest"] if s["availability"] == "ready"]
            if not self.store.heartbeat(batch_id, "loading", progress):
                raise Cancelled()
            if not active:
                self.store.finish(batch_id, "completed-with-blockers")
                return
            symbols = self.enumerate_symbols(request.market, request.symbols)
            if not symbols:
                raise ValueError("No history symbols were available; the batch cannot be evaluated")
            progress.update(total_symbols=len(symbols), symbols=symbols,
                            history_market=request.market,
                            currency=get_market(request.market).currency,
                            fee_model="ph-broker-0.25pct-vat-and-dated-stt-v1" if request.market == "ph" and request.txn_cost_pct is None else "fixed-per-side",
                            txn_cost_pct=get_market("us").txn_cost_pct if request.market == "us" and request.txn_cost_pct is None else request.txn_cost_pct)
            summaries = {s["id"]: _empty_summary(s) for s in active}
            failures = set()
            dataset_hash = hashlib.sha256()
            for index, symbol in enumerate(symbols):
                if self.store.cancelled(batch_id):
                    raise Cancelled()
                progress.update(stage="loading", current_symbol=symbol)
                self.store.heartbeat(batch_id, "loading", dict(progress))
                try:
                    raw = self.loader(symbol, request.market, request.end_date)
                    issues = []
                    try:
                        rows = normalize_bars(raw, request.market, request.end_date, issues=issues)
                    finally:
                        if issues:
                            self.store.save_quarantine(batch_id, symbol, issues)
                            progress["quarantined_rows"] += len(issues)
                    if self.store.cancelled(batch_id):
                        raise Cancelled()
                    digest = self.store.dataset(batch_id, symbol, rows)
                    dataset_hash.update(f"{symbol}:{digest}\n".encode())
                    df = frame(rows)
                except Cancelled:
                    raise
                except Exception as exc:
                    log.warning(f"Experiments | history failed for {symbol}: {type(exc).__name__}")
                    progress["data_errors"].append({"symbol": symbol, "reason": _data_error(exc)})
                    progress["completed_symbols"] = index + 1
                    for source in active:
                        summary = summaries[source["id"]]
                        summary["coverage"]["history_failed"] += 1
                        self.store.update_run(batch_id, source["id"], "processing", index + 1,
                                              _present(summary, request))
                    self.store.heartbeat(batch_id, "loading", dict(progress))
                    continue
                progress["stage"] = "processing"
                for source in active:
                    source_id = source["id"]
                    if source_id in failures:
                        continue
                    summary = summaries[source_id]
                    try:
                        if self.store.cancelled(batch_id):
                            raise Cancelled()
                        only = source.get("symbols")
                        if only and symbol not in only:
                            summary["coverage"]["outside_source_universe"] += 1
                        else:
                            result = replay(source["number"], symbol, df, request,
                                            cancel=lambda: self.store.cancelled(batch_id))
                            self.store.save_result(batch_id, source_id, symbol, result)
                            _merge(summary, result)
                        self.store.update_run(batch_id, source_id, "processing", index + 1, _present(summary, request))
                    except Cancelled:
                        raise
                    except Exception:
                        log.exception(f"Experiments | adapter failed: {source_id}")
                        failures.add(source_id)
                        self.store.update_run(batch_id, source_id, "failed", index + 1,
                                              _present(summary, request), "Adapter failed; see server logs")
                progress.update(completed_symbols=index + 1, dataset_sha256=dataset_hash.hexdigest())
                if not self.store.heartbeat(batch_id, "processing", dict(progress)):
                    raise Cancelled()
            for source in active:
                source_id = source["id"]
                if source_id in failures:
                    continue
                summary = _present(summaries[source_id], request, final=True)
                coverage = summary["coverage"]
                state, error = "completed", None
                if (coverage["history_failed"] and not coverage["evaluated_symbols"]
                        and not coverage["outside_source_universe"]):
                    state, error = "failed", "History failed for every symbol; partial results are retained"
                elif not coverage["evaluated_symbols"]:
                    required = source.get("symbols") or []
                    if required:
                        state, error = "blocked", (
                            "Required symbols are not in the current history universe: "
                            + ", ".join(required))
                    else:
                        state, error = "blocked", "No eligible symbol has sufficient in-range history and warm-up for this source"
                self.store.update_run(batch_id, source_id, state, len(symbols), summary, error)
            states = {r["state"] for r in self.store.runs(batch_id)}
            status = "failed" if "failed" in states else "completed-with-blockers" if "blocked" in states else "completed"
            progress.update(stage="finished", dataset_sha256=dataset_hash.hexdigest())
            self.store.heartbeat(batch_id, "processing", progress)
            self.store.finish(batch_id, status, "Some folder runs failed; review their retained results" if status == "failed" else None)
        except Cancelled:
            self.store.finish(batch_id, "cancelled", "Cancelled by user; partial results are retained")
        except Exception as exc:
            log.exception("Experiments | batch failed")
            from core.experiments.history import HistoryUnavailable

            error = str(exc) if isinstance(exc, (HistoryUnavailable,ValueError)) else "Experiment batch failed while preparing history; check history access and server logs"
            self.store.finish(batch_id, "failed", error)
        finally:
            stop.set()
            with self._lock:
                self._threads.pop(batch_id, None)


def _engine_hash() -> str:
    digest = hashlib.sha256()
    root = Path(__file__).resolve().parents[2]
    paths = [*Path(__file__).parent.glob("*.py"), root / "core/market.py", root / "indicators/_math.py"]
    for package in ("numpy", "pandas"):
        digest.update(f"{package}={version(package)}".encode())
    for path in sorted(paths):
        digest.update(str(path.relative_to(root)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _data_error(exc: Exception) -> str:
    from core.experiments.history import HistoryUnavailable

    if isinstance(exc, HistoryUnavailable):
        return "History unavailable; request failed (not an empty history)"
    if isinstance(exc, (ValueError, KeyError, TypeError)):
        return "History has invalid, missing or conflicting OHLCV fields"
    return "History loading failed; check server logs"


def _empty_summary(source: dict) -> dict:
    return {"kind": source["kind"], "tested_label": source.get("tested_label"),
            "family_id": source.get("family_id"), "coverage": {"evaluated_symbols": 0, "history_failed": 0,
            "insufficient_history": 0, "outside_source_universe": 0, "no_history":0,"no_in_range_bars":0},
            "variants": {}, "equity": {}, "exposure": {}, "signal_count": 0, "trade_count": 0, "open_count": 0}


def _merge(summary: dict, result: dict) -> None:
    evaluated = any(v.get("status") == "completed" for v in result["variants"].values())
    reason = result["coverage"].get("reason")
    if reason == "No history":
        bucket = "no_history"
    elif reason == "No bars inside the selected dates":
        bucket = "no_in_range_bars"
    elif evaluated:
        bucket = "evaluated_symbols"
    else:
        bucket = "insufficient_history"
    summary["coverage"][bucket] += 1
    summary["signal_count"] += sum(v.get("funnel", {}).get("raw", 0) for v in result["variants"].values())
    summary["trade_count"] += len(result["trades"])
    summary["open_count"] += len(result["positions"])
    for name, values in result["variants"].items():
        if values["status"] != "completed":
            continue
        total = summary["variants"].setdefault(name, {"status": "completed", "kind": values["kind"],
                                                     "account_model": values["account_model"], "funnel": {}})
        for key, value in values.items():
            if key in {"status", "kind", "account_model", "funnel"}:
                continue
            total[key] = total.get(key, 0) + value if value is not None else None
        for key, count in values["funnel"].items():
            total["funnel"][key] = total["funnel"].get(key, 0) + count
    samples = summary.setdefault("_samples", {})
    for trade in result["trades"]:
        samples.setdefault(trade["variant"], []).append(trade["pnl"])
    contributors = summary.setdefault("_contributors", {})
    for name, stats in result["variants"].items():
        if stats.get("trade_count"):
            symbol = result["symbol"]
            contributors.setdefault(name, {})[symbol] = stats["realized_pnl"]
    for outcome in result["outcomes"]:
        total = summary["variants"][outcome["variant"]].setdefault("outcomes",{}).setdefault(
            str(outcome["horizon"]), {"complete":0,"censored":0,"positive":0,
                                      "sum_return_pct":0.0,"sum_mae_pct":0.0,"sum_mfe_pct":0.0})
        if outcome["complete"]:
            total["complete"] += 1
            total["positive"] += int(outcome["return_pct"] > 0)
            for key in ("return_pct","mae_pct","mfe_pct"):
                total["sum_"+key] += outcome[key]
        else:
            total["censored"] += 1
    # Store per-date DELTAS, then take a cumulative sum when presenting. This
    # carries a symbol's last mark across dates when another symbol trades.
    for name, days in result.get("exposure", {}).items():
        curve = summary["exposure"].setdefault(name, {})
        previous = (0.0, 0.0, 0.0)
        for day, point in sorted(days.items()):
            delta = curve.setdefault(day, [0.0, 0.0, 0.0])
            for j, value in enumerate(point):
                delta[j] += value - previous[j]
            previous = point
    for name, days in result["equity"].items():
        curve = summary["equity"].setdefault(name, {})
        previous = 0.0
        for day, pnl in sorted(days.items()):
            curve[day] = curve.get(day, 0.0) + pnl - previous
            previous = pnl


def _present(summary: dict, request: BatchRequest, *, final: bool = False) -> dict:
    import copy

    view = copy.deepcopy({k: v for k, v in summary.items() if not k.startswith("_")})
    view["currency"] = get_market(request.market).currency
    exposure = view.pop("exposure", {})
    for name, values in view["variants"].items():
        if values.get("outcomes"):
            for outcomes in values.get("outcomes",{}).values():
                count = outcomes["complete"]
                for key in ("return_pct","mae_pct","mfe_pct"):
                    outcomes["mean_"+key] = outcomes["sum_"+key]/count if count else None
                outcomes["positive_fraction"] = outcomes["positive"]/count if count else None
        if values["kind"] != "trades":
            continue
        if values["gross_loss"]:
            values["profit_factor_status"] = "finite"
        elif values["wins"]:
            values["profit_factor_status"] = "profits without losses"
        else:
            values["profit_factor_status"] = "all scratches" if values["trade_count"] else "no closed trades"
        if final and values["trade_count"]:
            series = np.asarray(summary["_samples"][name])
            quantiles = np.quantile(series, [.05, .5, .95]).tolist()
            by_symbol = summary["_contributors"][name]
            best = sorted(by_symbol, key=by_symbol.get, reverse=True)[:3]
            worst = min(by_symbol, key=by_symbol.get)
            values.update(p05_pnl=quantiles[0], median_pnl=quantiles[1], p95_pnl=quantiles[2],
                          best_symbols={s: by_symbol[s] for s in best}, worst_symbol={worst: by_symbol[worst]},
                          realized_without_best_three=values["realized_pnl"] - sum(by_symbol[s] for s in best))
        finite = values["account_model"] == "finite-cash"
        base = request.initial_capital if finite else 0.0
        pnl, peak, max_dd, max_dollars = 0.0, base, 0.0, 0.0
        gross = net = basis = peak_basis = 0.0
        points = []
        for day, delta in sorted(view["equity"].get(name, {}).items()):
            pnl += delta
            change = exposure.get(name, {}).get(day, (0, 0, 0))
            gross += change[0]
            net += change[1]
            basis += change[2]
            peak_basis = max(peak_basis, basis)
            equity = base + pnl
            peak = max(peak, equity)
            max_dd = max(max_dd, 100 * (peak - equity) / peak if peak > 0 else 0)
            drawdown = peak - equity
            max_dollars = max(max_dollars, drawdown)
            points.append({"date": day, "equity": equity if finite else None, "pnl": pnl,
                           "drawdown": drawdown, "gross_exposure": gross, "net_exposure": net})
        view["equity"][name] = points
        values.update(net_pnl=values["realized_pnl"] + values["unrealized_pnl"],
                      win_rate=values["wins"] / values["trade_count"] if values["trade_count"] else None,
                      profit_factor=values["gross_profit"] / values["gross_loss"] if values["gross_loss"] else None,
                      expectancy=values["realized_pnl"] / values["trade_count"] if values["trade_count"] else None,
                      average_r=values["r_total"] / values["r_count"] if values["r_count"] else None,
                      max_drawdown_pct=max_dd if finite else None, max_drawdown_cash=max_dollars,
                      peak_committed_basis=peak_basis, gross_exposure=gross, net_exposure=net)
    return view


_service = None
_service_lock = threading.Lock()


def get_service() -> ExperimentService:
    global _service
    with _service_lock:
        if _service is None:
            from config import settings

            directory = Path(settings.experiments_store_dir)
            if not directory.is_absolute():
                directory = Path(__file__).resolve().parents[2] / directory
            _service = ExperimentService(ExperimentStore(directory))
        return _service
