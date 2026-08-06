"""Unit checks for kronos_eval scoring helpers (no Kronos weights)."""

from __future__ import annotations

import math

import pandas as pd

from core.kronos_eval import (
    WindowResult,
    _score,
    _score_persistence,
    bootstrap_ci,
    majority_sign_baseline,
    score_gate_rule,
)


def _r(pred: float, actual: float, persist: float = float("nan"), **kw) -> WindowResult:
    base = dict(
        symbol="T",
        asof=pd.Timestamp("2026-06-01"),
        actual_1d=0.0,
        pred_1d=0.0,
        actual_1w=actual,
        pred_1w=pred,
        persist_1w=persist,
    )
    base.update(kw)
    return WindowResult(**base)


def test_score_and_signed_return():
    rows = [_r(0.1, 0.08), _r(-0.1, -0.05), _r(0.1, -0.02)]
    s = _score(rows, "actual_1w", "pred_1w")
    assert s["n"] == 3
    # hits: +/+, -/-, +/- → 2/3
    assert abs(s["directional_accuracy_pct"] - 200 / 3) < 1e-6
    # signed: +0.08 + 0.05 + (-0.02) = 0.11 / 3
    assert abs(s["signed_return_pct"] - (0.11 / 3) * 100) < 1e-6


def test_gate_rule_filters_min_move():
    rows = [
        _r(0.01, 0.02),   # below 6%
        _r(0.10, 0.08),   # clear, hit
        _r(-0.10, 0.05),  # clear, miss
        _r(-0.07, -0.04), # clear, hit
    ]
    g = score_gate_rule(rows, min_move=0.06)
    assert g["n"] == 3
    assert g["n_all"] == 4
    assert abs(g["coverage_pct"] - 75.0) < 1e-9
    assert g["n_buy"] == 1
    assert g["n_sell"] == 2
    assert abs(g["directional_accuracy_pct"] - 200 / 3) < 1e-6
    assert abs(g["buy_dir_hit_pct"] - 100.0) < 1e-9
    assert abs(g["sell_dir_hit_pct"] - 50.0) < 1e-9


def test_persistence_and_majority():
    rows = [
        _r(0.1, 0.05, persist=0.04),
        _r(-0.1, -0.03, persist=-0.02),
        _r(0.05, 0.01, persist=0.10),
    ]
    p = _score_persistence(rows)
    assert p["n"] == 3
    m = majority_sign_baseline(rows)  # actuals mostly +
    assert m["majority_sign"] == 1
    assert m["directional_accuracy_pct"] > 50


def test_bootstrap_ci_shape():
    rows = [_r(0.1 if i % 2 == 0 else -0.1, 0.05 if i % 3 else -0.04) for i in range(30)]
    ci = bootstrap_ci(rows, n_boot=200, seed=1)
    assert ci["n"] == 30
    lo, mid, hi = ci["dir_acc_pct_ci"]
    assert lo <= mid <= hi
    gci = bootstrap_ci(rows, n_boot=200, seed=1, min_move=0.05)
    assert gci["n_boot_kept"] > 0
    assert not math.isnan(gci["mae_pct_ci"][1])


if __name__ == "__main__":
    test_score_and_signed_return()
    test_gate_rule_filters_min_move()
    test_persistence_and_majority()
    test_bootstrap_ci_shape()
    print("kronos_eval scoring tests OK")
