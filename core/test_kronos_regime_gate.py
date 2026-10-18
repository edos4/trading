"""Unit checks for the regime gate (CPU only, no weights, no GPU)."""

from __future__ import annotations

import math

from core.kronos_regime_gate import (
    GateDecision,
    evaluate,
    universe_trend,
)


def test_trend_mean_and_empty():
    assert abs(universe_trend([0.01, -0.01, 0.02]) - (0.02 / 3)) < 1e-12
    assert math.isnan(universe_trend([]))


def test_confirm_short_needs_magnitude_and_strong_tape():
    r = evaluate(-0.06, 0.02)
    assert r.decision is GateDecision.CONFIRM_SHORT
    assert "fade strength" in r.reason


def test_weak_magnitude_has_no_opinion():
    r = evaluate(-0.01, 0.05)
    assert r.decision is GateDecision.NO_OPINION
    assert "floor" in r.reason


def test_weak_tape_stands_down():
    r = evaluate(-0.08, -0.01)
    assert r.decision is GateDecision.NO_OPINION
    assert "weak tape" in r.reason


def test_long_predictions_never_confirmed():
    for pred in (0.0, 0.01, 0.10):
        r = evaluate(pred, 0.03)
        assert r.decision is GateDecision.NO_OPINION, pred


def test_nan_inputs_have_no_opinion():
    assert evaluate(float("nan"), 0.02).decision is GateDecision.NO_OPINION
    assert evaluate(-0.05, float("nan")).decision is GateDecision.NO_OPINION


def test_strong_tape_can_be_relaxed():
    r = evaluate(-0.05, -0.01, require_strong_tape=False)
    assert r.decision is GateDecision.CONFIRM_SHORT


if __name__ == "__main__":
    test_trend_mean_and_empty()
    test_confirm_short_needs_magnitude_and_strong_tape()
    test_weak_magnitude_has_no_opinion()
    test_weak_tape_stands_down()
    test_long_predictions_never_confirmed()
    test_nan_inputs_have_no_opinion()
    test_strong_tape_can_be_relaxed()
    print("kronos_regime_gate tests OK")
