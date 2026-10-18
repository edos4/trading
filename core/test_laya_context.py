"""Laya context + adapter checks (docs/laya.md section 15)."""
from __future__ import annotations

import unittest
from datetime import datetime, timezone


class LayaContextTests(unittest.TestCase):
    def test_future_bars_excluded(self) -> None:
        from core.laya_context import build_candidate_input
        from core.laya_contracts import LayaCandidate

        dec = datetime(2026, 1, 20, 21, 0, tzinfo=timezone.utc)
        bar = datetime(2026, 1, 21, 21, 0, tzinfo=timezone.utc)
        with self.assertRaises(ValueError):
            build_candidate_input(
                assessment_id="asm-x", market="us", symbol="AAPL",
                exchange="NASDAQ", candidate=LayaCandidate(
                    direction="BUY", detector="pattern_003",
                    detector_confidence=0.7),
                decision_at=dec, bar_close_at=bar,
            )

    def test_future_document_rejected(self) -> None:
        from core.laya_context import build_text_input

        with self.assertRaises(ValueError):
            build_text_input(
                assessment_id="asm-x", market="us", symbol="AAPL",
                exchange="NASDAQ", source_text="Some offering news.",
                publication_time="2026-01-10T12:00:00+00:00",
                available_at="2026-01-10T12:05:00+00:00",
                decision_at="2026-01-09T12:00:00+00:00",
            )

    def test_missing_text_rejected(self) -> None:
        from core.laya_context import build_text_input

        with self.assertRaises(ValueError):
            build_text_input(
                assessment_id="asm-x", market="us", symbol="AAPL",
                exchange="NASDAQ", source_text="   ",
            )

    def test_compact_state_marks_unavailable(self) -> None:
        from core.laya_context import build_candidate_input
        from core.laya_contracts import LayaCandidate

        dec = datetime(2026, 1, 20, 21, 0, tzinfo=timezone.utc)
        assessment = build_candidate_input(
            assessment_id="asm-1", market="ph", symbol="SM",
            exchange="PSE",
            candidate=LayaCandidate(direction="BUY", detector="pattern_003",
                                    detector_confidence=0.8),
            decision_at=dec, bar_close_at=dec,
        )
        self.assertIn("unavailable", assessment.context_text)
        self.assertNotIn("no event risk", assessment.context_text.lower())
        # No secret/outcome fields leak into the allowlisted input.
        dumped = assessment.model_dump(mode="json")
        blob = str(dumped)
        for banned in ("password", "pnl", "exit_reason", "credential"):
            self.assertNotIn(banned, blob.lower())

    def test_coalescing_hash_stable_across_identities(self) -> None:
        from core.laya_context import build_text_input, coalescing_hash

        kwargs = dict(market="us", symbol="AAPL", exchange="NASDAQ",
                      source_text="Offering news here.",
                      publication_time="2026-01-10T12:00:00+00:00",
                      decision_at="2026-01-11T12:00:00+00:00")
        a1, _ = build_text_input(assessment_id="asm-1", **kwargs)
        a2, _ = build_text_input(assessment_id="asm-2", **kwargs)
        self.assertEqual(coalescing_hash(a1), coalescing_hash(a2))
        b, _ = build_text_input(assessment_id="asm-3", **{**kwargs, "symbol": "MSFT"})
        self.assertNotEqual(coalescing_hash(a1), coalescing_hash(b))


class LayaAdapterTests(unittest.TestCase):
    def test_yes_probability_vs_answer_confidence(self) -> None:
        """A high-confidence 'no' can carry a low P(yes); both are preserved."""
        from ai.laya_worker import _normalize_router_output

        questions = [{"qid": "material_conflict", "kind": "noul", "text": "t",
                      "options": [], "rubric": []}]
        raw = {"answers": {"material_conflict": {
            "type": "noul", "noul": 0.05,
            "confidence": 0.95, "answer_confidence": 0.95}}}
        answers, _preserved, _w, _t, _c = _normalize_router_output(raw, questions, "candidate-review-v1")
        self.assertEqual(answers[0]["answer"], "no")
        self.assertAlmostEqual(answers[0]["yes_probability"], 0.05)
        self.assertAlmostEqual(answers[0]["answer_confidence"], 0.95)

    def test_malformed_answer_rejected(self) -> None:
        from ai.laya_worker import _normalize_router_output

        questions = [{"qid": "evidence_alignment", "kind": "choice", "text": "t",
                      "options": ["supports", "mixed"], "rubric": []}]
        with self.assertRaises(ValueError):
            _normalize_router_output(
                {"answers": {"evidence_alignment": {
                    "type": "choice", "choice": "moon",
                    "probabilities": {"supports": 0.5, "mixed": 0.5},
                    "answer_confidence": 0.9}}},
                questions, "candidate-review-v1")

    def test_missing_confidence_rejected(self) -> None:
        from ai.laya_worker import _normalize_router_output

        questions = [{"qid": "evidence_alignment", "kind": "choice", "text": "t",
                      "options": ["supports", "mixed"], "rubric": []}]
        with self.assertRaises(ValueError):
            _normalize_router_output(
                {"answers": {"evidence_alignment": {
                    "type": "choice", "choice": "supports",
                    "probabilities": {"supports": 0.9, "mixed": 0.1}}}},
                questions, "candidate-review-v1")


if __name__ == "__main__":
    unittest.main()
