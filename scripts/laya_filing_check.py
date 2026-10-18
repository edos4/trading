"""Laya filing check: event-review a symbol's recent EDGAR filings (P3 demo).

Usage:
    .venv/bin/python scripts/laya_filing_check.py --symbol RENX
    .venv/bin/python scripts/laya_filing_check.py --symbol AAPL --lookback-days 60
    LAYA_FAKE=1 .venv/bin/python scripts/laya_filing_check.py --symbol RENX  # adapter only

Lists offering/adverse-relevant filings accepted in the lookback window, runs
each through the real event-review task, and prints the policy verdict. Proves
the profitable loop on real data: real language in, trade-relevant verdict out.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


def main() -> int:
    parser = argparse.ArgumentParser(description="Laya EDGAR filing check")
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--lookback-days", type=int, default=30)
    parser.add_argument("--fake", action="store_true")
    parser.add_argument("--model-path", default="/home/r00t/models/laya-en")
    parser.add_argument("--device", default="cpu")
    args = parser.parse_args()

    import os

    fake = args.fake or os.environ.get("LAYA_FAKE", "") in ("1", "true", "yes")

    from data.laya_filings import excerpt_offering, fetch_filing_text, relevant_filings

    now = datetime.now(timezone.utc)
    filings = relevant_filings(args.symbol.upper(), cutoff=now,
                               lookback_days=args.lookback_days)
    print(f"symbol={args.symbol.upper()} relevant filings in last "
          f"{args.lookback_days}d: {len(filings)}")
    for f in filings:
        print(f"  {f['filed']} {f['form']:8} {f['description'][:60]} "
              f"(accepted {f['accepted_at'][:16]})")

    backend = None
    if not fake:
        from ai.laya_worker import _RealBackend

        backend = _RealBackend(args.model_path, args.device)
        err = backend.ensure_loaded()
        if err:
            print(json.dumps({"ok": False, "stage": "load", "error": err[:200]}))
            return 3

    from ai.laya_worker import handle_request
    from core.laya_contracts import TASK_PRESETS

    qs = TASK_PRESETS["event-review-v1"]["questions"]
    qwire = [{"qid": q.qid, "kind": q.kind, "text": q.text,
              "options": list(q.options), "rubric": list(q.rubric)} for q in qs]
    for f in filings:
        text = fetch_filing_text(f["doc_url"])
        if not text:
            print(f"  [{f['form']}] document fetch failed; no evidence")
            continue
        excerpt, shortened = excerpt_offering(text)
        digest = hashlib.sha256(text.encode()).hexdigest()[:16]
        print(f"  [{f['form']}] doc chars={len(text)} excerpt={len(excerpt)} "
              f"shortened={shortened} sha={digest}")
        state = (f"Issuer: {args.symbol.upper()}. Filing: {f['form']} filed "
                 f"{f['filed']} ({f['description'] or 'no description'}).\n"
                 f"Filing text excerpt:\n{excerpt}\n")
        resp = handle_request(
            {"request_id": f"filing-{f['accession']}", "task_id": "event-review-v1",
             "context_text": state, "questions": qwire},
            backend=backend, fake=fake, raw_version="laya-filing-v1")
        if not resp.get("ok"):
            print("   inference FAILED:", resp.get("error_detail", "")[:150])
            continue
        for a in resp["answers"]:
            extra = ""
            if a["kind"] == "noul":
                extra = f" P(yes)={a['yes_probability']}"
            print(f"   {a['qid']}: {a['answer']} "
                  f"(answer_confidence={a['answer_confidence']}){extra}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
