"""Why-is-this-part text for a saved detection's chart anchors.

A detector decides every gate before it emits a signal; the chart only keeps
the anchor dates/prices. This module re-derives the measured values from the
same frame so the viewers can print, under each part label, the conditions that
part satisfied (for example *"higher low +7.1%, RSI 43 (39-50), 9 bars after
L1"* under "Second bottom (L2)").

Advisory text only: detection is never decided here, and any failure to build a
reason degrades to "no note" rather than breaking a chart. Thresholds are read
from the detector modules themselves so a note can never describe a rule the
detector no longer enforces.
"""

from __future__ import annotations

import importlib
from typing import Callable

import numpy as np
import pandas as pd

_OHLCV = ("open", "high", "low", "close", "volume")

# pattern id -> module holding its thresholds. Channels keep theirs in the
# shared `_channels` helper rather than on the pattern class.
_MODULES = {
    "pattern_002_double_top": "patterns.002_double_top",
    "pattern_003_double_bottom": "patterns.003_double_bottom",
    "pattern_004_rounding_bottom": "patterns.004_rounding_bottom",
    "pattern_005_rounding_top": "patterns.005_rounding_top",
    "pattern_006_upward_channel": "patterns._channels",
    "pattern_007_descending_channel": "patterns._channels",
    "pattern_008_head_and_shoulders": "patterns.008_head_and_shoulders",
    "pattern_009_flag_pattern": "patterns.009_flag_pattern",
    "pattern_010_pennant": "patterns.010_pennant",
    "pattern_011_breakout_retest": "patterns.011_breakout_retest",
}

# Ordered label signatures for annotations that predate a saved pattern id.
_SIGNATURES = (
    ("pattern_008_head_and_shoulders", {"LS", "HEAD", "RS"}),
    ("pattern_003_double_bottom", {"L1", "L2"}),
    ("pattern_002_double_top", {"H1", "H2"}),
    ("pattern_006_upward_channel", {"SH1", "SH2"}),
    ("pattern_007_descending_channel", {"SL1", "SL2"}),
    ("pattern_009_flag_pattern", {"pole high"}),
    ("pattern_010_pennant", {"pole"}),
    ("pattern_011_breakout_retest", {"breakout", "retest"}),
    ("pattern_004_rounding_bottom", {"bottom", "Left rim"}),
    ("pattern_005_rounding_top", {"top", "Left rim"}),
    # Reconstructed legacy curves keep only their fit label.
    ("pattern_004_rounding_bottom", {"Rounding bottom fit"}),
    ("pattern_005_rounding_top", {"Rounding top fit"}),
)


# ── Public API ────────────────────────────────────────────────────────────────
def pattern_reasons(
    pattern: str | None, annotations: list[dict] | None, df: pd.DataFrame | None,
) -> dict[str, str]:
    """``{part label: why it is that part}`` for one saved detection.

    Keys are the detector's raw labels ("L1") *and* their display labels
    ("First bottom (L1)") so callers can look up either form.
    """
    try:
        return _reasons(pattern, annotations, df)
    except Exception:  # a chart note must never break a chart
        return {}


# ── Frame / anchor plumbing ───────────────────────────────────────────────────
def _ohlcv(df) -> pd.DataFrame | None:
    if df is None or not hasattr(df, "columns") or len(df) < 3:
        return None
    columns = {str(name).lower(): name for name in df.columns}
    if not set(_OHLCV) <= set(columns):
        return None
    frame = df.rename(columns={source: lower for lower, source in columns.items()})
    return frame.loc[:, list(_OHLCV)]


def _date_key(value) -> str | None:
    try:
        stamp = pd.Timestamp(value)
    except (TypeError, ValueError):
        return None
    if stamp.tzinfo is not None:
        stamp = stamp.tz_convert("UTC")
    return stamp.strftime("%Y-%m-%d")


def _bar_index(frame: pd.DataFrame) -> dict[str, int]:
    if not isinstance(frame.index, pd.DatetimeIndex):
        return {}
    stamps = frame.index.normalize()
    if stamps.tz is not None:
        stamps = stamps.tz_convert("UTC").tz_localize(None)
    # Later bars win, matching the renderer's "keep the last bar per session".
    return {stamp.strftime("%Y-%m-%d"): i for i, stamp in enumerate(stamps)}


def _display_labels() -> dict[str, str]:
    try:
        from patterns._annotations import PART_LABELS
        return PART_LABELS
    except Exception:
        return {}


def _detector_class(module):
    """The BasePattern subclass defined by a pattern module, if any."""
    from patterns.base_pattern import BasePattern
    for value in vars(module).values():
        if isinstance(value, type) and issubclass(value, BasePattern) and value is not BasePattern:
            return value
    return None


class _Ctx:
    """Anchor + frame lookups shared by every reason builder."""

    def __init__(self, pattern: str, frame: pd.DataFrame, anchors: dict, bars: dict):
        self.pattern = pattern
        self.frame = frame
        self.anchors = anchors
        self.bars = bars
        self._module = None
        self._ind = None
        self._rsi = None

    @property
    def module(self):
        if self._module is None:
            name = _MODULES.get(self.pattern)
            self._module = importlib.import_module(name) if name else False
        return self._module or None

    @property
    def ind(self):
        if self._ind is None:
            from analysis.indicator_engine import IndicatorEngine
            self._ind = IndicatorEngine(self.frame)
        return self._ind

    @property
    def rsi(self):
        if self._rsi is None:
            self._rsi = self.ind.rsi_wilder(14)
        return self._rsi

    @property
    def last(self) -> int:
        return len(self.frame) - 1

    def constant(self, name: str, default: float) -> float:
        """A detector threshold, read from the module or its class itself."""
        module = self.module
        if not module:
            return float(default)
        value = getattr(module, name, None)
        if value is None:
            detector = _detector_class(module)
            value = getattr(detector, name, None) if detector else None
        return float(value) if value is not None else float(default)

    def bar(self, label: str) -> int | None:
        ann = self.anchors.get(label)
        if not ann:
            return None
        key = _date_key(ann.get("date") or ann.get("start_date"))
        bar = self.bars.get(key) if key else None
        return bar

    def price(self, label: str, field: str = "price") -> float | None:
        ann = self.anchors.get(label)
        if not ann:
            return None
        try:
            return float(ann.get(field))
        except (TypeError, ValueError):
            return None

    def high(self, i: int) -> float:
        return float(self.ind.high.iloc[i])

    def low(self, i: int) -> float:
        return float(self.ind.low.iloc[i])

    def close(self, i: int) -> float:
        return float(self.ind.close.iloc[i])

    def open(self, i: int) -> float:
        return float(self.ind.open.iloc[i])

    def volume(self, i: int) -> float:
        return float(self.ind.volume.iloc[i])

    def rsi_at(self, i: int) -> float | None:
        value = float(self.rsi.iloc[i])
        return value if np.isfinite(value) else None

    def mean_volume(self, start: int, stop: int) -> float | None:
        if stop < start or start < 0:
            return None
        window = self.ind.volume.iloc[start:stop + 1]
        return float(window.mean()) if len(window) else None

    def first_close(self, start: int, stop: int, *, above=None, below=None) -> int | None:
        for i in range(max(0, start), min(stop, self.last) + 1):
            close = self.close(i)
            if above is not None and close > above:
                return i
            if below is not None and close < below:
                return i
        return None


# ── Formatting ────────────────────────────────────────────────────────────────
def _price(value: float) -> str:
    return f"{value:.4f}" if abs(value) < 1 else f"{value:,.2f}"


def _move(value: float, base: float) -> float:
    return (value - base) / base * 100.0 if base else 0.0


def _band(low: float, high: float) -> str:
    return f"{low:.0f}\u2013{high:.0f}"


_BUILDERS: dict[str, Callable[[_Ctx], dict[str, str]]] = {}


def _builder(*names: str):
    def register(fn):
        for name in names:
            _BUILDERS[name] = fn
        return fn
    return register


# ── Double top / double bottom ────────────────────────────────────────────────
@_builder("pattern_003_double_bottom")
def _double_bottom(c: _Ctx) -> dict[str, str]:
    l1, l2 = c.bar("L1"), c.bar("L2")
    if l1 is None or l2 is None:
        return {}
    low1, low2 = c.low(l1), c.low(l2)
    rsi1, rsi2 = c.rsi_at(l1), c.rsi_at(l2)
    neck = c.price("neckline") or c.high(max(l1, 0))
    gap, gap_min, gap_max = l2 - l1, c.constant("GAP_MIN", 8), c.constant("GAP_MAX", 90)
    rsi_max = c.constant("L1_RSI_MAX", 30.0)
    rsi_lo, rsi_hi = c.constant("L2_RSI_MIN", 39.0), c.constant("L2_RSI_MAX", 50.0)
    divergence = c.constant("RSI_DIVERGENCE_MIN", 3.0)
    height = c.constant("PEAK_HEIGHT_MIN", 0.05) * 100.0
    oversold = (
        f", RSI {rsi1:.0f} \u2264 {rsi_max:.0f} (oversold)" if rsi1 is not None else ""
    )
    oscillator = (
        f"RSI {rsi2:.0f} in {_band(rsi_lo, rsi_hi)}" if rsi2 is not None
        else f"RSI below {rsi_hi:.0f}"
    )
    if rsi1 is not None and rsi2 is not None:
        oscillator += f", {rsi2 - rsi1:+.0f} vs L1 (divergence \u2265 {divergence:.0f})"
    notes = {
        "L1": f"swing low 2 bars either side{oversold}",
        "L2": (
            f"higher low {_move(low2, low1):+.1f}% and higher close; {oscillator}; "
            f"{gap} bars apart ({gap_min:.0f}\u2013{gap_max:.0f})"
        ),
        "neckline": (
            f"highest high between the lows, {_move(neck, low1):+.1f}% over L1 low "
            f"(\u2265 {height:.0f}%)"
        ),
    }
    entry = c.bar("entry")
    if entry is not None and neck:
        delay = int(c.constant("ENTRY_DELAY", 7))
        break_bar = c.first_close(l2 + 1, entry, above=neck)
        if break_bar is not None and break_bar - l2 < delay:
            notes["entry"] = (
                f"neckline {_price(neck)} cleared {break_bar - l2} bars after L2 "
                f"-> long before day {delay}"
            )
        else:
            notes["entry"] = (
                f"day-{delay} entry after L2; close {_price(c.close(entry))} "
                f"vs neckline {_price(neck)}"
            )
    notes["target"] = f"neckline +{c.constant('TARGET_ABOVE_NECKLINE', 0.07):.0%} measured move"
    return notes


@_builder("pattern_002_double_top")
def _double_top(c: _Ctx) -> dict[str, str]:
    h1, h2 = c.bar("H1"), c.bar("H2")
    if h1 is None or h2 is None:
        return {}
    high1, high2 = c.high(h1), c.high(h2)
    rsi1, rsi2 = c.rsi_at(h1), c.rsi_at(h2)
    neck = c.price("neckline") or c.price("valley")
    gap = h2 - h1
    gap_min, gap_max = c.constant("GAP_MIN", 8), c.constant("GAP_MAX", 90)
    h1_min = c.constant("H1_RSI_MIN", 70.0)
    rsi_lo, rsi_hi = c.constant("H2_RSI_MIN", 50.0), c.constant("H2_RSI_MAX", 61.0)
    divergence = c.constant("RSI_DIV_MIN", 3.0)
    depth = c.constant("VALLEY_DEPTH_MIN", 0.05) * 100.0
    overbought = (
        f", RSI {rsi1:.0f} \u2265 {h1_min:.0f} (overbought)" if rsi1 is not None else ""
    )
    oscillator = (
        f"RSI {rsi2:.0f} in {_band(rsi_lo, rsi_hi)}" if rsi2 is not None
        else f"RSI below {rsi_hi:.0f}"
    )
    if rsi1 is not None and rsi2 is not None:
        oscillator += f", {rsi2 - rsi1:+.0f} vs H1"
    notes = {
        "H1": f"strict 3-bar swing high{overbought}",
        "H2": (
            f"lower high {_move(high2, high1):+.1f}% and weaker close; {oscillator}; "
            f"{gap} bars apart ({gap_min:.0f}\u2013{gap_max:.0f})"
        ),
    }
    if neck:
        notes["valley"] = (
            f"deepest low after H1; neckline {_price(neck)}, "
            f"{_move(high1, neck):.1f}% below H1 (\u2265 {depth:.0f}%)"
        )
        notes["neckline"] = f"neckline = deepest valley low {_price(neck)}, break on a close below"
    if rsi1 is not None and rsi2 is not None and rsi1 - rsi2 >= divergence:
        notes["H2"] += f", divergence \u2265 {divergence:.0f}"
    entry = c.bar("entry")
    if entry is not None and neck:
        delay = int(c.constant("ENTRY_DELAY", 7))
        break_bar = c.first_close(h2 + 1, entry, below=neck)
        if break_bar is not None and break_bar - h2 < delay:
            notes["entry"] = (
                f"neckline {_price(neck)} broken {break_bar - h2} bars after H2 "
                f"-> short before day {delay}"
            )
        else:
            notes["entry"] = f"day-{delay} short after H2 (neckline {_price(neck)})"
    notes["target"] = f"neckline \u2212{c.constant('TARGET_BELOW_NECKLINE', 0.10):.0%} measured move"
    return notes


# ── Rounding bottom / top ─────────────────────────────────────────────────────
@_builder("pattern_004_rounding_bottom", "pattern_005_rounding_top")
def _rounding(c: _Ctx) -> dict[str, str]:
    top = c.pattern.endswith("rounding_top")
    apex_label = "top" if top else "bottom"
    center = c.bar(apex_label)
    rim = c.price("Left rim")
    center_close = c.price(apex_label)
    notes: dict[str, str] = {}
    if center is not None:
        notes[f"Rounding {apex_label} fit"] = _rounding_fit(c, center, top)
        if center_close is not None:
            rsi_limit = 55.0 if top else 45.0
            center_rsi = c.rsi_at(center)
            extreme = "highest" if top else "lowest"
            notes[apex_label] = (
                f"{extreme} close of the cup; RSI {center_rsi:.0f} "
                f"{'>' if top else '<'} {rsi_limit:.0f}"
                if center_rsi is not None else f"{extreme} close of the whole cup"
            )
    if rim is not None and center_close is not None:
        depth = abs(_move(center_close, rim))
        notes["Left rim"] = f"cup rim close {_price(rim)}; depth {depth:.0f}% (15\u201350%)"
        notes["neckline"] = (
            f"rim {_price(rim)}; target = 80% retrace of the {depth:.0f}% cup"
        )
    entry = c.bar("entry")
    if entry is not None:
        target = c.price("target")
        reward = abs(_move(target, c.close(entry))) if target else None
        notes["entry"] = (
            f"2-day higher-{'low' if top else 'high'}/"
            f"{'lower-high' if top else 'higher-low'} trigger"
            + (f"; reward {reward:.0f}% \u2265 23%" if reward is not None else "")
        )
    return notes


def _rounding_fit(c: _Ctx, center: int, top: bool) -> str:
    try:
        from patterns._rules import parabolic_fit
        shape = parabolic_fit(c.ind.close, center, 60, 0.05)
    except Exception:
        shape = None
    if shape is None:
        return f"quadratic fit over \u00b160 bars, {'concave down' if top else 'concave up'}"
    coefficient, within = shape
    direction = "concave down" if coefficient < 0 else "concave up"
    return f"quadratic {direction} over \u00b160 bars, {within:.0%} of closes within 5% (\u2265 70%)"


# ── Channels ──────────────────────────────────────────────────────────────────
@_builder("pattern_006_upward_channel")
def _upward_channel(c: _Ctx) -> dict[str, str]:
    return _channel(c, up=True)


@_builder("pattern_007_descending_channel")
def _descending_channel(c: _Ctx) -> dict[str, str]:
    return _channel(c, up=False)


def _channel(c: _Ctx, *, up: bool) -> dict[str, str]:
    first_label, second_label = ("SH1", "SH2") if up else ("SL1", "SL2")
    turn_label = "valley" if up else "peak"
    first, second, turn = c.bar(first_label), c.bar(second_label), c.bar(turn_label)
    if None in (first, second, turn):
        return {}
    first_price = c.price(first_label) or (c.high(first) if up else c.low(first))
    second_price = c.price(second_label) or (c.high(second) if up else c.low(second))
    turn_price = c.price(turn_label) or (c.low(turn) if up else c.high(turn))
    slope = (second_price - first_price) / (second - first) if second != first else 0.0
    width = abs(first_price - (turn_price - slope * (turn - first)))
    lb = c.constant("LB", 3)
    first_rsi_min = c.constant("SH1_RSI_MIN", 55.0)
    above = c.constant("SH2_ABOVE", 1.02)
    rsi_lo, rsi_hi = c.constant("SH2_RSI_MIN", 35.0), c.constant("SH2_RSI_MAX", 75.0)
    divergence = c.constant("RSI_DIV_MIN", 5.0)
    valley_lo, valley_hi = c.constant("VALLEY_MIN", 0.02) * 100, c.constant("VALLEY_MAX", 0.25) * 100
    confirm = int(c.constant("NECK_CONFIRM", 2))
    freshness = int(c.constant("MAX_DAYS_TO_BREAK", 20))
    rsi1, rsi2 = c.rsi_at(first), c.rsi_at(second)
    first_note = f"{lb:.0f}-bar swing {'high' if up else 'low'}"
    if rsi1 is not None:
        first_note += f", RSI {rsi1:.0f} \u2265 {first_rsi_min:.0f}"
    second_note = (
        f"{'higher high' if up else 'lower low'} {_move(second_price, first_price):+.1f}% "
        f"(\u2265 {(above - 1) * 100:.0f}%)"
    )
    if rsi2 is not None:
        second_note += f", RSI {rsi2:.0f} in {_band(rsi_lo, rsi_hi)}"
        if rsi1 is not None:
            second_note += f", {rsi2 - rsi1:+.0f} vs {first_label}"
            if abs(rsi2 - rsi1) >= divergence:
                second_note += f" (divergence \u2265 {divergence:.0f})"
    notes = {
        first_label: first_note,
        second_label: second_note,
        turn_label: (
            f"pullback {abs(_move(turn_price, second_price)):.1f}% "
            f"(from {first_label}; rail depth {valley_lo:.0f}\u2013{valley_hi:.0f}% of price)"
        ),
        "Upper channel": f"rail through {first_label}->{second_label}, slope {slope:+.3f}/bar",
        "Lower channel": f"parallel rail through the {turn_label}, width {_price(width)}",
    }
    start = c.bar("start")
    if start is not None:
        notes["start"] = f"channel origin close {_price(c.close(start))}"
    entry = c.bar("entry")
    if entry is not None:
        side = "below the lower" if up else "above the upper"
        notes["entry"] = (
            f"{confirm} closes {side} rail, {entry - second} bars after {second_label} "
            f"(\u2264 {freshness})"
        )
    return notes


# ── Head and shoulders ────────────────────────────────────────────────────────
@_builder("pattern_008_head_and_shoulders")
def _head_and_shoulders(c: _Ctx) -> dict[str, str]:
    ls, ln = c.bar("LS"), c.bar("LN")
    head, rn, rs = c.bar("HEAD"), c.bar("RN"), c.bar("RS")
    if None in (ls, ln, head, rn, rs):
        return {}
    ls_close, head_close = c.close(ls), c.close(head)
    ln_close, rn_close, rs_close = c.close(ln), c.close(rn), c.close(rs)
    neck = (ln_close + rn_close) / 2.0
    ls_rsi, head_rsi, rs_rsi = c.rsi_at(ls), c.rsi_at(head), c.rsi_at(rs)
    shoulder_rsi = (
        f", RSI {ls_rsi:.0f} \u2265 head {head_rsi:.0f} + 2"
        if ls_rsi is not None and head_rsi is not None
        else ", RSI above the head's"
    )
    right_rsi = (
        f", RSI {rs_rsi:.0f} \u2264 60 and below the head"
        if rs_rsi is not None else ", RSI below the head's"
    )
    notes = {
        "LS": f"highest local-max close below the head{shoulder_rsi}",
        "LN": f"deepest close after the left shoulder, {_move(ls_close, ln_close):.1f}% below LS (\u2265 5%)",
        "HEAD": f"highest strict swing high, {_move(head_close, neck):.0f}% above the neckline (\u2265 10%)",
        "RN": (
            f"deepest close in the 60 bars after the head; skew {_move(rn_close, ln_close) / 2:.1f}% "
            f"(\u2264 \u00b110/30%)"
        ),
        "RS": (
            f"lower high above the neckline{right_rsi}; {rs - ls} bars from LS (20\u2013120)"
        ),
        "Neckline": f"flat neckline = mean(LN {_price(ln_close)}, RN {_price(rn_close)})",
    }
    entry = c.bar("entry")
    if entry is not None:
        notes["entry"] = f"2nd consecutive close below neckline {_price(neck)} -> short"
    return notes


# ── Flag ──────────────────────────────────────────────────────────────────────
@_builder("pattern_009_flag_pattern")
def _flag(c: _Ctx) -> dict[str, str]:
    pole_start, pole_end = c.bar("pole start"), c.bar("pole high")
    flag_low = c.price("flag low")
    if None in (pole_start, pole_end) or flag_low is None:
        return {}
    pole_start_price = c.open(pole_start)
    pole_end_price = c.close(pole_end)
    gain = _move(pole_end_price, pole_start_price)
    pole_volume = c.mean_volume(pole_start, pole_end)
    baseline = c.mean_volume(max(0, pole_start - 20), pole_start - 1)
    volume_x = (pole_volume / baseline) if pole_volume and baseline else None
    retrace = abs(_move(flag_low, pole_end_price))
    span = max(1, pole_end - pole_start + 1)
    high = c.price("Flag resistance") or c.price("Flag resistance", "start_price")
    notes = {
        "pole start": f"pole opens at {_price(pole_start_price)}, {span} bars before the top",
        "pole high": (
            f"pole top {_price(pole_end_price)}, {gain:+.0f}% (\u2265 25%)"
            + (f" on {volume_x:.1f}x baseline volume (\u2265 1.15x)" if volume_x else "")
        ),
        "Pole": (
            f"{gain:+.0f}% pole over {span} bars"
            + (f" on {volume_x:.1f}x baseline volume" if volume_x else "")
        ),
        "flag low": f"flag floor over the consolidation; {retrace:.0f}% pullback (10\u201334%)",
    }
    if high:
        notes["Flag resistance"] = f"consolidation high {_price(high)}; breakout reference"
        notes["Flag support"] = f"consolidation low {_price(flag_low)}; stop reference"
    entry = c.bar("entry")
    if entry is not None:
        flag_start = c.bar("Flag resistance") or entry - 1
        coil_volume = c.mean_volume(flag_start, max(flag_start, entry - 1))
        volume_ratio = c.volume(entry) / coil_volume if coil_volume else None
        notes["Breakout"] = (
            f"breakout close {_price(c.close(entry))} above the flag high"
            + (f" on {volume_ratio:.1f}x flag volume" if volume_ratio else "")
        )
        notes["entry"] = (
            f"first close above the flag high"
            + (f" {_price(high)}" if high else "")
            + (f" on {volume_ratio:.1f}x volume" if volume_ratio else "")
        )
    return notes


# ── Pennant ───────────────────────────────────────────────────────────────────
@_builder("pattern_010_pennant")
def _pennant(c: _Ctx) -> dict[str, str]:
    pole_start, pole_end = c.bar("pole start"), c.bar("pole")
    if pole_start is None or pole_end is None:
        return {}
    start_price, end_price = c.close(pole_start), c.close(pole_end)
    pole_return = _move(end_price, start_price)
    span = max(1, pole_end - pole_start + 1)
    pole_volume = c.mean_volume(pole_start, pole_end)
    baseline = c.mean_volume(max(0, pole_start - 21), pole_start - 1)
    volume_x = (pole_volume / baseline) if pole_volume and baseline else None
    bull = end_price >= start_price
    notes = {
        "pole start": f"pole starts at {_price(start_price)}, {span} bars before the coil",
        "pole": (
            f"{'+' if bull else ''}{pole_return:.0f}% pole over {span} bars"
            + (f" on {volume_x:.1f}x volume (\u2265 1.3x)" if volume_x else "")
        ),
        "Pole": (
            f"{'bull' if bull else 'bear'} pole {pole_return:+.0f}% over {span} bars"
            + (f" on {volume_x:.1f}x volume" if volume_x else "")
        ),
    }
    rail_span = _rail_span(c)
    slope_hi = _rail_slope(c, "Upper pennant fit")
    slope_lo = _rail_slope(c, "Lower pennant fit")
    if rail_span:
        notes["Upper pennant fit"] = (
            f"regression of the coil highs, {rail_span} bars"
            + (f", slope {slope_hi:+.3f}/bar" if slope_hi is not None else "")
        )
        notes["Lower pennant fit"] = (
            f"regression of the coil lows, {rail_span} bars"
            + (f", slope {slope_lo:+.3f}/bar" if slope_lo is not None else "")
        )
        if slope_hi is not None and slope_lo is not None:
            notes["Lower pennant fit"] += f"; rails converge ({slope_hi - slope_lo:+.3f})"
    entry = c.bar("entry")
    if entry is not None:
        coil_start = c.bar("Upper pennant fit") or entry - 1
        coil_volume = c.mean_volume(coil_start, max(coil_start, entry - 1))
        ratio = c.volume(entry) / coil_volume if coil_volume else None
        edge = "above the coil highs" if bull else "below the coil lows"
        notes["entry"] = (
            f"breakout close {_price(c.close(entry))} {edge}"
            + (f" on {ratio:.1f}x coil volume (\u2265 1.5x)" if ratio else "")
        )
    return notes


def _rail_span(c: _Ctx) -> int | None:
    ann = c.anchors.get("Upper pennant fit")
    if not ann:
        return None
    start = _date_key(ann.get("start_date"))
    end = _date_key(ann.get("end_date"))
    if start not in c.bars or end not in c.bars:
        return None
    return c.bars[end] - c.bars[start] + 1


def _rail_slope(c: _Ctx, label: str) -> float | None:
    ann = c.anchors.get(label)
    start = _date_key(ann.get("start_date")) if ann else None
    end = _date_key(ann.get("end_date")) if ann else None
    if ann is None or start not in c.bars or end not in c.bars:
        return None
    bars = c.bars[end] - c.bars[start]
    try:
        return (float(ann["end_price"]) - float(ann["start_price"])) / bars if bars else 0.0
    except (KeyError, TypeError, ValueError):
        return None


# ── Breakout and retest ───────────────────────────────────────────────────────
@_builder("pattern_011_breakout_retest")
def _breakout_retest(c: _Ctx) -> dict[str, str]:
    breakout, retest = c.bar("breakout"), c.bar("retest")
    resistance = c.price("Resistance / retest support", "start_price")
    support = c.price("Range support", "start_price")
    if breakout is None or retest is None or resistance is None:
        return {}
    retest_low = c.price("retest") or c.low(retest)
    span = c.bar("Resistance / retest support")
    notes = {
        "Resistance / retest support": (
            f"range high {_price(resistance)}"
            + (f" over {retest - span} bars" if span else "")
            + "; \u2265 2 swing-high touches"
        ),
        "breakout": f"first close {_price(c.close(breakout))} above range high {_price(resistance)}",
        "retest": (
            f"pullback low {_price(retest_low)} holds the old resistance "
            f"({_move(retest_low, resistance):+.1f}%)"
        ),
        "entry": f"bullish close {_price(c.close(c.last))} above the retest bar and resistance",
    }
    span_start = c.bar("Range support")
    if support is not None:
        width = _move(resistance, support)
        notes["Range support"] = (
            f"range low {_price(support)}"
            + (f" over {max(0, breakout - span_start)} bars" if span_start else "")
            + f"; range width {width:.1f}% (\u2264 15%)"
        )
    return notes


# ── Dispatch ──────────────────────────────────────────────────────────────────
def _infer_patterns(labels: set[str]) -> list[str]:
    """Every signature that fits, most specific first."""
    return [name for name, required in _SIGNATURES if required <= labels]


def _reasons(
    pattern: str | None, annotations: list[dict] | None, df,
) -> dict[str, str]:
    frame = _ohlcv(df)
    if frame is None or not annotations:
        return {}
    anchors: dict[str, dict] = {}
    for ann in annotations:
        label = ann.get("label")
        if label:
            anchors.setdefault(str(label), ann)
    if not anchors:
        return {}
    names = ([pattern] if pattern in _BUILDERS else []) + _infer_patterns(set(anchors))
    if not names:
        return {}
    # An explorer chart can mix signals from several patterns. Merge their notes,
    # but drop any key two patterns disagree about rather than mislabel a part.
    bars = _bar_index(frame)
    merged: dict[str, str] = {}
    conflicted: set[str] = set()
    for name in dict.fromkeys(names):
        for key, text in _BUILDERS[name](_Ctx(name, frame, anchors, bars)).items():
            if merged.setdefault(key, text) != text:
                conflicted.add(key)
    for key in conflicted:
        merged.pop(key, None)
    # Mirror each raw label onto its display label so either lookup form works.
    labels = _display_labels()
    for raw, text in list(merged.items()):
        display = labels.get(raw)
        if display and display not in merged:
            merged[display] = text
    return merged
