"""Pattern part notes must quote the detector's own measured gates.

The chart prints a reason under each part label ("First bottom (L1)",
"Neckline", …). These tests pin the two properties that matter: the numbers
come from the real detector thresholds, and a missing/unknown pattern degrades
to no note instead of breaking a chart.
"""

from __future__ import annotations

import importlib

import numpy as np
import pandas as pd
import pytest

from patterns._annotations import pattern_annotations
from patterns._rationale import _MODULES, _detector_class, pattern_reasons
from patterns.base_pattern import ann_hline, ann_marker


def frame(n: int = 140) -> pd.DataFrame:
    """A double bottom whose pivots sit at bars 40 / 58 / 78 and entry 97."""
    down = np.linspace(3.0, 2.0, 41)
    up = np.linspace(2.0, 2.6, 19)
    down2 = np.linspace(2.6, 2.2, 21)
    up2 = np.linspace(2.2, 2.5, 20)
    close = np.concatenate([down, up[1:], down2[1:], up2[1:]])
    close = close + 0.01 * ((np.arange(len(close)) % 3) - 1)
    index = pd.bdate_range("2024-01-02", periods=len(close))
    return pd.DataFrame(
        {
            "Open": close * 0.995,
            "High": close * 1.01,
            "Low": close * 0.985,
            "Close": close,
            "Volume": 1_000_000.0,
        },
        index=index,
    )


def pivots(df: pd.DataFrame) -> list[dict]:
    date = lambda i: str(df.index[i].date())
    return [
        ann_marker(date(40), 2.00, "L1", "#ffeb3b", "^", "below"),
        ann_marker(date(58), 2.60, "neckline", "#ffeb3b", "v", "above"),
        ann_marker(date(78), 2.20, "L2", "#ffeb3b", "^", "below"),
        ann_hline(2.60, "neckline", "#ffeb3b"),
        ann_marker(date(97), 2.50, "entry", "#ffeb3b", "o", "below"),
    ]


def test_double_bottom_notes_quote_measured_gates():
    df = frame()
    notes = pattern_reasons("pattern_003_double_bottom", pivots(df), df)
    low1, low2 = float(df["Low"].iloc[40]), float(df["Low"].iloc[78])

    assert "swing low" in notes["L1"]
    assert "30" in notes["L1"]  # L1_RSI_MAX
    assert f"higher low {(low2 - low1) / low1 * 100:+.1f}%" in notes["L2"]
    assert "38 bars apart" in notes["L2"]
    assert f"{(2.60 - low1) / low1 * 100:+.1f}% over L1 low" in notes["neckline"]
    assert notes["entry"]


def test_notes_follow_the_detector_thresholds(monkeypatch):
    """A note must never describe a rule the detector no longer enforces."""
    module = importlib.import_module("patterns.003_double_bottom")
    monkeypatch.setattr(module.DoubleBottomPattern, "L1_RSI_MAX", 12.0)
    monkeypatch.setattr(module.DoubleBottomPattern, "ENTRY_DELAY", 3)

    df = frame()
    notes = pattern_reasons("pattern_003_double_bottom", pivots(df), df)
    assert "12" in notes["L1"]
    assert "day-3" in notes["entry"]


def test_annotations_carry_reasons_and_stay_idempotent():
    df = frame()
    upgraded = pattern_annotations(pivots(df), df, "pattern_003_double_bottom")
    labelled = {a["label"]: a for a in upgraded if a.get("reason")}
    assert "First bottom (L1)" in labelled
    assert "Second bottom (L2)" in labelled
    assert labelled["Neckline"]["reason"]

    again = pattern_annotations(upgraded, df, "pattern_003_double_bottom")
    assert again == upgraded


def test_annotations_without_a_frame_get_no_reason():
    upgraded = pattern_annotations(pivots(frame()), None, "pattern_003_double_bottom")
    assert not any(a.get("reason") for a in upgraded)


def test_unknown_pattern_or_junk_input_is_silent():
    df = frame()
    unknown = [ann_marker(str(df.index[5].date()), 2.0, "mystery", "#ffeb3b")]
    assert pattern_reasons("pattern_999_nothing", unknown, df) == {}
    assert pattern_reasons(None, [], df) == {}
    assert pattern_reasons("pattern_003_double_bottom", pivots(df), None) == {}
    assert pattern_reasons("pattern_003_double_bottom", [{"label": "L1"}], "not a frame") == {}


def test_legacy_annotations_infer_their_pattern_from_labels():
    """Ledgers predating saved pattern ids still get notes."""
    df = frame()
    notes = pattern_reasons(None, pivots(df), df)
    assert "higher low" in notes["L2"]


def test_mixed_signals_never_mislabel_a_part():
    """The explorer can stack signals from several patterns on one chart."""
    df = frame()
    merged = pivots(df) + [
        ann_marker(str(df.index[i].date()), price, label, "#ffeb3b")
        for i, price, label in (
            (10, 2.9, "LS"), (20, 2.5, "LN"), (30, 3.1, "HEAD"),
            (45, 2.55, "RN"), (55, 2.85, "RS"),
        )
    ]
    notes = pattern_reasons(None, merged, df)
    assert "higher low" in notes["L2"]          # double bottom parts intact
    assert "strict swing high" in notes["HEAD"]  # head and shoulders too
    # "entry" exists in both patterns: dropped rather than attributed wrongly.
    assert "entry" not in notes


@pytest.mark.parametrize("pattern,module_path", sorted(_MODULES.items()))
def test_every_reason_pattern_points_at_a_real_detector(pattern, module_path):
    module = importlib.import_module(module_path)
    assert module is not None
    assert pattern in _MODULES


def test_detector_class_lookup_finds_the_pattern_class():
    module = importlib.import_module("patterns.003_double_bottom")
    assert _detector_class(module) is module.DoubleBottomPattern


def test_real_double_bottom_detection_explains_every_part():
    """End to end: manufacture a valid setup, run the detector, read the notes."""
    demos = pytest.importorskip("generate_pattern_demos")
    closes, peaks, troughs = demos.gen_double_bottom()
    volumes = demos.up_down_vol(closes, 89, 120, 1_500_000, 400_000, 1_000_000)
    candles = demos.build_candles(closes, volumes, peaks, troughs)
    snapshot = demos.make_snapshot("REBN", candles, rec="BUY")
    df, signal = demos.run_pattern(demos.DoubleBottomPattern(), "REBN", candles, snapshot)

    assert signal is not None and signal.action == "BUY"
    upgraded = pattern_annotations(signal.chart_annotations, df, signal.pattern)
    notes = {a["label"]: a["reason"] for a in upgraded if a.get("reason")}
    assert "RSI" in notes["First bottom (L1)"]
    assert "higher low" in notes["Second bottom (L2)"]
    assert "highest high" in notes["Neckline"]
