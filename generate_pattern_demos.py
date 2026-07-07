"""
generate_pattern_demos.py — Manufacture synthetic OHLCV data per pattern,
run the REAL pattern.analyze() to obtain authentic chart annotations, and
render labeled PNG demos into pattern_demos/.

For each pattern we engineer a close path (and volume path where the pattern
needs leg-2 volume weakness) so that the pattern's hard filters pass and
analyze() fires a TradeSignal on the last bar. The signal's chart_annotations
(H1/H2/neckline/entry/TP/stop/channel lines/…) are overlaid on a
TradingView-style candlestick chart and saved as a PNG.

Run:  python generate_pattern_demos.py
"""

from __future__ import annotations

import importlib

from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from data.ohlcv_store import OHLCVStore
from data.tv_client import OHLCVCandle, MarketSnapshot
from analysis.indicator_engine import IndicatorEngine
from analysis.chart_renderer import ChartRenderer


def _load_pattern_class(module_filename: str, class_name: str):
    """Import a pattern module by its on-disk filename and fetch a class.

    Pattern modules are numbered files with no "pattern_" prefix (e.g.
    patterns/002_double_top.py), so a literal `from patterns.002_double_top
    import ...` statement is a syntax error — module names can't start with
    a digit. Load them the same way core/scanner.py and core/backtester.py
    already do: via importlib, by filename string. Returns None (instead of
    raising) if the module or class isn't found, so a missing/renamed
    pattern only skips its own demo rather than crashing the whole script.
    """
    try:
        module = importlib.import_module(f"patterns.{module_filename}")
    except ModuleNotFoundError:
        return None
    return getattr(module, class_name, None)


EMACrossoverPattern = _load_pattern_class(
    "pattern_001_ema_crossover", "EMACrossoverPattern"
)
DoubleTopPattern = _load_pattern_class("002_double_top", "DoubleTopPattern")
DoubleBottomPattern = _load_pattern_class("003_double_bottom", "DoubleBottomPattern")
RoundingBottomPattern = _load_pattern_class(
    "004_rounding_bottom", "RoundingBottomPattern"
)
RoundingTopPattern = _load_pattern_class("005_rounding_top", "RoundingTopPattern")
UpwardChannelPattern = _load_pattern_class(
    "006_upward_channel", "UpwardChannelPattern"
)
DescendingChannelPattern = _load_pattern_class(
    "007_descending_channel", "DescendingChannelPattern"
)
HeadAndShouldersPattern = _load_pattern_class(
    "008_head_and_shoulders", "HeadAndShouldersPattern"
)

DEMO_DIR = Path("pattern_demos")
TF = "1d"


# ── candle construction helpers ──────────────────────────────────────────────
def timestamps(n: int) -> list:
    end = pd.Timestamp.now(tz="UTC").normalize()
    idx = pd.bdate_range(end=end, periods=n, freq="B")
    return [pd.Timestamp(t).to_pydatetime() for t in idx]


def build_candles(closes: np.ndarray, volumes: np.ndarray,
                  peak_wicks: set = (), trough_wicks: set = ()) -> list[OHLCVCandle]:
    """Build OHLCV candles from a close path.

    open = previous close; high/low = body ± a small wick. Bars in
    `peak_wicks` get an extra upper wick and bars in `trough_wicks` an extra
    lower wick so swing-high / swing-low detection (lookback 2) sees a single
    clean structural point instead of a 2-bar equal-high cluster.
    """
    ts = timestamps(len(closes))
    candles: list[OHLCVCandle] = []
    prev = float(closes[0])
    for i, c in enumerate(closes):
        o = prev
        c = float(c)
        body = abs(c - o)
        wick = max(0.4, body * 0.4)
        up = 2.0 if i in peak_wicks else 0.0
        dn = 2.0 if i in trough_wicks else 0.0
        h = max(o, c) + wick + up
        l = min(o, c) - wick - dn
        candles.append(OHLCVCandle(
            open=o, high=h, low=l, close=c,
            volume=float(volumes[i]), timestamp=ts[i],
        ))
        prev = c
    return candles


def from_anchors(n: int, anchors: list[tuple[int, float]]) -> np.ndarray:
    """Linear interpolation between (idx, value) anchors. Anchor values are
    set once; gaps are filled strictly between, so no endpoint duplication."""
    c = np.empty(n)
    for (i, vi), (j, vj) in zip(anchors[:-1], anchors[1:]):
        c[i] = vi
        for k in range(i + 1, j):
            c[k] = vi + (vj - vi) * (k - i) / (j - i)
    c[anchors[-1][0]] = anchors[-1][1]
    return c


def up_down_vol(closes: np.ndarray, lo: int, hi: int,
                up_vol: float, down_vol: float, base: float) -> np.ndarray:
    """Volume array: `base` everywhere except [lo, hi] where up-bars get `up_vol`
    and down-bars get `down_vol` (open = prev close, so up = close>prev close)."""
    vols = np.full(len(closes), base, dtype=float)
    for i in range(lo, hi + 1):
        if i == 0:
            continue
        if closes[i] > closes[i - 1]:
            vols[i] = up_vol
        elif closes[i] < closes[i - 1]:
            vols[i] = down_vol
    return vols


def make_snapshot(symbol: str, candles: list[OHLCVCandle], rec: str = "NEUTRAL") -> MarketSnapshot:
    last = candles[-1]
    return MarketSnapshot(
        symbol=symbol,
        timeframe=TF,
        timestamp=datetime.now(timezone.utc),
        candle=last,
        indicators={"open": last.open, "high": last.high, "low": last.low,
                    "close": last.close, "volume": last.volume},
        summary={"RECOMMENDATION": rec},
        oscillators={},
        moving_avgs={},
    )


def run_pattern(pattern, symbol: str, candles: list[OHLCVCandle],
                snapshot: MarketSnapshot):
    store = OHLCVStore(window=500)
    store.replace_all(symbol, TF, candles)
    df = store.get_df(symbol, TF)
    sig = pattern.analyze(snapshot, store)
    return df, sig


def render_demo(symbol_label: str, df: pd.DataFrame, annotations: list[dict],
                path: Path, with_ema: bool = False) -> None:
    renderer = ChartRenderer(save_to_disk=False)
    if with_ema:
        png = renderer.render_with_ema(symbol_label, TF, df,
                                       ema_periods=[20, 50],
                                       annotations=annotations)
    else:
        # ChartRenderer.render() has a kwarg bug; call _render_chart directly.
        png = renderer._render_chart(symbol_label, TF, df,
                                     annotations=annotations)
    path.write_bytes(png)
    print(f"  saved → {path}")


def save_csv(path: Path, df: pd.DataFrame, rsi: pd.Series | None = None) -> None:
    out = df.copy()
    if rsi is not None:
        out["rsi_14"] = rsi.values
    out.index.name = "date"
    out.to_csv(path)
    print(f"  saved → {path}")


# ── per-pattern data manufacturers ───────────────────────────────────────────
def gen_ema_crossover() -> tuple[np.ndarray, np.ndarray, int]:
    """Build a long decline + rally, find the first EMA20/EMA50 bullish cross,
    then take the prefix ending at that cross bar. EMA(adjust=False) is
    recursive, so ema[k] depends only on bars 0..k — the prefix's last-bar
    EMAs equal the full series' EMAs at k, so the cross lands on the last bar.
    """
    decline_bars = 70
    rally_bars = 90
    n = decline_bars + rally_bars
    c = np.empty(n)
    c[:decline_bars] = np.linspace(100, 78, decline_bars)
    c[decline_bars:] = np.linspace(78, 118, rally_bars)
    df = pd.DataFrame({"close": c})
    ind = IndicatorEngine(df.assign(open=c, high=c, low=c, volume=1e6))
    ef = ind.ema(20)
    es = ind.ema(50)
    for k in range(decline_bars + 2, n):
        if ef.iloc[k - 1] <= es.iloc[k - 1] and ef.iloc[k] > es.iloc[k]:
            closes = c[:k + 1].copy()
            vols = np.full(len(closes), 1_200_000, dtype=float)
            vols[decline_bars:] = 1_800_000  # volume spike on the rally
            return closes, vols, decline_bars
    raise RuntimeError("EMA crossover cross-on-last-bar not found")


def zigzag_leg(c, lo, hi, v0, v1, up_amp, dn_amp):
    """Fill c[lo:hi+1] with a choppy ramp from v0 to v1.

    Even steps (relative to lo) move up by `up_amp`, odd steps down by
    `dn_amp`, on top of a linear drift from v0 to v1. The balanced up/down
    churn keeps RSI in a moderate band (gains ≈ losses) while price still
    trends from v0 to v1. v0 is NOT rewritten (it is set by the caller); the
    last bar is forced to v1 so it is the leg extreme.
    """
    span = hi - lo
    for k in range(1, span + 1):
        i = lo + k
        drift = v0 + (v1 - v0) * k / span
        step = up_amp if (k % 2 == 0) else -dn_amp
        c[i] = drift + step
    c[hi] = v1


def gen_double_top() -> tuple[np.ndarray, set, set]:
    """M-pattern with clean H1/H2 peaks and a valley. RSI dip before H1 gives
    a real overbought reading; a choppy H2 rally keeps RSI in the 50–61 band."""
    n = 127
    c = from_anchors(n, [(0, 78), (56, 116), (62, 114.0), (68, 122),
                         (69, 124.0), (70, 121), (90, 106.0)])
    zigzag_leg(c, 90, 119, 106.0, 112.0, up_amp=0.7, dn_amp=0.5)  # H2 @119
    tail = [110, 108, 107, 106, 105, 104, 102]  # neckline break on last bar
    for k, v in enumerate(tail):
        c[120 + k] = v
    return c, {69, 119}, {90}


def gen_double_bottom() -> tuple[np.ndarray, set, set]:
    """W-pattern, inverse of double top."""
    n = 127
    c = from_anchors(n, [(0, 124), (56, 86), (62, 88.0), (68, 82),
                         (69, 78.0), (70, 81), (90, 94.0)])
    zigzag_leg(c, 90, 119, 94.0, 86.0, up_amp=0.5, dn_amp=0.7)  # L2 @119
    tail = [88, 90, 92, 93, 94, 96, 98]  # neckline break on last bar
    for k, v in enumerate(tail):
        c[120 + k] = v
    return c, {90}, {69, 119}


def gen_rounding_bottom() -> tuple[np.ndarray, set, set]:
    """Saucer: plateau at neckline, parabolic U to a late bottom, then a
    steeper rise that fires the 2-day HH+HL entry on the last bar."""
    n = 200
    bottom = 197
    neck, bclose = 100.0, 70.0
    a = (neck - bclose) / (60 ** 2)
    c = np.empty(n)
    c[:137] = neck
    for i in range(137, 198):
        c[i] = bclose + a * (i - bottom) ** 2
    c[198] = 71.0
    c[199] = 72.5
    return c, set(), {197}


def gen_rounding_top() -> tuple[np.ndarray, set, set]:
    """Inverted saucer: plateau at floor, parabolic ∩ to a late top with a
    shallow dip ~7 bars before for a real overbought RSI, then decline that
    fires the 2-day LH+LL entry on the last bar."""
    n = 200
    top = 197
    floor, crown = 70.0, 100.0
    a = (crown - floor) / (60 ** 2)
    c = np.empty(n)
    c[:137] = floor
    for i in range(137, 198):
        c[i] = crown - a * (i - top) ** 2
    # shallow dip 7 bars before the top (introduces a down bar for real RSI)
    trend = crown - a * (190 - top) ** 2
    c[190] = trend - 0.5
    c[198] = 99.5
    c[199] = 98.0
    return c, {197}, set()


def gen_upward_channel() -> tuple[np.ndarray, set, set]:
    """Rising channel: start → SH1 → valley → SH2 (higher high, lower RSI) →
    breakdown below the rising lower channel line on the last bar."""
    n = 213
    c = from_anchors(n, [(0, 80), (73, 97), (80, 100.0),          # SH1 @80
                         (81, 99), (140, 92.0),                    # valley @140
                         (191, 101), (195, 99.0), (205, 102.5),    # SH2 @205
                         (206, 100), (207, 99), (208, 98), (209, 96),
                         (210, 91), (211, 90), (212, 89)])
    # micro-pullback before SH1 so RSI(14) at SH1 is real (not nan)
    for k, v in enumerate([98.5, 97.8, 98.6, 99.3, 99.8]):
        c[75 + k] = v
    return c, {80, 205}, {0, 140}


def gen_descending_channel() -> tuple[np.ndarray, set, set]:
    """Falling channel, inverse of upward channel."""
    n = 213
    a = [(0, 120), (73, 103), (80, 100.0),        # SL1 @80 (rally @73 for RSI)
         (81, 101), (140, 108.0),                  # peak @140
         (191, 99), (195, 101.0), (205, 96.0),     # SL2 @205 (choppy, rally @195)
         (206, 98), (207, 100), (208, 102), (209, 104),
         (210, 106), (211, 109), (212, 110)]
    return from_anchors(n, a), {0, 140}, {80, 205}


def gen_head_and_shoulders() -> tuple[np.ndarray, set, set]:
    """LS → LN → HD → RN → RS, flat neckline, bearish RSI divergence, neckline
    break on the last bar. The RS leg uses micro-pullbacks (a 1-bar down step
    recovered the next bar) so Wilder RSI at RS sits ≤60 without creating any
    extra close-swing-highs (HEAD_LOOKBACK=4, strict)."""
    n = 217
    c = from_anchors(n, [(0, 70), (100, 100.0),        # LS @100
                         (101, 99), (120, 90.0),        # LN @120
                         (150, 120.0),                  # HD @150
                         (151, 119), (180, 90.0)])      # RN @180
    # RS leg: gentle rise 90 -> 93, then micro-pullback pattern to 95 at 210.
    for i in range(181, 197):
        c[i] = 90 + (93 - 90) * (i - 181) / 16
    rs_tail = [93.0, 93.7, 93.1, 93.8, 93.2, 93.9, 93.3, 94.0,
               93.4, 94.1, 93.5, 94.2, 93.6, 94.5]      # 197..210 (RS=94.5)
    for k, v in enumerate(rs_tail):
        c[197 + k] = v
    post = [94, 92, 91, 90.5, 89, 88]                   # 211..216
    for k, v in enumerate(post):
        c[211 + k] = v
    return c, set(), set()


# ── demo runner ──────────────────────────────────────────────────────────────
def main() -> None:
    DEMO_DIR.mkdir(exist_ok=True)

    def _demo_spec(name, cls, action, genfn, with_ema, extra):
        if cls is None:
            print(f"\n=== {name} ===\n  skipping — patterns/{name}.py not found")
            return None
        return (name, cls(), action, genfn, with_ema, extra)

    demos = [
        d
        for d in (
            _demo_spec("pattern_001_ema_crossover", EMACrossoverPattern, "BUY",
                       gen_ema_crossover, True, {}),
            _demo_spec("pattern_002_double_top", DoubleTopPattern, "SELL",
                       gen_double_top, False, {"leg2": (89, 120, 400_000, 1_500_000)}),
            _demo_spec("pattern_003_double_bottom", DoubleBottomPattern, "BUY",
                       gen_double_bottom, False, {"leg2": (89, 120, 1_500_000, 400_000)}),
            _demo_spec("pattern_004_rounding_bottom", RoundingBottomPattern, "BUY",
                       gen_rounding_bottom, False, {}),
            _demo_spec("pattern_005_rounding_top", RoundingTopPattern, "SELL",
                       gen_rounding_top, False, {}),
            _demo_spec("pattern_006_upward_channel", UpwardChannelPattern, "SELL",
                       gen_upward_channel, False, {"disable_edgar": True}),
            _demo_spec("pattern_007_descending_channel", DescendingChannelPattern, "BUY",
                       gen_descending_channel, False, {"disable_edgar": True}),
            _demo_spec("pattern_008_head_and_shoulders", HeadAndShouldersPattern, "SELL",
                       gen_head_and_shoulders, False, {}),
        )
        if d is not None
    ]

    for name, pattern, action, genfn, with_ema, extra in demos:
        print(f"\n=== {name} ===")
        try:
            if name == "pattern_001_ema_crossover":
                closes, vols, _ = genfn()
                peaks, troughs = set(), set()
            else:
                closes, peaks, troughs = genfn()
                vols = np.full(len(closes), 1_000_000, dtype=float)
                if "leg2" in extra:
                    lo, hi, uv, dv = extra["leg2"]
                    vols = up_down_vol(closes, lo, hi, uv, dv, 1_000_000)
        except Exception as exc:
            print(f"  data generation failed: {exc!r}")
            continue

        if extra.get("disable_edgar"):
            pattern.V9_EARNINGS_BLACKOUT = False

        symbol = "DEMO"
        candles = build_candles(closes, vols, peaks, troughs)
        snapshot = make_snapshot(symbol, candles,
                                 rec="BUY" if action == "BUY" else "SELL")
        df, sig = run_pattern(pattern, symbol, candles, snapshot)

        # RSI diagnostics
        ind = IndicatorEngine(df)
        rsi = ind.rsi(14)

        if sig is None:
            print(f"  !! analyze() returned None — rendering structure only")
            annotations = []
        else:
            print(f"  fired: {sig.action} conf={sig.confidence:.2f} "
                  f"entry={sig.price:.2f} "
                  f"tp={sig.take_profit} stop={sig.stop_loss}")
            annotations = sig.chart_annotations

        label = f"{name} ({action})"
        png_path = DEMO_DIR / f"{name}_demo.png"
        csv_path = DEMO_DIR / f"{name}_demo.csv"
        render_demo(label, df, annotations, png_path, with_ema=with_ema)
        save_csv(csv_path, df, rsi)


if __name__ == "__main__":
    main()
