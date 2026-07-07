"""
patterns/pattern_001_ema_crossover.py — EMA 20/50 crossover pattern (TEMPLATE).

Adjusted for SWING TRADING: daily/weekly bars, slower EMAs, and a confidence
score that no longer leans on intraday volume spikes (which are noisy on
higher timeframes).

Replace the parameters and logic in analyze() with Toby's actual rules.

What this template checks:
  - EMA 20 crosses above EMA 50 → BUY signal
  - EMA 20 crosses below EMA 50 → SELL signal
  - RSI confirms (not overbought on buy / not oversold on sell)
  - TV recommendation aligns
  - Volume above 20-bar average

Confidence is scored 0–1 based on how many conditions are met.
Vision check runs if confidence ≥ settings.vision_min_indicator_confidence.
"""

from __future__ import annotations

from patterns.base_pattern import (
    BasePattern, TradeSignal, ann_marker, ANN_ENTRY,
)
from data.tv_client import MarketSnapshot
from data.ohlcv_store import OHLCVStore
from analysis.indicator_engine import IndicatorEngine
from utils.logger import log


class EMACrossoverPattern(BasePattern):

    # ── Identity ───────────────────────────────────────────────────────────────
    @property
    def name(self) -> str:
        return "pattern_001_ema_crossover"

    @property
    def timeframes(self) -> list[str]:
        return ["1d", "1W"]       # swing trading: run on daily and weekly bars

    @property
    def chart_description(self) -> str:
        return (
            "EMA 20 and EMA 50 are overlaid on a daily/weekly candlestick chart. "
            "A BUY signal looks like: EMA 20 (fast) crosses above EMA 50 (slow) "
            "with rising volume and candles closing above both EMAs — the start "
            "of a multi-day/week uptrend, not a single-bar spike. "
            "A SELL signal is the inverse: EMA 20 crosses below EMA 50."
        )

    # ── Parameters (replace with Toby's values) ────────────────────────────────
    EMA_FAST   = 20      # ~1 month on daily bars
    EMA_SLOW   = 50      # ~2.5 months on daily bars
    RSI_PERIOD = 14
    RSI_OB     = 75      # overbought threshold (blocks buys above this) — widened
    RSI_OS     = 25      # oversold threshold  (blocks sells below this) — widened
    SHARES     = 25      # fixed position size — replace with risk-based sizing later

    # ── Core logic ─────────────────────────────────────────────────────────────
    def analyze(
        self,
        snapshot: MarketSnapshot,
        store: OHLCVStore,
    ) -> TradeSignal | None:

        symbol    = snapshot.symbol
        timeframe = snapshot.timeframe

        # ── Pull OHLCV history from store for custom indicator computation ──────
        df = store.get_df(symbol, timeframe)
        if df is None or len(df) < self.EMA_SLOW + 5:
            log.debug(f"[{self.name}] {symbol} {timeframe}: not enough history yet")
            return None

        ind  = IndicatorEngine(df)
        ema_fast = ind.ema(self.EMA_FAST)
        ema_slow = ind.ema(self.EMA_SLOW)
        rsi      = ind.rsi(self.RSI_PERIOD)
        vol_avg  = ind.volume.rolling(20).mean()

        # Current and previous bar values
        ef_now,  ef_prev  = ema_fast.iloc[-1],  ema_fast.iloc[-2]
        es_now,  es_prev  = ema_slow.iloc[-1],  ema_slow.iloc[-2]
        rsi_now           = rsi.iloc[-1]
        vol_now           = ind.volume.iloc[-1]
        vol_avg_now       = vol_avg.iloc[-1]
        close             = snapshot.candle.close

        # ── Crossover detection ────────────────────────────────────────────────
        crossed_up   = (ef_prev <= es_prev) and (ef_now > es_now)
        crossed_down = (ef_prev >= es_prev) and (ef_now < es_now)

        if not crossed_up and not crossed_down:
            return None    # No crossover this bar

        action = "BUY" if crossed_up else "SELL"

        # ── Confidence scoring (each condition adds to score) ──────────────────
        score = 0.0

        # 1. RSI filter
        if action == "BUY"  and rsi_now < self.RSI_OB:
            score += 0.25
        if action == "SELL" and rsi_now > self.RSI_OS:
            score += 0.25

        # 2. Volume confirmation
        if vol_now > vol_avg_now:
            score += 0.25

        # 3. TradingView recommendation alignment
        tv_rec = snapshot.tv_recommendation
        if action == "BUY"  and tv_rec in ("BUY", "STRONG_BUY"):
            score += 0.25
        if action == "SELL" and tv_rec in ("SELL", "STRONG_SELL"):
            score += 0.25

        # 4. Price above/below both EMAs (trend confirmation)
        if action == "BUY"  and close > ef_now and close > es_now:
            score += 0.25
        if action == "SELL" and close < ef_now and close < es_now:
            score += 0.25

        # Cap at 1.0
        confidence = min(score, 1.0)

        log.info(
            f"[{self.name}] {symbol} {timeframe} | "
            f"Crossover detected: {action} | "
            f"close={close:.4f} EMA{self.EMA_FAST}={ef_now:.4f} "
            f"EMA{self.EMA_SLOW}={es_now:.4f} RSI={rsi_now:.1f} "
            f"confidence={confidence:.2f}"
        )

        return TradeSignal(
            symbol=symbol,
            action=action,
            pattern=self.name,
            timeframe=timeframe,
            confidence=confidence,
            price=close,
            qty=self.SHARES,
            notes=f"EMA{self.EMA_FAST}/EMA{self.EMA_SLOW} crossover | RSI={rsi_now:.1f}",
            chart_annotations=[
                ann_marker(
                    date=self.bar_date(df, len(df) - 1),
                    price=float(close),
                    label=f"{action} · EMA cross",
                    color=ANN_ENTRY,
                    marker="o",
                    label_pos="below" if action == "BUY" else "above",
                ),
            ],
        )
