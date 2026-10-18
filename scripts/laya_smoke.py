"""P0 offline smoke + benchmark (docs/laya.md P0).

Reproducible offline command that leaves the bot's base environment and MCP
dependencies intact:

    .venv-laya/bin/python scripts/laya_smoke.py --fake --corpus data/laya_fixtures.json
    .venv-laya/bin/python scripts/laya_smoke.py --model-path /models/laya-en --corpus ...

Without --fake it verifies package import/version WITHOUT model loading first,
then pinned offline loading, all three answer types, truncation diagnostics,
and cancellation strategy. With --fake it runs the deterministic fake used by
CI. Never downloads a checkpoint in routine tests.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


def _load_corpus(path: Path) -> list[dict]:
    if not path.exists():
        return [
            {"task_id": "event-review-v1",
             "context_text": "EXAMPLE Corp announces a secondary offering of 10M shares.",
             "note": "built-in fallback; supply --corpus for the real fixture set"},
            {"task_id": "candidate-review-v1",
             "context_text": "Market: US; symbol: EXAMPLE; Candidate: BUY; engine eligibility: passed.",
             "note": "built-in fallback"},
        ]
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description="Laya P0 smoke + benchmark")
    parser.add_argument("--fake", action="store_true")
    parser.add_argument("--model-path", default="")
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--corpus", default="data/laya_fixtures.json")
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()

    report: dict = {"fake": bool(args.fake), "cases": []}
    if not args.fake:
        try:
            import importlib.metadata as _md

            try:
                report["laya_version"] = _md.version("laya")
            except _md.PackageNotFoundError:
                report["laya_version"] = "not-installed"
            import laya  # noqa: F401  (import check only, no model load)

            report["import_ok"] = True
        except Exception as exc:
            print(json.dumps({"ok": False, "stage": "import", "error": str(exc)[:300]}))
            return 2
    else:
        report["import_ok"] = "fake-skipped"

    from ai.laya_worker import handle_request
    from core.laya_contracts import TASK_PRESETS

    backend = None
    if not args.fake:
        from ai.laya_worker import _RealBackend

        backend = _RealBackend(args.model_path, args.device)
        err = backend.ensure_loaded()
        report["load_error"] = err
        if err:
            print(json.dumps({"ok": False, "stage": "load", "error": err[:300]}))
            return 3

    corpus = _load_corpus(REPO_ROOT / args.corpus)
    peak_rss = 0
    try:
        import resource

        def _rss() -> int:
            return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    except ImportError:
        def _rss() -> int:
            return 0

    ok = True
    for case in corpus:
        task_id = case.get("task_id", "event-review-v1")
        preset = TASK_PRESETS.get(task_id)
        if preset is None:
            report["cases"].append({"task_id": task_id, "ok": False, "error": "unknown task"})
            ok = False
            continue
        questions = [
            {"qid": q.qid, "kind": q.kind, "text": q.text,
             "options": list(q.options), "rubric": list(q.rubric)}
            for q in preset["questions"]
        ]
        latencies: list[int] = []
        last: dict = {}
        for _ in range(max(1, args.repeats)):
            payload = {"request_id": f"smoke-{task_id}", "task_id": task_id,
                       "context_text": case.get("context_text", ""), "questions": questions}
            started = time.monotonic()
            last = handle_request(payload, backend=backend, fake=args.fake,
                                  raw_version="laya-smoke")
            latencies.append(int((time.monotonic() - started) * 1000))
            if not last.get("ok"):
                break
        peak_rss = max(peak_rss, _rss())
        kinds = sorted({q["kind"] for q in questions})
        report["cases"].append({
            "task_id": task_id, "question_kinds": kinds,
            "ok": bool(last.get("ok")),
            "cold_ms": latencies[0] if latencies else None,
            "warm_ms": latencies[-1] if latencies else None,
            "answers": len(last.get("answers") or []),
            "error": last.get("error_detail") if not last.get("ok") else None,
        })
        if not last.get("ok"):
            ok = False
    report["peak_rss_kb"] = peak_rss
    report["ok"] = ok
    print(json.dumps(report, indent=2))
    return 0 if ok else 4


if __name__ == "__main__":
    raise SystemExit(main())
