from __future__ import annotations

from analysis.indicator_engine import IndicatorEngine
from data.ohlcv_store import OHLCVStore
from data.tv_client import MarketSnapshot
from patterns._channels import find_channel
from patterns._rules import earnings_blackout, notional_qty
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

    def analyze(self, snapshot: MarketSnapshot, store: OHLCVStore) -> TradeSignal | None:
        df = store.get_df(snapshot.symbol, snapshot.timeframe, min_bars=self.MIN_BARS)
        if df is None:
            return None
        ind = IndicatorEngine(df)
        setup = find_channel(ind, ind.rsi(14), len(df) - 1, "up")
        if setup is None:
            return None
        if earnings_blackout(df, snapshot.symbol, setup.entry, 15):
            return None
        price = float(ind.close.iloc[setup.entry])
        stop = round(setup.second_price * 1.01, 4)
        target = round(max(price - setup.width, price * 0.93), 4)
        upper_at_entry = setup.first_price + setup.slope * (setup.entry - setup.first)
        return TradeSignal(
            symbol=snapshot.symbol,
            action="SELL",
            pattern=self.name,
            timeframe=snapshot.timeframe,
            confidence=1.0,
            price=price,
            qty=notional_qty(self.POSITION_NOTIONAL, price),
            stop_loss=stop,
            stop_loss_on_close=True,
            take_profit=target,
            trailing_stop_pct=0.025,
            trailing_stop_mode="lowest_close",
            trailing_activation_pct=0.04,
            exit_bars_after_entry=15,
            notes=f"Upward channel SH1={setup.first} SH2={setup.second} valley={setup.turn} width={setup.width:.2f} RSI={setup.first_rsi:.1f}->{setup.second_rsi:.1f}->{setup.break_rsi:.1f}",
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
