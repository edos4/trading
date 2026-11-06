"""Audit accounting and volume selection cannot use future information."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from analysis.patternsv2_audit import AuditPolicy, forward_return, render_markdown, run_audit
from patterns_v2.test_double_bottom import _bars


@pytest.mark.parametrize("direction", [1, -1])
def test_forward_returns_use_next_open_and_charge_both_legs(direction):
    policy = AuditPolicy(cost_per_side=.001, slippage_per_side=.0005)
    opening = np.array([50., 100., 130.])
    close = np.array([50., 110., 120.])
    got = forward_return(opening, close, 0, direction, 2, policy)
    entry = 100 * (1 + direction * .0005)
    exit_fill = 120 * (1 - direction * .0005)
    assert got["gross"] == pytest.approx(direction * .20)
    assert got["net"] == pytest.approx((direction * (exit_fill-entry) - .001 * (entry+exit_fill))/entry)


def test_unmatured_or_invalid_forward_returns_are_not_zero_filled():
    policy = AuditPolicy()
    assert forward_return(np.array([1.]), np.array([1.]), 0, 1, 5, policy) is None
    assert forward_return(np.array([1., np.nan]), np.array([1., 2.]), 0, 1, 1, policy) is None


@pytest.mark.parametrize("kwargs", [{"cost_per_side": -1}, {"slippage_per_side": float("nan")},
                                    {"horizons": (0,)}, {"rvol_min": -1}, {"obv_bars": 0}])
def test_invalid_audit_policy_is_rejected(kwargs):
    with pytest.raises(ValueError):
        AuditPolicy(**kwargs)


def _write_fixture(root: Path, future_volume: float = 1000) -> None:
    h, l, c = _bars([0,20,35,50,65,79,110], [100,140,90,110,91,115,118])
    rows = [{"t": 1704067200 + i*86400, "o": float(cc), "h": float(hh), "l": float(ll),
             "c": float(cc), "v": 1000. if i < 90 else future_volume}
            for i, (hh, ll, cc) in enumerate(zip(h, l, c))]
    (root / "TEST.json").write_text(json.dumps({"symbol": "TEST", "bars": rows}))


def test_audit_is_reproducible_and_volume_gate_only_sees_signal_prefix(tmp_path, monkeypatch):
    from analysis import patternsv2_audit as audit

    _write_fixture(tmp_path)
    calls = []
    production_gate = audit.volume_confirm_gate

    def observe(signal, store, **kwargs):
        assert len(store.frame) == signal.signal_bar_idx + 1
        calls.append(len(store.frame))
        return production_gate(signal, store, **kwargs)

    monkeypatch.setattr(audit, "volume_confirm_gate", observe)
    report = run_audit(tmp_path, check_prefixes=True)
    assert report == run_audit(tmp_path, check_prefixes=True)
    assert calls and min(calls) < 90
    assert report["prefix_verification"]["checks"] == 52
    assert not report["prefix_verification"]["mismatches"]
    assert report["counts"]["double_bottom"]["converted"] > 0
    assert "Matured" in render_markdown(report)
    assert render_markdown(report) == render_markdown(json.loads(json.dumps(report, sort_keys=True)))
    json.dumps(report, allow_nan=False)
    _write_fixture(tmp_path, future_volume=1e12)
    changed = run_audit(tmp_path)
    earlier = next(e for e in report["events"] if e["pattern"] == "double_bottom")
    after = next(e for e in changed["events"] if e["pattern"] == "double_bottom")
    assert (earlier["rvol"], earlier["obv_slope"], earlier["volume_passed"]) == (
        after["rvol"], after["obv_slope"], after["volume_passed"])
