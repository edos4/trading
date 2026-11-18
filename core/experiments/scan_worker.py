"""One symbol of an experiment batch, safe to run in a spawned process.

The module stays free of numpy until a job actually replays. Spawned workers
set a single-thread BLAS limit before that import so a pool does not start
one OpenMP team per core on every process.
"""
from __future__ import annotations

import multiprocessing as mp
import os
import traceback

_BLAS = (
    "OMP_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "MKL_NUM_THREADS",
    "NUMEXPR_NUM_THREADS",
    "VECLIB_MAXIMUM_THREADS",
)

if mp.parent_process() is not None:
    for _name in _BLAS:
        os.environ[_name] = "1"

_stores: dict[str, object] = {}


def prepare_history(loader, symbol: str, market: str, end_date) -> dict:
    """Normalize one symbol. ``error`` is set instead of raising."""
    from core.experiments.history import normalize_bars
    from core.experiments.replay import Cancelled

    issues: list[dict] = []
    try:
        raw = loader(symbol, market, end_date)
        try:
            rows = normalize_bars(raw, market, end_date, issues=issues)
        except Cancelled:
            raise
        except Exception as exc:
            return {"rows": None, "issues": issues, "error": exc}
        return {"rows": rows, "issues": issues, "error": None}
    except Cancelled:
        raise
    except Exception as exc:
        return {"rows": None, "issues": issues, "error": exc}


def _cancelled(root: str, batch_id: str) -> bool:
    if not root or not batch_id:
        return False
    store = _stores.get(root)
    if store is None:
        from pathlib import Path

        from core.experiments.store import ExperimentStore

        store = ExperimentStore(Path(root))
        _stores[root] = store
    return store.cancelled(batch_id)


def scan_symbol(job: dict) -> dict:
    """Replay every ready source for one symbol.

    When ``load`` is true the worker reads history itself. Otherwise the
    parent passes normalized ``rows`` and keeps them for the frozen dataset.
    """
    from core.experiments.contracts import BatchRequest
    from core.experiments.history import frame
    from core.experiments.replay import Cancelled, DailyTape, replay

    symbol = job["symbol"]
    request = BatchRequest.model_validate(job["request"])
    root = job.get("store_root") or ""
    batch_id = job.get("batch_id") or ""
    loaded_here = bool(job.get("load"))
    outcome = {
        "issues": list(job.get("issues") or []),
        "rows": None,
        "error_type": None,
        "error_text": None,
        "cancelled": False,
        "results": [],
    }
    if loaded_here:
        from core.experiments.history import raw_symbol

        prepared = prepare_history(raw_symbol, symbol, request.market, request.end_date)
        outcome["issues"] = prepared["issues"]
        if prepared["error"] is not None:
            exc = prepared["error"]
            outcome["error_type"] = type(exc).__name__
            outcome["error_text"] = str(exc)
            return outcome
        rows = prepared["rows"]
        outcome["rows"] = rows
    else:
        rows = job.get("rows")
    if rows is None:
        outcome["error_type"] = "ValueError"
        outcome["error_text"] = "History rows were not provided"
        return outcome
    if _cancelled(root, batch_id):
        outcome["cancelled"] = True
        return outcome
    try:
        df = frame(rows)
        tape = DailyTape.prepare(df, request.market)
    except Cancelled:
        raise
    except Exception as exc:
        outcome["error_type"] = type(exc).__name__
        outcome["error_text"] = str(exc)
        if not loaded_here:
            outcome["rows"] = None
        return outcome
    results = []
    for source in job["sources"]:
        if _cancelled(root, batch_id):
            outcome["cancelled"] = True
            outcome["results"] = results
            return outcome
        source_id = source["id"]
        only = source.get("symbols")
        if only and symbol not in only:
            results.append({"id": source_id, "skip": True})
            continue
        try:
            result = replay(
                source["number"], symbol, df, request, tape=tape,
                cancel=lambda: _cancelled(root, batch_id),
            )
        except Cancelled:
            outcome["cancelled"] = True
            outcome["results"] = results
            return outcome
        except Exception:
            results.append({"id": source_id, "fail": True, "trace": traceback.format_exc()})
            continue
        results.append({"id": source_id, "result": result})
    outcome["results"] = results
    if not loaded_here:
        outcome["rows"] = None
    return outcome
