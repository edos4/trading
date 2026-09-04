from __future__ import annotations

from analysis.indicator_engine import IndicatorEngine
from data.ohlcv_store import OHLCVStore
from data.tv_client import MarketSnapshot
from patterns._rounding import find_rounding_setup
from patterns._rules import notional_qty
from patterns.base_pattern import (
    ANN_ENTRY,
    ANN_LINE,
    ANN_PEAK,
    ANN_STOP,
    ANN_TARGET,
    BasePattern,
    TradeSignal,
    ann_hline,
    ann_marker,
)


class RoundingTopPattern(BasePattern):
    MIN_BARS = 121
    POSITION_NOTIONAL = 10_000.0

    @property
    def name(self) -> str:
        return "pattern_005_rounding_top"

    @property
    def timeframes(self) -> list[str]:
        return ["1d"]

    @property
    def chart_description(self) -> str:
        return "Concave-down rounding top with a fifteen-to-fifty-percent dome, overbought top RSI, bearish RSI decline, and a two-day lower-low/lower-high entry."

    def analyze(self, snapshot: MarketSnapshot, store: OHLCVStore) -> TradeSignal | None:
        df = store.get_df(snapshot.symbol, snapshot.timeframe, min_bars=self.MIN_BARS)
        if df is None:
            return None
        ind = IndicatorEngine(df)
        setup = find_rounding_setup(ind, ind.rsi_wilder(14), len(df) - 1, "top")
        if setup is None or setup.entry != len(df) - 1:
            return None
        price = float(ind.close.iloc[setup.entry])
        stop = round(price * 1.05, 4)
        target = round(setup.target, 4)
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
            trailing_stop_pct=0.15,
            trailing_stop_mode="lowest_low",
            trailing_stop_on_close=True,
            trailing_activation_pct=0.0,
            notes=f"Rounding top start={setup.start} top={setup.center} height={setup.depth:.1%} fit={setup.fit:.1%} RSI={setup.center_rsi:.1f} divergence={setup.divergence}",
            chart_annotations=[
                ann_hline(setup.neckline, "neckline", ANN_LINE),
                ann_marker(self.bar_date(df, setup.center), setup.center_close, "top", ANN_PEAK, "v", "above"),
                ann_hline(stop, "stop", ANN_STOP),
                ann_hline(target, "target", ANN_TARGET),
                ann_marker(self.bar_date(df, setup.entry), price, "entry", ANN_ENTRY, "o", "above"),
            ],
        )
