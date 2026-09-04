from __future__ import annotations

from analysis.indicator_engine import IndicatorEngine
from data.ohlcv_store import OHLCVStore
from data.tv_client import MarketSnapshot
from patterns._channels import find_channel
from patterns._rules import earnings_blackout
from patterns.base_pattern import (
    ANN_ENTRY,
    ANN_LINE,
    ANN_PEAK,
    ANN_REF,
    ANN_STOP,
    ANN_TARGET,
    ANN_TROUGH,
    BasePattern,
    TradeSignal,
    ann_hline,
    ann_marker,
    ann_segment,
)

# `.cjs` backtest_uc_v14.cjs locked constants (C16-C24).
STOP_MULT = 1.01           # C16 dynamic stop = SH2 x 1.01
FIXED_STOP_CAP = 0.05      # C24 dual stop: OR entry x 1.05, whichever first
FIXED_TARGET_PCT = 0.07    # C17/C18 target = max(measured move, entry x 0.93)
TIME_STOP_BARS = 15        # C19
TRAIL_TRIGGER = 0.04       # C20 arm after +4%
TRAIL_GAP = 0.025          # C20 trail 2.5% off best close
MAX_DAYS_TO_BREAK = 20     # C22 freshness
MAX_DROP_FROM_SH2 = 0.15   # C23 don't-chase
EARNINGS_WINDOW_BARS = 15  # v9 earnings blackout window


class UpwardChannelPattern(BasePattern):
    MIN_BARS = 210
    POSITION_NOTIONAL = 10_000.0

    @property
    def name(self) -> str:
        return "pattern_006_upward_channel"

    @property
    def timeframes(self) -> list[str]:
        return ["1d"]

    @property
    def chart_description(self) -> str:
        return "Rising parallel channel with two higher swing highs, bearish RSI divergence, and two closes below the lower channel line."

    def _stub(self, snapshot: MarketSnapshot, price: float, **flags) -> TradeSignal:
        return TradeSignal(
            symbol=snapshot.symbol,
            action="SELL",
            pattern=self.name,
            timeframe=snapshot.timeframe,
            confidence=1.0,
            price=price,
            qty=0.0,
            **flags,
        )

    def analyze(self, snapshot: MarketSnapshot, store: OHLCVStore) -> TradeSignal | None:
        df = store.get_df(snapshot.symbol, snapshot.timeframe, min_bars=self.MIN_BARS)
        if df is None:
            return None
        ind = IndicatorEngine(df)
        rsi = ind.rsi_wilder(14)
        setup = find_channel(ind, rsi, len(df) - 1, "up")
        if setup is None:
            return None
        price = float(ind.close.iloc[setup.entry])

        # C22 freshness — skip if the break is a slow drift far past SH2.
        days_to_break = setup.entry - setup.second
        if days_to_break > MAX_DAYS_TO_BREAK:
            return self._stub(snapshot, price,
                              filtered_reason=f"C22 stale ({days_to_break}d)")
        # C23 don't-chase — skip if price already slid >15% from SH2.
        drop = (setup.second_price - price) / setup.second_price if setup.second_price else 0.0
        if drop > MAX_DROP_FROM_SH2:
            return self._stub(snapshot, price,
                              filtered_reason=f"C23 chasing ({drop * 100:.1f}%)")
        # v9 earnings blackout.
        if earnings_blackout(df, snapshot.symbol, setup.entry, EARNINGS_WINDOW_BARS):
            return self._stub(snapshot, price, blocked_reason="earnings")

        stop = round(setup.second_price * STOP_MULT, 4)
        target = round(max(price - setup.width, price * (1 - FIXED_TARGET_PCT)), 4)
        upper_at_entry = setup.first_price + setup.slope * (setup.entry - setup.first)
        return TradeSignal(
            symbol=snapshot.symbol,
            action="SELL",
            pattern=self.name,
            timeframe=snapshot.timeframe,
            confidence=1.0,
            price=price,
            qty=self.POSITION_NOTIONAL / price if price > 0 else 0.0,
            stop_loss=stop,
            stop_loss_on_close=True,           # C16 close-based
            stop_loss_pct_cap=FIXED_STOP_CAP,  # C24 dual stop
            take_profit=target,
            trailing_stop_pct=TRAIL_GAP,
            trailing_stop_mode="lowest_close",
            trailing_stop_on_close=True,       # C20 close-based
            trailing_activation_pct=TRAIL_TRIGGER,
            reclaim_exit=True,                 # C21
            reclaim_lower_rail=(setup.entry_line, setup.slope),
            exit_bars_after_entry=TIME_STOP_BARS,
            notes=(
                f"UC SH1={setup.first} SH2={setup.second} valley={setup.turn} "
                f"width={setup.width:.2f} RSI={setup.first_rsi:.1f}->{setup.second_rsi:.1f}"
                f"->{setup.break_rsi:.1f} daysToBreak={days_to_break} dropFromSH2={drop * 100:.1f}%"
            ),
            chart_annotations=[
                ann_marker(self.bar_date(df, setup.start), setup.start_price, "start", ANN_REF, "^", "below"),
                ann_marker(self.bar_date(df, setup.first), setup.first_price, "SH1", ANN_PEAK, "v", "above"),
                ann_marker(self.bar_date(df, setup.turn), setup.turn_price, "valley", ANN_TROUGH, "^", "below"),
                ann_marker(self.bar_date(df, setup.second), setup.second_price, "SH2", ANN_PEAK, "v", "above"),
                ann_segment(self.bar_date(df, setup.first), self.bar_date(df, setup.entry), setup.first_price, upper_at_entry, ANN_LINE),
                ann_segment(self.bar_date(df, setup.turn), self.bar_date(df, setup.entry), setup.turn_price, setup.entry_line, ANN_LINE),
                ann_hline(stop, "stop", ANN_STOP),
                ann_hline(target, "target", ANN_TARGET),
                ann_marker(self.bar_date(df, setup.entry), price, "entry", ANN_ENTRY, "o", "above"),
            ],
        )
