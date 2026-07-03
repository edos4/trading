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

from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from data.ohlcv_store import OHLCVStore
from data.tv_client import OHLCVCandle, MarketSnapshot
from analysis.indicator_engine import IndicatorEngine
from analysis.chart_renderer import ChartRenderer
from patterns.pattern_001_ema_crossover import EMACrossoverPattern
from patterns.pattern_002_double_top import DoubleTopPattern
from patterns.pattern_003_double_bottom import DoubleBottomPattern
from patterns.pattern_004_rounding_bottom import RoundingBottomPattern
from patterns.pattern_005_rounding_top import RoundingTopPattern
from patterns.pattern_006_upward_channel import UpwardChannelPattern
from patterns.pattern_007_descending_channel import DescendingChannelPattern
from patterns.pattern_008_head_and_shoulders import HeadAndShouldersPattern

DEMO_DIR = Path("pattern_demos")
TF = "1d"


# ── candle construction helpers ──────────────────────────────────────────────
def timestamps(n: int) -> list:
    end = pd.Timestamp.now(tz="UTC").normalize()
    idx = pd.bdate_range(end=end, periods=n, freq="B")
    return [pd.Timestamp(t).to_pydatetime() for t in idx]


def build_candles(closes: np.ndarray, volumes: np.ndarray) -> list[OHLCVCandle]:
    """Build OHLCV candles from a close path.

    open = previous close, high/low = body ± a small wick so that swing-high /
    swing-low detection (lookback 2) tracks the close shape.
    """
    ts = timestamps(len(closes))
    candles: list[OHLCVCandle] = []
    prev = float(closes[0])
    for i, c in enumerate(closes):
        o = prev
        c = float(c)
        body = abs(c - o)
        wick = max(0.4, body * 0.4)
        h = max(o, c) + wick
        l = min(o, c) - wick
        candles.append(OHLCVCandle(
            open=o, high=h, low=l, close=c,
            volume=float(volumes[i]), timestamp=ts[i],
        ))
        prev = c
    return candles


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


def gen_double_top() -> np.ndarray:
    """M-pattern: strong rise to H1, pullback to valley, weaker rally to H2,
    then decline through the neckline. Designed so analyze() fires SHORT on
    the last bar (day-7 entry after H2)."""
    c = np.empty(128)
    # 0..70 strong uptrend 78 -> 124 (H1 at 70)
    c[:71] = np.linspace(78, 124, 71)
    # 71..92 pullback to ~106 (valley ~88)
    c[71:93] = np.linspace(124, 106, 22)
    # 93..120 weaker, zigzag rise to H2 (close 118 at 120) with down-bars
    for i in range(93, 121):
        step = (i - 93)
        base = 106 + step * 0.43
        if step % 3 == 2:
            base -= 1.6
        c[i] = base
    c[120] = 118  # H2 close
    # 121..127 decline through neckline (valley low ≈ 105.5)
    c[121:128] = [116, 114, 112, 110, 108, 105, 104]
    return c


def gen_double_bottom() -> np.ndarray:
    """W-pattern: inverse of double top. Decline to L1, rally to peak, weaker
    decline to L2 (higher low, higher RSI), then rally through neckline."""
    c = np.empty(128)
    # 0..70 strong downtrend 124 -> 78 (L1 at 70)
    c[:71] = np.linspace(124, 78, 71)
    # 71..92 rally to ~94 (peak ~88)
    c[71:93] = np.linspace(78, 94, 22)
    # 93..120 weaker, zigzag decline to L2 (close 84 at 120) with up-bars
    for i in range(93, 121):
        step = (i - 93)
        base = 94 - step * 0.43
        if step % 3 == 2:
            base += 1.6
        c[i] = base
    c[120] = 84  # L2 close (higher low: 84 > 78)
    # 121..127 rally through neckline (peak high ≈ 94.5)
    c[121:128] = [86, 88, 90, 92, 93, 95, 96]
    return c


def gen_rounding_bottom() -> np.ndarray:
    """Saucer: plateau at neckline, parabolic U into a late bottom, gentle
    rise that triggers the 2-day HH+HL+RSI entry on the last bar.

    Layout (200 bars, bottom at 197):
      [0..136]  plateau at neckline = 100
      [137..197] left half of parabola: 100 -> 70 (the cup)
      [197..199] right half start: parabola rising (entry fires at 199)
    The ±60 window [137..199] is a clean concave-up parabola.
    """
    n = 200
    bottom = 197
    neck = 100.0
    bclose = 70.0
    a = (neck - bclose) / (60 ** 2)  # parabola coefficient
    c = np.empty(n)
    c[:137] = neck
    for i in range(137, n):
        c[i] = bclose + a * (i - bottom) ** 2
    return c


def gen_rounding_top() -> np.ndarray:
    """Inverted saucer: plateau at floor, parabolic ∩ into a late top, gentle
    decline that triggers the 2-day LH+LL+RSI entry on the last bar."""
    n = 200
    top = 197
    c = np.empty(n)
    floor = 70.0
    crown = 100.0
    depth = crown - floor
    a = depth / (60 ** 2)
    c[:137] = floor
    # parabolic dome: close = crown - a*(i-top)^2 for i in [top-60, top+60]
    for i in range(top - 60, min(n, top + 60)):
        if i < 137:
            continue
        c[i] = crown - a * (i - top) ** 2
    return c


def gen_upward_channel() -> np.ndarray:
    """Rising channel: start -> SH1 -> valley -> SH2 (higher high, lower RSI)
    -> breakdown below the rising lower channel line on the last bar."""
    c = np.empty(213)
    # 0..80 rise 80 -> 100 (SH1 at 80)
    c[:81] = np.linspace(80, 100, 81)
    # 81..120 pullback to 92 (valley at 120)
    c[81:121] = np.linspace(100, 92, 40)
    # 121..180 rise 92 -> 105 (SH2 at 180) — gentler than leg 1
    c[121:181] = np.linspace(92, 105, 60)
    # 181..213 decline below the rising lower channel line
    # lower line through (120, 92) slope 0.05 -> at 210 ≈ 96.5
    c[181:213] = np.linspace(105, 93, 32)
    # Force the 2-consec below to land on the last bar (212):
    # lower_line(210)=96.5, (211)=96.55, (212)=96.6
    c[209] = 97   # >= line(209)=96.45  (reset)
    c[210] = 95   # < line(210)=96.5   (consec 1)
    c[211] = 94   # < line(211)=96.55  (consec 2 -> entry=211)
    c[212] = 93   # last bar (cur=212, but entry=211 -> we need entry==cur)
    return c


def gen_descending_channel() -> np.ndarray:
    """Falling channel: start -> SL1 -> peak -> SL2 (lower low, higher RSI)
    -> breakout above the falling upper channel line on the last bar."""
    c = np.empty(213)
    # 0..80 decline 120 -> 100 (SL1 at 80)
    c[:81] = np.linspace(120, 100, 81)
    # 81..120 rally to 108 (peak at 120)
    c[81:121] = np.linspace(100, 108, 40)
    # 121..180 decline 108 -> 96 (SL2 at 180) — gentler than leg 1
    c[121:181] = np.linspace(108, 96, 60)
    # 181..213 rally above the falling upper channel line
    c[181:213] = np.linspace(96, 106, 32)
    # upper line through (120,108) slope -0.04 -> at 209≈104.44, 210≈104.4,
    # 211≈104.36, 212≈104.32
    c[209] = 104  # <= line(209)=104.44 (reset)
    c[210] = 105  # > line(210)=104.4  (consec 1)
    c[211] = 106  # > line(211)=104.36 (consec 2 -> entry=211)
    c[212] = 107  # last bar
    return c


def gen_head_and_shoulders() -> np.ndarray:
    """LS -> LN -> HD -> RN -> RS, flat neckline, bearish RSI divergence,
    neckline break on the last bar."""
    c = np.empty(217)
    # 0..100 rise 70 -> 100 (LS at 100, sharp rally -> high RSI)
    c[:101] = np.linspace(70, 100, 101)
    # 101..120 pullback 100 -> 90 (LN at 120)
    c[101:121] = np.linspace(100, 90, 20)
    # 121..150 rise 90 -> 120 (HD at 150, gentler -> lower RSI than LS)
    c[121:151] = np.linspace(90, 120, 30)
    # 151..180 pullback 120 -> 90 (RN at 180)
    c[151:181] = np.linspace(120, 90, 30)
    # 181..210 weak rise 90 -> 95 (RS at 210, close 95)
    c[181:211] = np.linspace(90, 95, 30)
    # 211..216 decline through neckline (flat at 90)
    c[211:217] = [94, 92, 91, 90.5, 89, 88]  # close[215]=89<90, close[216]=88<90
    # need close[214] >= 90 to reset consec: 90.5 >= 90 OK
    return c


# ── demo runner ──────────────────────────────────────────────────────────────
def main() -> None:
    DEMO_DIR.mkdir(exist_ok=True)

    demos = [
        ("pattern_001_ema_crossover", EMACrossoverPattern(), "BUY",
         gen_ema_crossover, True, {}),
        ("pattern_002_double_top", DoubleTopPattern(), "SELL",
         gen_double_top, False, {"leg2": (89, 120, 400_000, 1_500_000)}),
        ("pattern_003_double_bottom", DoubleBottomPattern(), "BUY",
         gen_double_bottom, False, {"leg2": (89, 120, 1_500_000, 400_000)}),
        ("pattern_004_rounding_bottom", RoundingBottomPattern(), "BUY",
         gen_rounding_bottom, False, {}),
        ("pattern_005_rounding_top", RoundingTopPattern(), "SELL",
         gen_rounding_top, False, {}),
        ("pattern_006_upward_channel", UpwardChannelPattern(), "SELL",
         gen_upward_channel, False, {"disable_edgar": True}),
        ("pattern_007_descending_channel", DescendingChannelPattern(), "BUY",
         gen_descending_channel, False, {"disable_edgar": True}),
        ("pattern_008_head_and_shoulders", HeadAndShouldersPattern(), "SELL",
         gen_head_and_shoulders, False, {}),
    ]

    for name, pattern, action, genfn, with_ema, extra in demos:
        print(f"\n=== {name} ===")
        try:
            if name == "pattern_001_ema_crossover":
                closes, vols, _ = genfn()
            else:
                closes = genfn()
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
        candles = build_candles(closes, vols)
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
