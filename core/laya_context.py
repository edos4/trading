"""Immutable Laya evidence builder (docs/laya.md section 6).

Constructs an allowlisted assessment input. Never serializes an entire
TradeSignal, account object, candle table, or trade-export bundle. Excludes
future outcomes, P&L, exit reasons, later daily marks, credentials, and
unrelated portfolio state.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Sequence

from core.laya_contracts import (
    LAYA_FEATURE_SCHEMA_VERSION,
    LAYA_SCHEMA_VERSION,
    LayaAssessmentInput,
    LayaCandidate,
    LayaDocument,
    LayaFeatures,
    LayaQuestion,
    LayaRuntime,
    TASK_PRESETS,
)

MAX_CONTEXT_CHARS = 6000


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _ensure_aware(value: datetime | str) -> datetime:
    if isinstance(value, str):
        dt = datetime.fromisoformat(value)
    else:
        dt = value
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def question_schema_hash(questions: Sequence[LayaQuestion]) -> str:
    payload = [
        {"qid": q.qid, "kind": q.kind, "text": q.text,
         "options": list(q.options), "rubric": list(q.rubric)}
        for q in questions
    ]
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def task_questions(task_id: str) -> tuple[LayaQuestion, ...]:
    preset = TASK_PRESETS.get(task_id)
    if preset is None:
        raise ValueError(f"Unknown Laya task: {task_id}")
    return tuple(preset["questions"])


def compute_features(
    closes: Sequence[float] | None = None,
    volumes: Sequence[float] | None = None,
    reward_risk: float | None = None,
) -> LayaFeatures:
    """Small explicitly versioned set of computed facts with units/lookback."""
    missing: list[str] = []
    close_above: bool | None = None
    vol_ratio: float | None = None
    bucket: Any = None
    if closes and len(closes) >= 20:
        mean20 = sum(closes[-20:]) / 20
        close_above = bool(closes[-1] > mean20) if mean20 else None
        # Volatility bucket from 20-bar range/mean (deterministic, versioned).
        import statistics

        try:
            stdev = statistics.pstdev(closes[-20:])
            cv = (stdev / mean20) if mean20 else 0.0
            bucket = "low" if cv < 0.02 else ("medium" if cv < 0.05 else "high")
        except statistics.StatisticsError:
            missing.append("volatility")
    else:
        missing.append("trend_20d")
    if volumes and len(volumes) >= 20:
        mean_v = sum(volumes[-20:]) / 20
        vol_ratio = float(volumes[-1] / mean_v) if mean_v else None
        if vol_ratio is not None and not (vol_ratio == vol_ratio and vol_ratio >= 0):
            vol_ratio = None
            missing.append("volume_ratio_20d")
    else:
        missing.append("volume_20d")
    rr = None
    if reward_risk is not None and reward_risk == reward_risk:
        rr = float(reward_risk)
    return LayaFeatures(
        close_above_20d_mean=close_above,
        volume_ratio_20d=vol_ratio,
        reward_risk=rr,
        volatility_bucket=bucket,
        trend="",
        missing=tuple(missing),
    )


def render_compact_state(
    *,
    market: str,
    symbol: str,
    currency: str,
    timeframe: str,
    cutoff_iso: str,
    candidate: LayaCandidate | None,
    features: LayaFeatures,
    external_text_available: bool,
) -> str:
    lines = [
        "Task: review supplied candidate evidence; do not infer missing news.",
        f"Market: {market.upper()}; symbol: {symbol}; currency: {currency}; timeframe: {timeframe}.",
        f"Evidence cutoff: {cutoff_iso}; all bars complete through this cutoff.",
    ]
    if candidate is not None:
        lines.append(
            f"Candidate: {candidate.direction}; detector: {candidate.detector}; "
            f"engine eligibility: {candidate.engine_eligibility}."
        )
    else:
        lines.append("Candidate: none; text classification only.")
    facts: list[str] = []
    if features.close_above_20d_mean is not None:
        facts.append(f"close_above_20d_mean={str(features.close_above_20d_mean).lower()}")
    if features.volume_ratio_20d is not None:
        facts.append(f"volume_ratio_20d={features.volume_ratio_20d:.2f}")
    if features.reward_risk is not None:
        facts.append(f"reward_risk={features.reward_risk:.2f}")
    if features.volatility_bucket is not None:
        facts.append(f"volatility_bucket={features.volatility_bucket}")
    lines.append(f"Computed facts: {'; '.join(facts) if facts else 'unavailable'}.")
    lines.append(f"External text: {'supplied' if external_text_available else 'unavailable'}.")
    return "\n".join(lines) + "\n"


def build_text_input(
    *,
    assessment_id: str,
    market: str,
    symbol: str,
    exchange: str,
    timeframe: str = "1d",
    task_id: str = "event-review-v1",
    source_text: str,
    source_title: str = "",
    publisher: str = "",
    language: str = "en",
    publication_time: datetime | str | None = None,
    available_at: datetime | str | None = None,
    decision_at: datetime | str | None = None,
    bar_close_at: datetime | str | None = None,
    runtime: LayaRuntime | None = None,
) -> tuple[LayaAssessmentInput, LayaDocument]:
    """Validate pasted operator text and build an immutable text-mode input."""
    from core.laya_contracts import TASK_PRESETS as _P

    if task_id not in _P or _P[task_id]["input_mode"] != "text":
        raise ValueError(f"Task {task_id!r} does not accept pasted text")
    text = (source_text or "").strip()
    if not text:
        raise ValueError("Source text is required for text review")
    if len(text.encode("utf-8")) > 20000:
        raise ValueError("Source text exceeds the bounded evidence size")
    now = datetime.now(timezone.utc)
    dec = _ensure_aware(decision_at) if decision_at else now
    bar = _ensure_aware(bar_close_at) if bar_close_at else dec
    pub = _ensure_aware(publication_time) if publication_time else None
    avail = _ensure_aware(available_at) if available_at else (pub or now)
    if pub is not None and avail < pub:
        raise ValueError("available_at precedes publication_time")
    # Availability rule: a source is eligible only if its verifiable
    # availability time is no later than the simulated decision cutoff.
    if avail > dec:
        raise ValueError("Source available_at is after the decision cutoff")
    doc = LayaDocument(
        source_id=f"op-{_sha256_text(text)[:16]}",
        issuer_symbol=symbol.upper().strip(),
        title=source_title[:500],
        publisher=publisher[:200],
        language=(language or "en")[:16],
        text_sha256=_sha256_text(text),
        text_bytes=len(text.encode("utf-8")),
        publication_time=pub,
        available_at=avail,
        ingested_at=now,
        revision_id=f"r1-{_sha256_text(text)[:12]}",
        operator_supplied=True,
    )
    questions = task_questions(task_id)
    cutoff_iso = dec.isoformat()
    context = (
        f"Task: classify the supplied announcement for the named issuer.\n"
        f"Market: {market.upper()}; symbol: {symbol.upper()}; language: {doc.language}.\n"
        f"Evidence cutoff: {cutoff_iso}.\n"
        f"Source: {source_title[:120] or doc.source_id} ({publisher[:80] or 'operator-supplied'}).\n"
        f"Source text:\n{text[:MAX_CONTEXT_CHARS]}\n"
    )
    if len(text) > MAX_CONTEXT_CHARS:
        context += "[source truncated for model input; full hash retained]\n"
    content_hash = _sha256_text(context)
    assessment = LayaAssessmentInput(
        assessment_id=assessment_id,
        schema_version=LAYA_SCHEMA_VERSION,
        market=market.lower(),  # type: ignore[typeddict-item]
        symbol=symbol.upper().strip(),
        exchange=exchange.upper().strip(),
        timeframe=timeframe,  # type: ignore[typeddict-item]
        decision_at=dec,
        bar_close_at=bar,
        observed_at=now,
        as_of=dec,
        features=LayaFeatures(),
        documents=(doc,),
        context_text=context,
        content_hash=content_hash,
        feature_schema_version=LAYA_FEATURE_SCHEMA_VERSION,
        question_schema_id=task_id,
        question_schema_hash=question_schema_hash(questions),
        questions=questions,
        runtime=runtime or LayaRuntime(),
    )
    return assessment, doc


def build_candidate_input(
    *,
    assessment_id: str,
    market: str,
    symbol: str,
    exchange: str,
    timeframe: str = "1d",
    task_id: str = "candidate-review-v1",
    candidate: LayaCandidate,
    closes: Sequence[float] | None = None,
    volumes: Sequence[float] | None = None,
    decision_at: datetime | str | None = None,
    bar_close_at: datetime | str | None = None,
    currency: str = "USD",
    runtime: LayaRuntime | None = None,
) -> LayaAssessmentInput:
    from core.laya_contracts import TASK_PRESETS as _P

    if task_id not in _P or _P[task_id]["input_mode"] != "candidate":
        raise ValueError(f"Task {task_id!r} does not accept candidate evidence")
    now = datetime.now(timezone.utc)
    dec = _ensure_aware(decision_at) if decision_at else now
    bar = _ensure_aware(bar_close_at) if bar_close_at else dec
    if bar > dec:
        raise ValueError("bar_close_at is after the decision cutoff")
    features = compute_features(closes, volumes, candidate.reward_risk)
    cutoff_iso = dec.isoformat()
    context = render_compact_state(
        market=market, symbol=symbol.upper(), currency=currency,
        timeframe=timeframe, cutoff_iso=cutoff_iso, candidate=candidate,
        features=features, external_text_available=False,
    )
    questions = task_questions(task_id)
    return LayaAssessmentInput(
        assessment_id=assessment_id,
        schema_version=LAYA_SCHEMA_VERSION,
        market=market.lower(),  # type: ignore[typeddict-item]
        symbol=symbol.upper().strip(),
        exchange=exchange.upper().strip(),
        timeframe=timeframe,  # type: ignore[typeddict-item]
        decision_at=dec,
        bar_close_at=bar,
        observed_at=now,
        as_of=dec,
        candidate=candidate,
        features=features,
        documents=(),
        context_text=context,
        content_hash=_sha256_text(context),
        feature_schema_version=LAYA_FEATURE_SCHEMA_VERSION,
        question_schema_id=task_id,
        question_schema_hash=question_schema_hash(questions),
        questions=questions,
        runtime=runtime or LayaRuntime(),
    )


def input_hash(assessment: LayaAssessmentInput) -> str:
    return hashlib.sha256(
        json.dumps(assessment.model_dump(mode="json"), sort_keys=True).encode()
    ).hexdigest()


def semantic_hash(assessment: LayaAssessmentInput) -> str:
    """Stable identity for idempotency: excludes wall-clock-only fields.

    `observed_at` and document `ingested_at` are operational timestamps, not
    evidence. Two operator retries seconds apart must compare equal; causal
    cutoffs (decision_at/bar_close_at/as_of/available_at) stay in the hash.
    """
    data = assessment.model_dump(mode="json")
    data.pop("observed_at", None)
    docs = []
    for doc in data.get("documents") or []:
        doc = dict(doc)
        doc.pop("ingested_at", None)
        docs.append(doc)
    data["documents"] = docs
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def coalescing_hash(assessment: LayaAssessmentInput) -> str:
    """Hash of exact evidence+cutoff+schema+model+calibration+inference settings.

    Same raw assessment may support multiple policy evaluations, but a cached
    assessment must never cross market/symbol identities or data revisions.
    """
    payload = {
        "market": assessment.market,
        "symbol": assessment.symbol,
        "as_of": assessment.as_of.isoformat(),
        "context_text": assessment.context_text,
        "content_hash": assessment.content_hash,
        "feature_schema": assessment.feature_schema_version,
        "question_schema": [assessment.question_schema_id, assessment.question_schema_hash],
        "questions": [q.model_dump(mode="json") for q in assessment.questions],
        "model": assessment.runtime.model_dump(mode="json"),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
