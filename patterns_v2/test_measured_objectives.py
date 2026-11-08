"""A pattern's objective stays fixed as the confirmation price changes."""

from __future__ import annotations

import pandas as pd
import pytest

from patterns_v2.signals import _levels, detect_trade_signals, signals_from_raw
from patterns_v2.test_family_causality import _FAMILIES, family_bars


def reversal(short: bool) -> dict:
    return {"pattern": "double_top" if short else "double_bottom",
            "direction": -1 if short else 1, "level": 100.,
            "start": 10, "end": 40, "signal": 59, "score": 5.,
            "points": [(10, 120. if short else 80., "high 1" if short else "low 1")],
            "lines": []}


@pytest.mark.parametrize("short", [False, True])
def test_confirmation_overshoot_consumes_reward_without_extending_objective(short):
    raw = reversal(short)
    direction = raw["direction"]
    for overshoot in (1., 5., 19.):
        stop, target = _levels(raw, 100 + direction * overshoot)
        assert stop == (120 if short else 80)
        assert target == (80 if short else 120)
    for overshoot in (20., 21.):
        assert _levels(raw, 100 + direction * overshoot) == (None, None)
    assert _levels(raw, 100 - direction) == (None, None)


@pytest.mark.parametrize("short", [False, True])
@pytest.mark.parametrize("family", ["flag", "cup", "triangle"])
def test_measured_objective_is_symmetric_across_pattern_families(short, family):
    raw = reversal(short)
    direction = raw["direction"]
    if family == "flag":
        raw.update(pattern="bear_flag" if short else "bull_flag",
                   points=[(10, 100., "pole"), (20, 100 + direction * 20, "pole bottom" if short else "pole top")],
                   lines=[(40, 100., 59, 100.), (40, 100 - direction * 10, 59, 100 - direction * 10)])
    elif family == "cup":
        raw.update(pattern="inv_cup_handle" if short else "cup_handle",
                   points=[(20, 100 - direction * 20, "cup"),
                           (40, 100 - direction * 10, "handle")])
    else:
        raw.update(pattern="symmetrical_triangle", points=[],
                   lines=[(10, 100., 59, 100.), (10, 100 - direction * 20, 59, 100 - direction * 10)])
    stop, target = _levels(raw, 100 + direction * 5)
    assert stop == 100 - direction * 10
    assert target == 100 + direction * 20


@pytest.mark.parametrize("direction", [0, 2, -2, None])
def test_missing_or_ambiguous_direction_does_not_default_to_short(direction):
    assert _levels({**reversal(False), "direction": direction}, 105) == (None, None)


def test_unusable_high_score_does_not_hide_an_executable_same_bar_pattern(monkeypatch):
    monkeypatch.setattr("patterns_v2.signals._muted_names", lambda: frozenset())
    frame = pd.DataFrame({"close": [105.] * 60, "high": [106.] * 60, "low": [104.] * 60},
                         index=pd.bdate_range("2026-01-01", periods=60))
    good = reversal(False)
    # This shallow candidate has already completed its entire measured move.
    bad = {**good, "score": 10., "points": [(10, 99., "low 1")]}
    hits = signals_from_raw(frame, [bad, good], symbol="TEST", timeframe="1d", latest_bar_only=True)
    assert len(hits) == 1
    assert hits[0].stop_loss == 80
    assert hits[0].take_profit == 120


def test_production_detector_preserves_candidates_until_objectives_are_validated(monkeypatch):
    from patterns_v2 import patterns as detectors

    good = reversal(False)
    bad = {**good, "score": 10., "points": [(10, 99., "low 1")]}
    monkeypatch.setattr(detectors, "DISABLED", frozenset())
    monkeypatch.setattr("patterns_v2.signals._muted_names", lambda: frozenset())
    monkeypatch.setattr(detectors, "_BULLISH_DETECTORS", (
        lambda _h, _l, c, *_args: [bad, good] if c[-1] > 0 else [],))
    monkeypatch.setattr(detectors, "detect_triangles", lambda *args, **kwargs: [])
    frame = pd.DataFrame({"close": [105.] * 60, "high": [106.] * 60, "low": [104.] * 60},
                         index=pd.bdate_range("2026-01-01", periods=60))
    assert detectors.detect(frame.high, frame.low, frame.close) == [bad]
    hits = detect_trade_signals(frame, symbol="TEST", timeframe="1d", latest_bar_only=True,
                                detect=detectors.detect)
    assert len(hits) == 1
    assert hits[0].take_profit == 120


@pytest.mark.parametrize("family", sorted(_FAMILIES))
def test_all_candidate_mode_retains_causal_latest_bar_results(family):
    from patterns_v2.patterns import detect

    high, low, close = family_bars(family)
    full = detect(high, low, close, all_candidates=True)
    for n in range(60, len(close) + 1):
        current = detect(high[:n], low[:n], close[:n], latest=n-1, all_candidates=True)
        assert [s for s in current if s["signal"] == n-1] == [s for s in full if s["signal"] == n-1]
