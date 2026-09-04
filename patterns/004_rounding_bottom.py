from __future__ import annotations

from analysis.indicator_engine import IndicatorEngine
from data.ohlcv_store import OHLCVStore
from data.tv_client import MarketSnapshot
from patterns._rounding import find_rounding_setup
from patterns._rules import notional_qty
from patterns.base_pattern import (
    ANN_ENTRY,
    ANN_LINE,
    ANN_STOP,
    ANN_TARGET,
    ANN_TROUGH,
    BasePattern,
    TradeSignal,
    ann_hline,
    ann_marker,
)


class RoundingBottomPattern(BasePattern):
    MIN_BARS = 121
    POSITION_NOTIONAL = 10_000.0

    @property
    def name(self) -> str:
        return "pattern_004_rounding_bottom"

    @property
    def timeframes(self) -> list[str]:
        return ["1d"]

    @property
    def chart_description(self) -> str:
        return "Concave-up rounding bottom with a fifteen-to-fifty-percent cup, oversold bottom RSI, bullish RSI recovery, and a two-day higher-high/higher-low entry."

    def analyze(self, snapshot: MarketSnapshot, store: OHLCVStore) -> TradeSignal | None:
        df = store.get_df(snapshot.symbol, snapshot.timeframe, min_bars=self.MIN_BARS)
        if df is None:
            return None
        ind = IndicatorEngine(df)
        setup = find_rounding_setup(ind, ind.rsi_wilder(14), len(df) - 1, "bottom")
        if setup is None or setup.entry != len(df) - 1:
            return None
        price = float(ind.close.iloc[setup.entry])
        stop = round(price * 0.95, 4)
        target = round(setup.target, 4)
        return TradeSignal(
            symbol=snapshot.symbol,
            action="BUY",
            pattern=self.name,
            timeframe=snapshot.timeframe,
            confidence=1.0,
            price=price,
            qty=notional_qty(self.POSITION_NOTIONAL, price),
            stop_loss=stop,
            stop_loss_on_close=True,           # .cjs rb_v3: fixed 5% close-based
            take_profit=target,
            trailing_stop_pct=0.15,            # .cjs: highestHigh x 0.85
            trailing_stop_mode="highest_high",
            trailing_stop_on_close=True,
            trailing_activation_pct=0.0,
            notes=f"Rounding bottom start={setup.start} bottom={setup.center} depth={setup.depth:.1%} fit={setup.fit:.1%} RSI={setup.center_rsi:.1f} divergence={setup.divergence}",
            chart_annotations=[
                ann_hline(setup.neckline, "neckline", ANN_LINE),
                ann_marker(self.bar_date(df, setup.center), setup.center_close, "bottom", ANN_TROUGH, "^", "below"),
                ann_hline(stop, "stop", ANN_STOP),
                ann_hline(target, "target", ANN_TARGET),
                ann_marker(self.bar_date(df, setup.entry), price, "entry", ANN_ENTRY, "o", "below"),
            ],
        )
