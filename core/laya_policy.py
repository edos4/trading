"""Deterministic Laya policy evaluator (docs/laya.md section 10).

Used by historical replay AND paper. The model never changes direction, stop,
target, quantity, currency, fees, lots, or exits. Produces allow/veto/abstain
plus explicit reasons. Inference timeout, malformed answer, stale context, and
missing model are abstentions, never positive votes.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from core.laya_contracts import (
    LayaAssessmentResult, LayaPolicy, LayaPolicyDecision,
)

_SCORE_ORDER = ("insufficient", "weak", "moderate", "strong")


def policy_decision_hash(
    *, assessment_id: str, policy_id: str, mode: str,
    outcome: str, reasons: tuple[str, ...],
) -> str:
    payload = {
        "assessment_id": assessment_id, "policy_id": policy_id,
        "mode": mode, "outcome": outcome, "reasons": list(reasons),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def evaluate_policy(
    *,
    result: LayaAssessmentResult | None,
    policy: LayaPolicy,
    mode: str,
    market: str,
    candidate_id: str | None = None,
    book_id: str | None = None,
    run_id: str | None = None,
    now: datetime | None = None,
) -> LayaPolicyDecision:
    ts = now or datetime.now(timezone.utc)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)

    def _decision(outcome: str, reasons: list[str]) -> LayaPolicyDecision:
        reasons_t = tuple(reasons)
        assessment_id = result.assessment_id if result is not None else "missing"
        return LayaPolicyDecision(
            assessment_id=assessment_id,
            policy_id=policy.policy_id,
            candidate_id=candidate_id,
            book_id=book_id,
            run_id=run_id,
            mode=mode,  # type: ignore[typeddict-item]
            outcome=outcome,  # type: ignore[typeddict-item]
            reasons=reasons_t,
            evaluated_at=ts,
            decision_hash=policy_decision_hash(
                assessment_id=assessment_id, policy_id=policy.policy_id,
                mode=mode, outcome=outcome, reasons=reasons_t,
            ),
        )

    if mode == "off":
        return _decision("abstain", ["laya mode is off; baseline unchanged"])
    if result is None:
        return _decision("abstain", ["missing assessment: no valid Laya result"])
    if market not in list(policy.markets):
        return _decision("abstain", [f"market {market} not covered by policy {policy.policy_id}"])
    if result.truncated or result.options_collapsed:
        return _decision("abstain", ["evidence truncated or options collapsed: not gate-eligible"])
    by_qid = {a.qid: a for a in result.answers}

    if policy.task_id == "candidate-review-v1":
        return _evaluate_candidate(by_qid, policy, _decision)
    if policy.task_id == "event-review-v1":
        return _evaluate_event(by_qid, policy, _decision)
    return _decision("abstain", [f"unknown policy task {policy.task_id}"])


def _evaluate_event(by_qid: dict, policy: LayaPolicy, decide) -> LayaPolicyDecision:
    """Veto-only gate over filing evidence.

    Open by default: entries proceed unless a filing affirmatively shows
    dilution or material adversity at the policy's confidence bars. Weak or
    missing danger evidence allows with a reason (absence of danger is the
    normal state); only broken inputs abstain. This is the opposite asymmetry
    from candidate review, which needs affirmative support to allow.
    """
    event = by_qid.get("event_type")
    tone = by_qid.get("directional_tone")
    risk = by_qid.get("material_risk_mentioned")
    if event is None or tone is None or risk is None:
        return decide("abstain", ["missing required event-review answers"])
    for ans in (event, tone, risk):
        if ans.answer_confidence is None:
            return decide("abstain", [f"question {ans.qid} lacks answer_confidence"])
    if risk.answer not in ("yes", "no"):
        return decide("abstain", [f"unexpected risk answer {risk.answer!r}"])
    if event.answer not in ("earnings", "guidance", "financing", "corporate_action",
                            "regulatory", "operations", "other", "insufficient"):
        return decide("abstain", [f"unexpected event answer {event.answer!r}"])
    if risk.answer == "yes":
        yes_p = risk.yes_probability if risk.yes_probability is not None else 1.0
        if yes_p >= policy.veto_on_material_risk_yes_prob:
            return decide("veto", [f"material adverse event yes_prob={yes_p:.3f}"])
    if event.answer == "financing":
        conf = event.answer_confidence or 0.0
        if conf >= policy.veto_financing_min_confidence:
            return decide("veto", [
                f"financing event (tone={tone.answer}) "
                f"answer_confidence={conf:.3f}"])
    return decide("allow", [
        f"no danger signal: event={event.answer} tone={tone.answer} "
        f"risk={risk.answer}"])


def _evaluate_candidate(by_qid: dict, policy: LayaPolicy, decide) -> LayaPolicyDecision:
    alignment = by_qid.get("evidence_alignment")
    strength = by_qid.get("evidence_strength")
    conflict = by_qid.get("material_conflict")
    if alignment is None or strength is None or conflict is None:
        return decide("abstain", ["missing required candidate-review answers"])
    for ans in (alignment, strength, conflict):
        if ans.answer_confidence is None:
            return decide("abstain", [f"question {ans.qid} lacks answer_confidence"])
    # Validate expected answer types/options before applying thresholds.
    if alignment.answer not in ("supports", "mixed", "conflicts", "insufficient"):
        return decide("abstain", [f"unexpected alignment answer {alignment.answer!r}"])
    if strength.answer not in _SCORE_ORDER:
        return decide("abstain", [f"unexpected strength answer {strength.answer!r}"])
    if conflict.answer not in ("yes", "no"):
        return decide("abstain", [f"unexpected conflict answer {conflict.answer!r}"])

    reasons: list[str] = []
    # Material conflict veto: supplied evidence explicitly contradicts direction.
    if conflict.answer == "yes":
        threshold = policy.veto_on_material_conflict_yes_prob
        yes_p = conflict.yes_probability if conflict.yes_probability is not None else 1.0
        if threshold is None or yes_p >= threshold:
            return decide("veto", [f"material conflict yes_prob={yes_p:.3f}"])
        reasons.append(f"conflict yes_prob={yes_p:.3f} below veto threshold")
    # Confidence floor (application threshold on answer_confidence, never the
    # entropy field).
    conf = min(
        a.answer_confidence for a in (alignment, strength, conflict)
        if a.answer_confidence is not None
    )
    if conf < policy.min_answer_confidence:
        return decide("abstain", [
            f"min answer_confidence {conf:.3f} below policy floor {policy.min_answer_confidence:.3f}",
        ])
    if alignment.answer not in list(policy.require_alignment):
        return decide("veto", [
            f"alignment {alignment.answer!r} not in required {list(policy.require_alignment)}",
        ])
    if policy.min_evidence_score is not None:
        need = _SCORE_ORDER.index(policy.min_evidence_score) if policy.min_evidence_score in _SCORE_ORDER else 0
        have = _SCORE_ORDER.index(strength.answer)
        if have < need:
            return decide("veto", [
                f"evidence strength {strength.answer!r} below minimum {policy.min_evidence_score!r}",
            ])
    summary = (
        f"alignment={alignment.answer} strength={strength.answer} "
        f"conflict={conflict.answer} min_conf={conf:.3f}"
    )
    reasons.append(summary)
    return decide("allow", reasons)
