"""Laya historical-stream experiment checks (docs/laya.md P4 + section 15)."""
from __future__ import annotations

import unittest
from datetime import date


def _settings():
    from core.pattern_editor_contracts import (
        BacktestSettings, ExecutionSettings, ReplayWindow,
    )

    window = ReplayWindow(start_date=date(2026, 2, 6), session_count=10,
                          warmup_bars=30)
    execution = ExecutionSettings(
        initial_capital=100_000.0, sizing_mode="fixed-notional",
        position_notional=10_000.0, txn_cost_pct=0.0, slippage_pct=0.0,
        pattern_only=False, volume_gate=False, collect_first=0,
        end_policy="keep-open")
    return BacktestSettings(mode="historical-stream", market="us",
                            symbols=("AAA",), universe=None, window=window,
                            execution=execution)


def _dataset(n: int = 60):
    from datetime import datetime, timedelta, timezone

    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows = []
    price = 100.0
    for i in range(n):
        price += 0.5 if i % 2 == 0 else -0.3
        stamp = (base + timedelta(days=i)).isoformat()
        rows.append([stamp, price - 1, price + 1, price - 1.5, price, 1_000_000])
    return {"AAA": rows}


class _Pattern:
    name = "test_pattern"
    timeframes = ("1d",)
    MIN_BARS = 2

    def __init__(self, fire_once: bool = True):
        self._fire_once = fire_once
        self._fired = False

    def analyze(self, snapshot, store):
        if not self._fire_once or self._fired:
            return None
        self._fired = True
        from patterns.base_pattern import TradeSignal

        close = float(snapshot.candle.close)
        return TradeSignal(
            symbol=snapshot.symbol, timeframe="1d", action="BUY",
            pattern=self.name, confidence=0.8, price=close, qty=10.0,
            stop_loss=close * 0.95, take_profit=close * 1.1,
            setup_key=(12345,), chart_annotations=[],
        )


class StreamLayaTests(unittest.TestCase):
    def test_off_baseline_and_shadow_equality(self) -> None:
        from core.stream_backtest import StreamReplay

        base = StreamReplay(dataset=_dataset(), patterns=[_Pattern()],
                            settings=_settings(), session_tz="America/New_York")
        base_result = base.run()
        shadow = StreamReplay(
            dataset=_dataset(), patterns=[_Pattern()], settings=_settings(),
            session_tz="America/New_York", laya_mode="shadow",
            laya_assess=lambda _cand: {"outcome": "allow", "reasons": ["t"]})
        shadow_result = shadow.run()
        self.assertEqual(base_result["trades"]["meta"]["trades"],
                         shadow_result["trades"]["meta"]["trades"])
        laya_events = [e for e in shadow.events if str(e.get("status", "")).startswith("laya-")]
        self.assertTrue(laya_events)
        # Shadow fills carry the verdict stamp for export attribution.
        stamped = [t for series in shadow.series.values()
                   for t in list(series.open_positions) + list(series.closed)
                   if getattr(t, "laya_assessment", None)]
        self.assertTrue(stamped)
        self.assertTrue(all(s["mode"] == "shadow" and s["outcome"] == "allow"
                            for s in (t.laya_assessment for t in stamped)))

    def test_fake_filter_veto_reproduces(self) -> None:
        from core.stream_backtest import StreamReplay

        veto = StreamReplay(
            dataset=_dataset(), patterns=[_Pattern()], settings=_settings(),
            session_tz="America/New_York", laya_mode="filter",
            laya_assess=lambda _cand: {"outcome": "veto", "reasons": ["test veto"]})
        result = veto.run()
        self.assertEqual(result["trades"]["meta"]["trades"], 0)
        self.assertEqual(result["laya"]["stats"]["vetoed"], 1)

    def test_missing_decision_never_bypasses_filter(self) -> None:
        from core.stream_backtest import StreamReplay

        filt = StreamReplay(dataset=_dataset(), patterns=[_Pattern()],
                            settings=_settings(), session_tz="America/New_York",
                            laya_mode="filter", laya_assess=None)
        result = filt.run()
        self.assertEqual(result["trades"]["meta"]["trades"], 0)
    def test_laya_rejects_collect_first(self) -> None:
        from core.pattern_edit_store import EditError
        from core.pattern_editor_contracts import (
            BacktestSettings, ExecutionSettings, ReplayWindow,
        )

        window = ReplayWindow(start_date=date(2026, 1, 6), session_count=10,
                              warmup_bars=5)
        execution = ExecutionSettings(
            initial_capital=100_000.0, sizing_mode="fixed-notional",
            position_notional=10_000.0, txn_cost_pct=0.0, slippage_pct=0.0,
            pattern_only=False, volume_gate=False, collect_first=2,
            end_policy="keep-open")
        settings = BacktestSettings(mode="historical-stream", market="us",
                                    symbols=("AAA",), universe=None,
                                    window=window, execution=execution)
        from core.stream_backtest import StreamReplay

        with self.assertRaises(EditError):
            StreamReplay(dataset=_dataset(), patterns=[_Pattern()],
                         settings=settings, session_tz="America/New_York",
                         laya_mode="filter",
                         laya_assess=lambda _c: {"outcome": "allow", "reasons": []})

    def test_open_trade_copies_laya_verdict(self) -> None:
        from datetime import datetime, timezone

        from core.backtester import _open_trade
        from data.tv_client import OHLCVCandle
        from patterns.base_pattern import TradeSignal

        signal = TradeSignal(
            symbol="AAA", timeframe="1d", action="BUY", pattern="test",
            confidence=0.8, price=100.0, qty=10.0, stop_loss=95.0,
            take_profit=110.0, setup_key=(1,),
        )
        # The assessment is additive evidence, copied into the saved trade.
        signal.laya_assessment = {"assessment_id": "asm-9", "policy_id": "pol-9",
                                  "mode": "shadow", "outcome": "allow"}
        candle = OHLCVCandle(open=100.0, high=101.0, low=99.0, close=100.0,
                             volume=1.0,
                             timestamp=datetime(2026, 1, 6, tzinfo=timezone.utc))
        position = _open_trade(signal, candle, 5)
        self.assertEqual(position.laya_assessment, signal.laya_assessment)
        # A copy, not a shared reference.
        signal.laya_assessment["outcome"] = "veto"
        self.assertEqual(position.laya_assessment["outcome"], "allow")

    def test_open_trade_defaults_to_no_laya(self) -> None:
        from datetime import datetime, timezone

        from core.backtester import _open_trade
        from data.tv_client import OHLCVCandle
        from patterns.base_pattern import TradeSignal

        signal = TradeSignal(
            symbol="AAA", timeframe="1d", action="BUY", pattern="test",
            confidence=0.8, price=100.0, qty=10.0, stop_loss=95.0,
            take_profit=110.0, setup_key=(1,),
        )
        candle = OHLCVCandle(open=100.0, high=101.0, low=99.0, close=100.0,
                             volume=1.0,
                             timestamp=datetime(2026, 1, 6, tzinfo=timezone.utc))
        self.assertIsNone(_open_trade(signal, candle, 5).laya_assessment)


if __name__ == "__main__":
    unittest.main()
