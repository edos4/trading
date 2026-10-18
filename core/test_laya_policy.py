"""Laya policy evaluator checks (docs/laya.md P6 + section 15)."""
from __future__ import annotations

import unittest
from datetime import datetime, timezone

from core.laya_contracts import LayaAnswer, LayaAssessmentResult, LayaPolicy
from core.laya_policy import evaluate_policy


def _result(**overrides) -> LayaAssessmentResult:
    answers = overrides.pop("answers", None)
    if answers is None:
        answers = (
            LayaAnswer(qid="evidence_alignment", kind="choice", answer="supports",
                       distribution=(0.8, 0.1, 0.05, 0.05), answer_confidence=0.8,
                       confidence=0.7),
            LayaAnswer(qid="evidence_strength", kind="score", answer="moderate",
                       distribution=(0.1, 0.2, 0.5, 0.2), answer_confidence=0.6,
                       confidence=0.6, score_value=2.0),
            LayaAnswer(qid="material_conflict", kind="noul", answer="no",
                       answer_confidence=0.9, confidence=0.9, yes_probability=0.05),
        )
    return LayaAssessmentResult(
        assessment_id="asm-1",
        input_hash="0" * 64,
        answers=answers,
        **overrides,
    )


def _policy(**overrides) -> LayaPolicy:
    base = dict(policy_id="pol-1", task_id="candidate-review-v1",
                model_id="pinned", markets=("us", "ph"),
                min_answer_confidence=0.5,
                require_alignment=("supports",),
                veto_on_material_conflict_yes_prob=0.5,
                min_evidence_score="weak")
    base.update(overrides)
    return LayaPolicy(**base)


class LayaPolicyTests(unittest.TestCase):
    def test_allow(self) -> None:
        d = evaluate_policy(result=_result(), policy=_policy(), mode="filter", market="us")
        self.assertEqual(d.outcome, "allow")

    def test_veto_on_alignment(self) -> None:
        r = _result(answers=(
            LayaAnswer(qid="evidence_alignment", kind="choice", answer="conflicts",
                       distribution=(0.05, 0.05, 0.8, 0.1), answer_confidence=0.8,
                       confidence=0.7),
            LayaAnswer(qid="evidence_strength", kind="score", answer="moderate",
                       distribution=(0.1, 0.2, 0.5, 0.2), answer_confidence=0.6,
                       confidence=0.6, score_value=2.0),
            LayaAnswer(qid="material_conflict", kind="noul", answer="no",
                       answer_confidence=0.9, confidence=0.9, yes_probability=0.05),
        ))
        d = evaluate_policy(result=r, policy=_policy(), mode="filter", market="us")
        self.assertEqual(d.outcome, "veto")

    def test_veto_on_material_conflict(self) -> None:
        r = _result(answers=(
            LayaAnswer(qid="evidence_alignment", kind="choice", answer="supports",
                       distribution=(0.8, 0.1, 0.05, 0.05), answer_confidence=0.8,
                       confidence=0.7),
            LayaAnswer(qid="evidence_strength", kind="score", answer="moderate",
                       distribution=(0.1, 0.2, 0.5, 0.2), answer_confidence=0.6,
                       confidence=0.6, score_value=2.0),
            LayaAnswer(qid="material_conflict", kind="noul", answer="yes",
                       answer_confidence=0.9, confidence=0.9, yes_probability=0.9),
        ))
        d = evaluate_policy(result=r, policy=_policy(), mode="filter", market="us")
        self.assertEqual(d.outcome, "veto")

    def test_abstain_on_low_confidence(self) -> None:
        d = evaluate_policy(result=_result(), policy=_policy(min_answer_confidence=0.95),
                            mode="filter", market="us")
        self.assertEqual(d.outcome, "abstain")

    def test_abstain_on_truncation(self) -> None:
        d = evaluate_policy(result=_result(truncated=True), policy=_policy(),
                            mode="filter", market="us")
        self.assertEqual(d.outcome, "abstain")

    def test_missing_result_abstains(self) -> None:
        d = evaluate_policy(result=None, policy=_policy(), mode="filter", market="us")
        self.assertEqual(d.outcome, "abstain")

    def test_off_mode_abstains(self) -> None:
        d = evaluate_policy(result=_result(), policy=_policy(), mode="off", market="us")
        self.assertEqual(d.outcome, "abstain")

    def test_market_mismatch_abstains(self) -> None:
        d = evaluate_policy(result=_result(), policy=_policy(markets=("us",)),
                            mode="filter", market="ph")
        self.assertEqual(d.outcome, "abstain")

    def test_missing_confidence_abstains(self) -> None:
        r = _result(answers=(
            LayaAnswer(qid="evidence_alignment", kind="choice", answer="supports",
                       distribution=(0.8, 0.1, 0.05, 0.05),
                       answer_confidence=None, confidence=0.7),
            LayaAnswer(qid="evidence_strength", kind="score", answer="moderate",
                       distribution=(0.1, 0.2, 0.5, 0.2), answer_confidence=0.6,
                       confidence=0.6, score_value=2.0),
            LayaAnswer(qid="material_conflict", kind="noul", answer="no",
                       answer_confidence=0.9, confidence=0.9, yes_probability=0.05),
        ))
        d = evaluate_policy(result=r, policy=_policy(), mode="filter", market="us")
        self.assertEqual(d.outcome, "abstain")

    def test_deterministic_hash(self) -> None:
        ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
        a = evaluate_policy(result=_result(), policy=_policy(), mode="filter",
                            market="us", now=ts)
        b = evaluate_policy(result=_result(), policy=_policy(), mode="filter",
                            market="us", now=ts)
        self.assertEqual(a.decision_hash, b.decision_hash)


def _event_result(**overrides) -> LayaAssessmentResult:
    answers = overrides.pop("answers", None)
    if answers is None:
        answers = (
            LayaAnswer(qid="event_type", kind="choice", answer="earnings",
                       distribution=(0.7, 0.05, 0.05, 0.05, 0.02, 0.03, 0.05, 0.0),
                       answer_confidence=0.7, confidence=0.5),
            LayaAnswer(qid="directional_tone", kind="choice", answer="positive",
                       distribution=(0.75, 0.05, 0.05, 0.1, 0.05),
                       answer_confidence=0.75, confidence=0.5),
            LayaAnswer(qid="material_risk_mentioned", kind="noul", answer="no",
                       answer_confidence=0.9, confidence=0.9, yes_probability=0.1),
        )
    return LayaAssessmentResult(
        assessment_id="asm-evt-1",
        input_hash="0" * 64,
        answers=answers,
        **overrides,
    )


def _event_policy(**overrides) -> LayaPolicy:
    base = dict(policy_id="pol-evt", task_id="event-review-v1",
                model_id="pinned", markets=("us",),
                veto_on_material_risk_yes_prob=0.6,
                veto_financing_min_confidence=0.35)
    base.update(overrides)
    return LayaPolicy(**base)


class LayaEventPolicyTests(unittest.TestCase):
    def test_allow_when_no_danger(self) -> None:
        d = evaluate_policy(result=_event_result(), policy=_event_policy(),
                            mode="filter", market="us")
        self.assertEqual(d.outcome, "allow")

    def test_veto_on_financing(self) -> None:
        r = _event_result(answers=(
            LayaAnswer(qid="event_type", kind="choice", answer="financing",
                       distribution=(0.05, 0.05, 0.58, 0.05, 0.02, 0.03, 0.12, 0.1),
                       answer_confidence=0.58, confidence=0.5),
            LayaAnswer(qid="directional_tone", kind="choice", answer="neutral",
                       distribution=(0.05, 0.05, 0.05, 0.7, 0.15),
                       answer_confidence=0.7, confidence=0.5),
            LayaAnswer(qid="material_risk_mentioned", kind="noul", answer="no",
                       answer_confidence=0.9, confidence=0.9, yes_probability=0.1),
        ))
        d = evaluate_policy(result=r, policy=_event_policy(),
                            mode="filter", market="us")
        self.assertEqual(d.outcome, "veto")

    def test_weak_financing_allows(self) -> None:
        """Below-threshold danger evidence allows: the gate is veto-only."""
        r = _event_result(answers=(
            LayaAnswer(qid="event_type", kind="choice", answer="financing",
                       distribution=(0.1, 0.1, 0.24, 0.1, 0.06, 0.1, 0.2, 0.1),
                       answer_confidence=0.24, confidence=0.4),
            LayaAnswer(qid="directional_tone", kind="choice", answer="neutral",
                       distribution=(0.05, 0.05, 0.05, 0.7, 0.15),
                       answer_confidence=0.7, confidence=0.5),
            LayaAnswer(qid="material_risk_mentioned", kind="noul", answer="no",
                       answer_confidence=0.9, confidence=0.9, yes_probability=0.1),
        ))
        d = evaluate_policy(result=r, policy=_event_policy(),
                            mode="filter", market="us")
        self.assertEqual(d.outcome, "allow")

    def test_veto_on_material_risk(self) -> None:
        r = _event_result(answers=(
            LayaAnswer(qid="event_type", kind="choice", answer="regulatory",
                       distribution=(0.05, 0.05, 0.05, 0.1, 0.6, 0.05, 0.05, 0.05),
                       answer_confidence=0.6, confidence=0.5),
            LayaAnswer(qid="directional_tone", kind="choice", answer="negative",
                       distribution=(0.05, 0.7, 0.1, 0.05, 0.1),
                       answer_confidence=0.7, confidence=0.5),
            LayaAnswer(qid="material_risk_mentioned", kind="noul", answer="yes",
                       answer_confidence=0.85, confidence=0.85, yes_probability=0.8),
        ))
        d = evaluate_policy(result=r, policy=_event_policy(),
                            mode="filter", market="us")
        self.assertEqual(d.outcome, "veto")

    def test_sub_threshold_risk_allows(self) -> None:
        r = _event_result(answers=(
            LayaAnswer(qid="event_type", kind="choice", answer="earnings",
                       distribution=(0.7, 0.05, 0.05, 0.05, 0.02, 0.03, 0.05, 0.0),
                       answer_confidence=0.7, confidence=0.5),
            LayaAnswer(qid="directional_tone", kind="choice", answer="positive",
                       distribution=(0.75, 0.05, 0.05, 0.1, 0.05),
                       answer_confidence=0.75, confidence=0.5),
            LayaAnswer(qid="material_risk_mentioned", kind="noul", answer="yes",
                       answer_confidence=0.55, confidence=0.55, yes_probability=0.4),
        ))
        d = evaluate_policy(result=r, policy=_event_policy(),
                            mode="filter", market="us")
        self.assertEqual(d.outcome, "allow")

    def test_truncated_abstains(self) -> None:
        d = evaluate_policy(result=_event_result(truncated=True),
                            policy=_event_policy(), mode="filter", market="us")
        self.assertEqual(d.outcome, "abstain")

    def test_ph_market_abstains(self) -> None:
        d = evaluate_policy(result=_event_result(), policy=_event_policy(),
                            mode="filter", market="ph")
        self.assertEqual(d.outcome, "abstain")


if __name__ == "__main__":
    unittest.main()
