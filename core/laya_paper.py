"""Live paper assessor: bridge scanner candidates to the Laya service (P5/P6).

Filter mode calls this synchronously before the paper entry attempt and waits
for a valid assessment under the pinned policy (bounded by a defined
deadline). Shadow mode fires the same work in the background and returns
``allow`` immediately, so fills and latency are unchanged.

Inference timeout, malformed answers, stale context, and a missing model are
abstentions in filter mode (the entry is skipped) and no-ops in shadow mode.
Neither mode ever touches exits, stops, targets, or sizing.
"""
from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Callable

#: Default paper policy: event-risk veto over EDGAR filing text. Candidate
#: snapshots carry no language the model can judge (measured: flat
#: "insufficient" / chance-level inverted answers), so they are not the default.
DEFAULT_POLICY_ID = "laya-event-v1-default"

#: Cap on filings classified per candidate: newest first, break on first veto.
#: Bounds filter latency (each assessment is seconds on CPU).
MAX_FILINGS_PER_CANDIDATE = 3
FILING_LOOKBACK_DAYS = 30

_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="laya-shadow")
_executor_lock = threading.Lock()


def laya_readiness() -> tuple[bool, str]:
    """Whether a paper book may start with Laya shadow/filter enabled."""
    try:
        from core.laya_service import get_service

        status = get_service().status()
    except Exception as exc:  # noqa: BLE001 - readiness, not inference
        return False, f"Laya service unavailable: {exc}"
    if status.get("state") == "ready":
        model = status.get("model") or {}
        return True, f"ready ({model.get('model_id', 'pinned')})"
    return False, str(status.get("reason") or status.get("state") or "unavailable")


def make_assessor(
    *,
    market: str,
    policy_id: str = "",
    mode: str = "filter",
    timeout_s: float | None = None,
    book_id: str | None = None,
    service=None,
) -> Callable[[dict[str, Any]], dict[str, Any]]:
    """Build the ``laya_assess`` callable a paper ``MarketScanner`` needs."""
    if mode not in ("shadow", "filter"):
        raise ValueError(f"Unsupported Laya paper mode: {mode}")
    policy = (policy_id or "").strip() or DEFAULT_POLICY_ID
    book = book_id or f"paper-{market}"
    if timeout_s is None:
        try:
            from config import settings

            timeout_s = float(getattr(settings, "laya_timeout_seconds", 30.0) or 30.0)
        except Exception:
            timeout_s = 30.0
    deadline = max(1.0, float(timeout_s))

    def _assess(snapshot: dict[str, Any]) -> dict[str, Any]:
        if mode == "shadow":
            _submit_shadow(snapshot, market=market, policy=policy,
                           book=book, service=service)
            return {"outcome": "allow",
                    "reasons": ["shadow: assessment running in background; fills unchanged"],
                    "assessment_id": None, "mode": mode}
        return _assess_filter(snapshot, market=market, policy=policy,                              book=book, deadline=deadline, service=service)

    return _assess


def _cutoff(snapshot: dict[str, Any]):
    """Decision cutoff: the completed bar (deterministic per-bar identity)."""
    bar_ts = snapshot.get("bar_ts")
    try:
        bar_close_at = datetime.fromisoformat(bar_ts) if bar_ts else None
    except (TypeError, ValueError):
        bar_close_at = None
    now = datetime.now(timezone.utc)
    if bar_close_at is None or bar_close_at.tzinfo is None:
        bar_close_at = now
    elif bar_close_at > now:
        bar_close_at = now
    return bar_close_at


def _policy_task(service, policy_id: str) -> str | None:
    """Task family of the pinned policy; None when the policy is unknown."""
    try:
        svc = _service_or_default(service)
        prow = svc.store.get_policy(policy_id)
        if prow is None:
            return None
        from core.laya_contracts import LayaPolicy

        return LayaPolicy.model_validate(json.loads(prow["payload_json"])).task_id
    except Exception:
        return None


def _snapshot_candidate(snapshot: dict[str, Any], market: str):
    from core.laya_contracts import LayaCandidate, LayaRuntime

    action = str(snapshot.get("action") or "").upper()
    if action not in ("BUY", "SELL"):
        raise ValueError(f"Candidate action {action!r} is not tradeable")
    try:
        confidence = max(0.0, min(1.0, float(snapshot.get("confidence") or 0.0)))
    except (TypeError, ValueError):
        confidence = 0.0

    def _num(key: str) -> float | None:
        try:
            value = snapshot.get(key)
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    entry, stop, target = _num("price"), _num("stop"), _num("target")
    reward_risk = None
    if entry and stop and target:
        risk = abs(entry - stop)
        reward_risk = abs(target - entry) / risk if risk > 0 else None
    return LayaCandidate(
        direction=action,  # type: ignore[typeddict-item]
        detector=str(snapshot.get("pattern") or "unknown"),
        pattern_version_id=snapshot.get("pattern_version_id"),
        detector_confidence=confidence,
        entry=entry, stop=stop, target=target,
        reward_risk=reward_risk,
        engine_eligibility="passed",
    ), LayaRuntime(package_version="laya-0.4.7",
                   model_id="pinned", backend="cpu")


def _build_input(snapshot: dict[str, Any], market: str,
                 assessment_id: str):
    from core.laya_context import build_candidate_input

    candidate, runtime = _snapshot_candidate(snapshot, market)
    bar_ts = snapshot.get("bar_ts")
    try:
        bar_close_at = datetime.fromisoformat(bar_ts) if bar_ts else None
    except (TypeError, ValueError):
        bar_close_at = None
    now = datetime.now(timezone.utc)
    if bar_close_at is None or bar_close_at.tzinfo is None:
        bar_close_at = now
    elif bar_close_at > now:
        bar_close_at = now
    closes = snapshot.get("closes")
    volumes = snapshot.get("volumes")
    # Deterministic per-bar identity: the decision cutoff is the completed
    # bar itself, so a rescan of the same bar dedupes (same idempotency key
    # and same semantic input) instead of conflicting.
    return build_candidate_input(
        assessment_id=assessment_id,
        market=market,
        symbol=str(snapshot.get("symbol") or "").upper(),
        exchange="PSE" if market == "ph" else "NASDAQ",
        timeframe=str(snapshot.get("timeframe") or "1d"),
        candidate=candidate,
        closes=list(closes[-60:]) if closes else None,
        volumes=list(volumes[-60:]) if volumes else None,
        decision_at=bar_close_at,
        bar_close_at=bar_close_at,
        currency="PHP" if market == "ph" else "USD",
        runtime=runtime,
    )


def _service_or_default(service):
    if service is not None:
        return service
    from core.laya_service import get_service

    return get_service()


def _paper_ids(snapshot: dict[str, Any], market: str, policy: str,
               prefix: str) -> tuple[str, str, str]:
    """Deterministic per-bar identity so rescans dedupe instead of conflicting."""
    import hashlib

    symbol = str(snapshot.get("symbol") or "").upper()
    bar_key = str(snapshot.get("bar_ts") or "live").replace(":", "").replace("+", "")
    digest = hashlib.sha256(
        f"{market}|{symbol}|{bar_key}|{policy}".encode()).hexdigest()[:16]
    assessment_id = f"{prefix}-{digest}"
    idempotency_key = f"paper-{market}-{symbol}-{bar_key}-{policy}"[:200]
    return symbol, assessment_id, idempotency_key
    if service is not None:
        return service
    from core.laya_service import get_service

    return get_service()


def _wait_terminal(service, job_id: str, deadline_s: float) -> dict[str, Any] | None:
    end = time.monotonic() + deadline_s
    while time.monotonic() < end:
        job = service.store.get_job(job_id)
        if job is None:
            return None
        if job.get("state") in ("succeeded", "failed", "cancelled", "interrupted"):
            return job
        time.sleep(0.2)
    return None


def _run_once(service, assessment, idempotency_key: str, *, policy: str,
              mode: str, book: str, candidate_id: str, deadline: float,
              default_aid: str) -> tuple[str, list[str], str | None]:
    """Submit, wait, evaluate one assessment. Never raises: failures abstain."""
    from core.laya_service import ConflictError

    try:
        job, created = service.submit(assessment, idempotency_key=idempotency_key)
        # A retry dedupes onto the existing job: evaluate the stored
        # assessment, not the rebuilt copy.
        aid = default_aid if created else job["assessment_id"]
    except ConflictError:
        return ("abstain",
                ["idempotency conflict: key already used with different input"],
                None)
    except Exception as exc:  # noqa: BLE001 - failures abstain, never bypass
        return "abstain", [f"submission failed: {exc}"], None
    terminal = _wait_terminal(service, job["id"], deadline)
    if terminal is None:
        return ("abstain", [f"assessment deadline exceeded ({deadline:g}s)"], aid)
    if terminal.get("state") != "succeeded":
        return ("abstain", [f"assessment {terminal.get('state')}: "
                            f"{terminal.get('error_detail') or terminal.get('error_code') or 'failed'}"], aid)
    try:
        decision = service.evaluate(aid, policy, mode=mode,
                                    book_id=book, candidate_id=candidate_id)
    except Exception as exc:  # noqa: BLE001 - evaluation failure abstains
        return "abstain", [f"policy evaluation failed: {exc}"], aid
    return (decision["outcome"],
            list(decision.get("reasons") or ["no reason"]), aid)


def _assess_filter(snapshot: dict[str, Any], *, market: str, policy: str,
                   book: str, deadline: float, service) -> dict[str, Any]:
    """Synchronous policy gate: allow opens, veto/abstain skips the entry."""
    service = _service_or_default(service)
    task = _policy_task(service, policy)
    if task == "event-review-v1":
        return _assess_event_filter(snapshot, market=market, policy=policy,
                                    book=book, deadline=deadline,
                                    service=service)
    if task is None:
        return {"outcome": "abstain", "reasons": [f"unknown policy {policy}"],
                "assessment_id": None, "mode": "filter"}
    if task != "candidate-review-v1":
        return {"outcome": "abstain",
                "reasons": [f"unsupported policy task {task}"],
                "assessment_id": None, "mode": "filter"}
    symbol, assessment_id, idempotency_key = _paper_ids(
        snapshot, market, policy, "asm-paper")
    try:
        assessment = _build_input(snapshot, market, assessment_id)
    except ValueError as exc:
        return {"outcome": "abstain", "reasons": [f"invalid candidate: {exc}"],
                "assessment_id": None, "mode": "filter"}
    outcome, reasons, aid = _run_once(
        service, assessment, idempotency_key, policy=policy, mode="filter",
        book=book, candidate_id=symbol, deadline=deadline,
        default_aid=assessment_id)
    return {"outcome": outcome, "reasons": reasons,
            "assessment_id": aid, "mode": "filter"}


def _build_event_input(snapshot: dict[str, Any], filing: dict,
                       excerpt: str, assessment_id: str, market: str):
    from core.laya_context import build_text_input

    symbol = str(snapshot.get("symbol") or "").upper()
    assessment, _doc = build_text_input(
        assessment_id=assessment_id, market=market, symbol=symbol,
        exchange="PSE" if market == "ph" else "NASDAQ",
        task_id="event-review-v1", source_text=excerpt,
        source_title=f"{filing['form']} {filing['accession']} filed {filing['filed']}",
        publisher="EDGAR",
        publication_time=filing["filed"],
        available_at=filing["accepted_at"],
        decision_at=_cutoff(snapshot),
        bar_close_at=_cutoff(snapshot),
    )
    return assessment


def _assess_event_filter(snapshot: dict[str, Any], *, market: str,
                         policy: str, book: str, deadline: float,
                         service) -> dict[str, Any]:
    """Event-risk veto over EDGAR filing text: open unless danger is shown.

    Classifies each recent offering/adverse-relevant filing newest-first (capped)
    and vetoes on the first one that trips the event policy. No filing evidence
    abstains (nothing known dangerous, but nothing assessed either).
    """
    from data.laya_filings import excerpt_offering, fetch_filing_text, relevant_filings

    started = time.monotonic()
    symbol = str(snapshot.get("symbol") or "").upper()
    cutoff = _cutoff(snapshot)
    try:
        filings = relevant_filings(symbol, cutoff=cutoff,
                                   lookback_days=FILING_LOOKBACK_DAYS)
    except Exception as exc:  # noqa: BLE001 - ingestion failure abstains
        return {"outcome": "abstain",
                "reasons": [f"filing lookup failed: {exc}"],
                "assessment_id": None, "mode": "filter"}
    if not filings:
        return {"outcome": "abstain",
                "reasons": ["no offering/adverse filing evidence in window"],
                "assessment_id": None, "mode": "filter"}
    seen: list[str] = []
    last_aid: str | None = None
    for filing in sorted(filings, key=lambda f: f["accepted_at"])[-MAX_FILINGS_PER_CANDIDATE:][::-1]:
        remaining = deadline - (time.monotonic() - started)
        if remaining < 2.0:
            seen.append(f"{filing['form']}: deadline exhausted")
            break
        text = fetch_filing_text(filing["doc_url"])
        if not text:
            seen.append(f"{filing['form']}: document unavailable")
            continue
        excerpt, _shortened = excerpt_offering(text)
        # Evidence-aware identity: the excerpt hash joins the assessment id
        # and the idempotency key, so identical evidence dedupes while
        # changed excerpting can never silently reuse a stale verdict.
        import hashlib as _hashlib

        evidence = _hashlib.sha256(excerpt.encode()).hexdigest()[:12]
        assessment_id = "asm-fil-" + _hashlib.sha256(
            f"{market}|{symbol}|{filing['accession']}|{policy}|{evidence}"
            .encode()).hexdigest()[:16]
        idempotency_key = (
            f"paper-{market}-{symbol}-{filing['accession']}-{evidence}-{policy}")[:200]
        try:
            assessment = _build_event_input(snapshot, filing, excerpt,
                                            assessment_id, market)
        except ValueError as exc:
            seen.append(f"{filing['form']}: invalid evidence ({exc})")
            continue
        outcome, reasons, aid = _run_once(
            service, assessment, idempotency_key, policy=policy, mode="filter",
            book=book, candidate_id=symbol, deadline=remaining,
            default_aid=assessment_id)
        last_aid = aid or last_aid
        seen.append(f"{filing['form']} {filing['filed']}: {outcome}")
        if outcome == "veto":
            return {"outcome": "veto",
                    "reasons": reasons + [f"evidence: {filing['form']} "
                                          f"filed {filing['filed']}"],
                    "assessment_id": aid, "mode": "filter"}
        if outcome == "abstain" and aid is not None:
            # Evidence existed but could not be gated (truncated, failed
            # inference): fail closed rather than skating past it.
            return {"outcome": "abstain", "reasons": reasons,
                    "assessment_id": aid, "mode": "filter"}
        # Broken plumbing for this filing (no assessment stored): older
        # filings may still yield a verdict.
        continue
    return {"outcome": "allow",
            "reasons": [f"no filing tripped {policy}: " + "; ".join(seen)],
            "assessment_id": last_aid, "mode": "filter"}


def _submit_shadow(snapshot: dict[str, Any], *, market: str, policy: str,
                   book: str, service) -> None:
    """Enqueue the assessment without awaiting inference (fills unchanged)."""
    symbol, assessment_id, key = _paper_ids(snapshot, market, policy, "asm-shadow")

    def _run() -> None:
        try:
            svc = _service_or_default(service)
            if _policy_task(svc, policy) == "event-review-v1":
                _shadow_event(dict(snapshot), market=market, policy=policy,
                              book=book, symbol=symbol, service=svc)
                return
            assessment = _build_input(dict(snapshot), market, assessment_id)
            job, created = svc.submit(assessment, idempotency_key=key)
            aid = assessment_id if created else job["assessment_id"]
            terminal = _wait_terminal(svc, job["id"], 300.0)
            if terminal is not None and terminal.get("state") == "succeeded":
                svc.evaluate(aid, policy, mode="shadow",
                             book_id=book, candidate_id=symbol)
        except Exception:
            # Shadow may drop the assessment with an operational error; it
            # must never affect fills.
            from utils.logger import log

            log.exception("Laya shadow | background assessment failed")

    with _executor_lock:
        _executor.submit(_run)


def _shadow_event(snapshot: dict[str, Any], *, market: str, policy: str,
                  book: str, symbol: str, service) -> None:
    """Background event-risk recording: every recent filing, no deadline."""
    from data.laya_filings import excerpt_offering, fetch_filing_text, relevant_filings

    try:
        filings = relevant_filings(symbol, cutoff=_cutoff(snapshot),
                                   lookback_days=FILING_LOOKBACK_DAYS)
    except Exception:
        return
    for filing in sorted(filings, key=lambda f: f["accepted_at"])[-MAX_FILINGS_PER_CANDIDATE:][::-1]:
        text = fetch_filing_text(filing["doc_url"])
        if not text:
            continue
        excerpt, _shortened = excerpt_offering(text)
        import hashlib as _hashlib

        evidence = _hashlib.sha256(excerpt.encode()).hexdigest()[:12]
        assessment_id = "asm-shfil-" + _hashlib.sha256(
            f"{market}|{symbol}|{filing['accession']}|{policy}|{evidence}".encode()
        ).hexdigest()[:16]
        key = (f"paper-{market}-{symbol}-"
               f"{filing['accession']}-{evidence}-{policy}-shadow")[:200]
        try:
            assessment = _build_event_input(snapshot, filing, excerpt,
                                            assessment_id, market)
        except ValueError:
            continue
        outcome, _reasons, aid = _run_once(
            service, assessment, key, policy=policy, mode="shadow",
            book=book, candidate_id=symbol, deadline=300.0,
            default_aid=assessment_id)
        if outcome == "veto":
            break
