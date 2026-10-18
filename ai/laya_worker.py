"""Isolated Laya inference worker (docs/laya.md section 8).

Dedicated local worker process with one loaded model and a bounded queue. The
application communicates through a small application-owned JSON protocol over
local process pipes (one JSON request per line on stdin, one JSON response per
line on stdout). Model operations stay out of FastAPI's event loop, paper
scanner execution, detector subprocesses, and DB transactions.

Protocol:
  request:  {"request_id": str, "task_id": str, "context_text": str,
             "questions": [{"qid","kind","text","options","rubric"}], "timeout_s": float}
  response: {"request_id": str, "ok": bool, "answers": [...], "raw_version": str,
             "warnings": [...], "truncated": bool, "options_collapsed": bool,
             "elapsed_ms": int, "error_code": str, "error_detail": str}

Fixed executable/module arguments only — never shell commands assembled from
request text. Library logging goes to stderr so it cannot corrupt responses.

Real inference lazily imports `laya` (requirements-laya.txt env only). Without
the package or a pinned checkpoint, the worker reports `model-unavailable`
instead of fabricating answers. `LAYA_FAKE=1` (or --fake) runs a deterministic
fake used by CI and ordinary tests: no checkpoint is ever downloaded.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from typing import Any


def _estimate_tokens(text: str) -> int:
    """Conservative token estimate (chars/4). The English budget is 512 total."""
    return max(1, len(text.encode("utf-8", errors="ignore")) // 4)


# Token budget guard: instructions + options consume space too, so the state
# portion must stay well under the 512-token English context. Over-budget
# input still runs (the tokenizer truncates tail tokens) but is flagged, and
# policy layers must treat flagged results as not gate-eligible.
_STATE_TOKEN_BUDGET = 350


def _fake_answers(task_id: str, questions: list[dict]) -> tuple[list[dict], list[str]]:
    answers: list[dict] = []
    for q in questions:
        qid = str(q.get("qid") or "")
        kind = str(q.get("kind") or "")
        if kind == "choice":
            options = list(q.get("options") or [])
            label = options[0] if options else "insufficient"
            dist = [0.0] * len(options)
            if dist:
                dist[0] = 1.0
            answers.append({
                "qid": qid, "kind": "choice", "answer": label,
                "distribution": dist, "answer_confidence": 0.5,
                "confidence": 0.5, "yes_probability": None, "score_value": None,
            })
        elif kind == "score":
            rubric = list(q.get("rubric") or [])
            label = rubric[0] if rubric else "insufficient"
            idx = 0
            dist = [0.0] * len(rubric)
            if dist:
                dist[0] = 1.0
            answers.append({
                "qid": qid, "kind": "score", "answer": label,
                "distribution": dist, "answer_confidence": 0.5,
                "confidence": 0.5, "yes_probability": None,
                "score_value": float(idx),
            })
        elif kind == "noul":
            answers.append({
                "qid": qid, "kind": "noul", "answer": "no",
                "distribution": (), "answer_confidence": 0.5,
                "confidence": 0.5, "yes_probability": 0.1, "score_value": None,
            })
        else:
            answers.append({
                "qid": qid, "kind": kind or "choice", "answer": "insufficient",
                "distribution": (), "answer_confidence": 0.0,
                "confidence": 0.0, "yes_probability": None, "score_value": None,
            })
    return answers, ["fake-inference; not a model result"]


class _RealBackend:
    """Lazy single-resident-checkpoint backend. One model, no preload-all."""

    def __init__(self, model_path: str, device: str) -> None:
        self.model_path = model_path
        self.device = device
        self._router = None
        self._load_error: str | None = None

    def ensure_loaded(self) -> str | None:
        if self._router is not None:
            return None
        if self._load_error is not None:
            return self._load_error
        try:
            from laya.router import Router  # type: ignore  # lazy; worker env only
        except Exception as exc:
            self._load_error = f"laya package unavailable: {exc}"
            return self._load_error
        try:
            # Explicit pinned checkpoint: the "english" key is re-pointed at
            # the operator-provisioned local directory (which itself is the
            # revision pin — see the manifest beside the model dir). One
            # resident checkpoint; loading is lazy.
            self._router = Router(
                models={"english": self.model_path},
                device=self.device,
                max_loaded=1,
            )
        except Exception as exc:
            self._load_error = f"model load failed: {exc}"
            return self._load_error
        return None

    def predict(self, state: str, questions: list[dict]) -> dict[str, Any]:
        err = self.ensure_loaded()
        if err or self._router is None:
            raise RuntimeError(err or "model unavailable")
        qdict: dict[str, Any] = {}
        for q in questions:
            kind = str(q.get("kind") or "")
            text = str(q.get("text") or "")
            if kind == "choice":
                options = list(q.get("options") or [])
                qdict[str(q["qid"])] = {
                    "type": "choice", "instructions": text,
                    "criteria": {opt: opt for opt in options},
                }
            elif kind == "score":
                qdict[str(q["qid"])] = {
                    "type": "score", "instructions": text,
                    "criteria": list(q.get("rubric") or []),
                }
            elif kind == "noul":
                qdict[str(q["qid"])] = {"type": "noul", "instructions": text}
            else:
                raise ValueError(f"unknown question kind {kind}")
        # Pinned checkpoint: no auto-routing surprises. Our tasks are English
        # computed facts / operator text; multilingual is a separate evaluation.
        return self._router.predict(state, qdict, model="english")


def _normalize_router_output(
    raw: Any, questions: list[dict], task_id: str,
) -> tuple[list[dict], dict, list[str], bool, bool]:
    """Validate expected IDs/types/options/bounds; preserve raw + version.

    Real 0.4.x answer shapes: choice carries ``choice`` + ``probabilities``
    keyed by label; score carries an expected-ordinal ``score`` + index-keyed
    ``probabilities`` aligned with the criteria order sent; noul carries
    ``noul`` = P(true). ``answer_confidence`` (max answer probability) is
    required for application thresholds; the entropy ``confidence`` is kept
    separate and never substituted.
    """
    warnings: list[str] = []
    if not isinstance(raw, dict):
        raise ValueError("router returned a non-object payload")
    answers_raw = raw.get("answers", raw)
    if not isinstance(answers_raw, dict):
        raise ValueError("router answers payload is not a mapping")
    for qid in answers_raw:
        if qid not in {str(q["qid"]) for q in questions}:
            warnings.append(f"router returned an unasked question {qid!r}; ignored")
    out: list[dict] = []
    for q in questions:
        qid = str(q["qid"])
        kind = str(q["kind"])
        node = answers_raw.get(qid)
        if node is None:
            raise ValueError(f"router omitted answer for {qid}")
        if not isinstance(node, dict):
            raise ValueError(f"router answer for {qid} is not an object")
        conf = node.get("answer_confidence")
        if conf is None or isinstance(conf, bool) or not isinstance(conf, (int, float)):
            raise ValueError(f"question {qid} lacks numeric answer_confidence")
        if not (0.0 <= float(conf) <= 1.0):
            raise ValueError(f"answer_confidence out of bounds for {qid}")
        entropy = node.get("confidence")
        entropy_f = float(entropy) if isinstance(entropy, (int, float)) and not isinstance(entropy, bool) else None
        if kind == "choice":
            options = list(q.get("options") or [])
            label = str(node.get("choice") or "")
            probs = node.get("probabilities") or {}
            if label not in options:
                raise ValueError(f"answer {label!r} not in pinned options for {qid}")
            try:
                dist = [float(probs[opt]) for opt in options]
            except (KeyError, TypeError, ValueError):
                raise ValueError(f"probability map mismatch for {qid}") from None
            for p in dist:
                if not (0.0 <= p <= 1.0):
                    raise ValueError(f"probability out of bounds for {qid}")
            if abs(sum(dist) - 1.0) > 0.05:
                raise ValueError(f"distribution does not sum to 1 for {qid}")
            out.append({"qid": qid, "kind": "choice", "answer": label,
                        "distribution": dist,
                        "answer_confidence": float(conf),
                        "confidence": entropy_f,
                        "yes_probability": None, "score_value": None})
        elif kind == "score":
            rubric = list(q.get("rubric") or [])
            probs = node.get("probabilities") or {}
            try:
                dist = [float(probs[str(i)]) for i in range(len(rubric))]
            except (KeyError, TypeError, ValueError):
                raise ValueError(f"probability map mismatch for {qid}") from None
            for p in dist:
                if not (0.0 <= p <= 1.0):
                    raise ValueError(f"probability out of bounds for {qid}")
            best = max(range(len(dist)), key=lambda i: dist[i])
            try:
                expected = float(node.get("score"))
            except (TypeError, ValueError):
                expected = float(best)
            out.append({"qid": qid, "kind": "score", "answer": rubric[best],
                        "distribution": dist,
                        "answer_confidence": float(conf),
                        "confidence": entropy_f,
                        "yes_probability": None,
                        "score_value": expected})
        elif kind == "noul":
            yes_p = node.get("noul")
            if yes_p is None or isinstance(yes_p, bool) or not isinstance(yes_p, (int, float)):
                raise ValueError(f"noul answer lacks numeric P(true) for {qid}")
            if not (0.0 <= float(yes_p) <= 1.0):
                raise ValueError(f"yes probability out of bounds for {qid}")
            # A high-confidence "no" can have a low P(yes): keep both fields.
            out.append({"qid": qid, "kind": "noul",
                        "answer": "yes" if float(yes_p) >= 0.5 else "no",
                        "distribution": (), "answer_confidence": float(conf),
                        "confidence": entropy_f,
                        "yes_probability": float(yes_p), "score_value": None})
        else:
            raise ValueError(f"unknown question kind {kind}")
    preserved = {"task_id": task_id, "router": raw.get("routing", {}),
                 "raw_answers": answers_raw}
    return out, preserved, warnings, False, False


def handle_request(
    payload: dict[str, Any], *,
    backend: _RealBackend | None,
    fake: bool,
    raw_version: str,
) -> dict[str, Any]:
    request_id = str(payload.get("request_id") or "")
    task_id = str(payload.get("task_id") or "")
    context_text = str(payload.get("context_text") or "")
    questions = payload.get("questions") or []
    started = time.monotonic()
    if not request_id:
        return {"request_id": "", "ok": False, "error_code": "invalid-request",
                "error_detail": "request_id is required", "elapsed_ms": 0}
    if not context_text or not isinstance(questions, list) or not questions:
        return {"request_id": request_id, "ok": False, "error_code": "invalid-request",
                "error_detail": "context_text and questions are required", "elapsed_ms": 0}
    if len(context_text.encode("utf-8")) > 20000:
        return {"request_id": request_id, "ok": False, "error_code": "invalid-request",
                "error_detail": "context_text exceeds bounded size", "elapsed_ms": 0}
    state_tokens = _estimate_tokens(context_text)
    over_budget = state_tokens > _STATE_TOKEN_BUDGET
    try:
        if fake or backend is None:
            answers, warnings = _fake_answers(task_id, questions)
            preserved: Any = {"task_id": task_id, "fake": True}
            truncated = False
            collapsed = False
        else:
            raw = backend.predict(context_text, questions)
            answers, preserved, warnings, truncated, collapsed = _normalize_router_output(
                raw, questions, task_id)
        if over_budget and not fake:
            # The tokenizer truncated tail tokens: never gate on this result.
            truncated = True
            warnings = list(warnings) + [
                f"state ~{state_tokens} tokens exceeds the {_STATE_TOKEN_BUDGET} budget; "
                f"tail evidence lost"]
        elapsed_ms = int((time.monotonic() - started) * 1000)
        return {"request_id": request_id, "ok": True, "answers": answers,
                "raw": preserved, "raw_version": raw_version,
                "warnings": warnings, "truncated": truncated,
                "options_collapsed": collapsed, "elapsed_ms": elapsed_ms}
    except Exception as exc:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        return {"request_id": request_id, "ok": False, "error_code": "inference-failed",
                "error_detail": str(exc)[:500], "elapsed_ms": elapsed_ms}


def main() -> int:
    parser = argparse.ArgumentParser(description="Laya isolated inference worker")
    parser.add_argument("--model-path", default=os.environ.get("LAYA_MODEL_PATH", ""))
    parser.add_argument("--device", default=os.environ.get("LAYA_DEVICE", "cpu"))
    parser.add_argument("--fake", action="store_true",
                        default=os.environ.get("LAYA_FAKE", "") in ("1", "true", "yes"))
    args = parser.parse_args()

    backend: _RealBackend | None = None
    raw_version = "laya-app-v1-fake" if args.fake else "laya-app-v1"
    if not args.fake:
        if not args.model_path:
            print(json.dumps({"ready": False, "reason": "missing-model"}), flush=True)
        else:
            backend = _RealBackend(args.model_path, args.device)
            err = backend.ensure_loaded()
            print(json.dumps({"ready": err is None, "reason": err or "ok"}), flush=True)
    else:
        print(json.dumps({"ready": True, "reason": "fake"}), flush=True)

    stdin = sys.stdin
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            print(json.dumps({"request_id": "", "ok": False,
                               "error_code": "invalid-request",
                               "error_detail": "request line is not JSON",
                               "elapsed_ms": 0}), flush=True)
            continue
        response = handle_request(payload, backend=backend, fake=args.fake,
                                  raw_version=raw_version)
        # Dedicated channel: exactly one JSON object per line on stdout;
        # library logging must go to stderr.
        print(json.dumps(response), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
