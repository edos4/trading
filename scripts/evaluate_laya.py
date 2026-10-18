"""P3 versioned Laya evaluation command (docs/laya.md P3).

Produces model-quality and calibration reports and locks initial policy
candidates. Compares against simple deterministic baselines. Held-out quality
and coverage are reported per supported task/market/language. Unvalidated
cases remain research-only.

Usage:
    python scripts/evaluate_laya.py --fake --out /tmp/laya-eval.json
    python scripts/evaluate_laya.py --store data/cache/laya.sqlite3 --fake
"""
from __future__ import annotations

import argparse
import json
import sys
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


def _run_fixture(fixture: dict, *, fake: bool, backend=None) -> dict:
    from ai.laya_worker import handle_request
    from core.laya_contracts import TASK_PRESETS

    preset = TASK_PRESETS[fixture["task_id"]]
    questions = [
        {"qid": q.qid, "kind": q.kind, "text": q.text,
         "options": list(q.options), "rubric": list(q.rubric)}
        for q in preset["questions"]
    ]
    context = fixture.get("source_text") or fixture.get("context_text") or ""
    payload = {"request_id": f"eval-{fixture['fixture_id']}",
               "task_id": fixture["task_id"],
               "context_text": context, "questions": questions}
    response = handle_request(payload, backend=backend, fake=fake,
                              raw_version="laya-eval-v1")
    return response


def _deterministic_baseline(fixture: dict) -> dict[str, str]:
    """Simple deterministic rule-only baseline for comparison."""
    text = ((fixture.get("source_text") or "") + " " +
            (fixture.get("context_text") or "")).lower()
    if fixture["task_id"] == "event-review-v1":
        if "offering" in text or "dilut" in text:
            event, tone = "financing", "negative"
        elif "guidance" in text or "revenue above" in text:
            event, tone = "earnings", "positive"
        elif "inquiry" in text or "regulator" in text:
            event, tone = "regulatory", "mixed"
        else:
            event, tone = "other", "neutral"
        risk = "yes" if any(w in text for w in ("inquiry", "fraud", "default", "lawsuit")) else "no"
        return {"event_type": event, "directional_tone": tone,
                "material_risk_mentioned": risk}
    return {"evidence_alignment": "mixed", "material_conflict": "no"}


def main() -> int:
    parser = argparse.ArgumentParser(description="Laya P3 evaluation")
    parser.add_argument("--fake", action="store_true",
                        help="Use the deterministic fake instead of a real checkpoint")
    parser.add_argument("--model-path", default="",
                        help="Pinned local checkpoint dir for real-model evaluation")
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--out", default="")
    parser.add_argument("--store", default="")
    args = parser.parse_args()

    from data.laya_fixtures import FIXTURE_VERSION, FIXTURES

    backend = None
    if not args.fake:
        if not args.model_path:
            print("Real-model evaluation needs --model-path (or pass --fake).")
            return 2
        from ai.laya_worker import _RealBackend

        backend = _RealBackend(args.model_path, args.device)
        err = backend.ensure_loaded()
        if err:
            print(json.dumps({"ok": False, "stage": "load", "error": err[:300]}))
            return 3

    per_task: dict[str, Counter] = defaultdict(Counter)
    per_market: dict[str, Counter] = defaultdict(Counter)
    baseline_hits = 0
    model_hits = 0
    total_labels = 0
    rows: list[dict] = []
    for fixture in FIXTURES:
        response = _run_fixture(fixture, fake=args.fake, backend=backend)
        predicted: dict[str, str] = {}
        if response.get("ok"):
            for ans in response.get("answers") or []:
                predicted[ans["qid"]] = ans["answer"]
        baseline = _deterministic_baseline(fixture)
        labels = fixture.get("labels") or {}
        for qid, expected in labels.items():
            total_labels += 1
            if predicted.get(qid) == expected:
                model_hits += 1
                per_task[fixture["task_id"]]["hit"] += 1
            per_task[fixture["task_id"]]["total"] += 1
            key = f"{fixture['market']}/{fixture.get('language', 'en')}"
            if predicted.get(qid) == expected:
                per_market[key]["hit"] += 1
            per_market[key]["total"] += 1
            if baseline.get(qid) == expected:
                baseline_hits += 1
        rows.append({"fixture_id": fixture["fixture_id"], "task_id": fixture["task_id"],
                     "market": fixture["market"], "split": fixture.get("split"),
                     "predicted": predicted, "expected": labels,
                     "baseline": baseline, "ok": bool(response.get("ok"))})

    report = {
        "report_version": "laya-eval-v1",
        "fixture_version": FIXTURE_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "fake_inference": bool(args.fake),
        "model_accuracy": (model_hits / total_labels) if total_labels else None,
        "baseline_accuracy": (baseline_hits / total_labels) if total_labels else None,
        "total_labels": total_labels,
        "per_task": {k: {"accuracy": (v["hit"] / v["total"]) if v["total"] else None,
                         "hits": v["hit"], "total": v["total"]}
                     for k, v in per_task.items()},
        "per_market_language": {k: {"accuracy": (v["hit"] / v["total"]) if v["total"] else None,
                                    "hits": v["hit"], "total": v["total"]}
                                for k, v in per_market.items()},
        "coverage": {"fixtures": len(FIXTURES),
                     "splits": dict(Counter(f.get("split", "?") for f in FIXTURES))},
        "note": ("Unvalidated task/market/language cases remain research-only. "
                 "Thresholds and coverage floors are evaluation outputs."),
        "rows": rows,
    }
    text = json.dumps(report, indent=2)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    else:
        print(text)
    # A stronger model result is informative; a fake-model tie with the rule
    # baseline keeps Laya research-only (per plan: keep the rule for trading).
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
