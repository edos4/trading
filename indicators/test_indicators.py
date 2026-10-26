"""Indicator math and the explorer's indicators-only scan."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

from data.ohlcv_store import OHLCVStore
from data.tv_client import MarketSnapshot, OHLCVCandle
from indicators import discover
from indicators._math import flip_action, floor_pivots, supertrend
from indicators._signal import reward_bracket
from indicators.pivot_points import PivotPointsIndicator
from indicators.supertrend import SupertrendIndicator


def _series(n=240, seed=1):
    rng = np.random.default_rng(seed)
    close = np.empty(n)
    close[0] = 100.0
    drift = np.where(np.arange(n) < n // 2, -0.35, 0.45)
    for i in range(1, n):
        close[i] = max(5.0, close[i - 1] + drift[i] + rng.normal(0, 0.35))
    high = close + rng.uniform(0.2, 0.8, n)
    low = close - rng.uniform(0.2, 0.8, n)
    open_ = close + rng.normal(0, 0.15, n)
    return open_, high, low, close


def _store(open_, high, low, close, start="2022-01-03"):
    idx = pd.bdate_range(start, periods=len(close))
    candles = [
        OHLCVCandle(float(o), float(h), float(l), float(c), 1_000_000.0, ts.to_pydatetime())
        for ts, o, h, l, c in zip(idx, open_, high, low, close)
    ]
    store = OHLCVStore(window=len(candles) + 5)
    store.replace_all("TEST", "1d", candles)
    last = candles[-1]
    snap = MarketSnapshot(
        symbol="TEST", timeframe="1d", timestamp=last.timestamp or datetime.now(),
        candle=last, indicators={}, summary={}, oscillators={}, moving_avgs={},
    )
    return store, snap


def test_discover_lists_the_ported_indicators():
    names = {item.name for item in discover()}
    assert names == {
        "indicator_supertrend",
        "indicator_vol_gated_supertrend",
        "indicator_ut_bot_dema",
        "indicator_ott",
        "indicator_gaussian_channel",
        "indicator_supertrend_ai",
        "indicator_dema_supertrend",
        "indicator_pivot_points",
        "indicator_directional_imbalance_index",
    }


def test_supertrend_buy_on_the_flip_bar():
    open_, high, low, close = _series()
    st = supertrend(high, low, close, SupertrendIndicator.ATR_PERIOD, SupertrendIndicator.MULTIPLIER)
    ups = np.flatnonzero((st.trend[1:] > 0) & (st.trend[:-1] < 0))
    assert len(ups) >= 1
    end = int(ups[0]) + 2  # inclusive flip bar, plus the slice end
    store, snap = _store(open_[:end], high[:end], low[:end], close[:end])
    signal = SupertrendIndicator().analyze(snap, store)
    assert signal is not None
    assert signal.action == "BUY"
    assert signal.take_profit is None
    assert signal.stop_loss_on_close is True
    assert signal.stop_loss < signal.price
    assert flip_action(st.trend[:end]) == "BUY"
    trail = SupertrendIndicator().trail(
        store.get_df("TEST", "1d", min_bars=SupertrendIndicator.MIN_BARS)
    )
    assert trail is not None
    assert trail[0] == "BUY"
    assert trail[1] == pytest.approx(signal.stop_loss, abs=1e-3)


def test_floor_pivot_cross_is_a_long():
    # February sets P=100, R1=110, S1=90. March holds under P, then closes through it.
    feb = pd.bdate_range("2024-02-01", "2024-02-29")
    march = pd.bdate_range("2024-03-01", "2024-03-29")
    idx = feb.append(march)
    close = np.full(len(idx), 99.0)
    close[: len(feb)] = 100.0
    close[-1] = 105.0
    high = np.full(len(idx), 101.0)
    low = np.full(len(idx), 99.0)
    high[: len(feb)] = 110.0
    low[: len(feb)] = 90.0
    candles = [
        OHLCVCandle(float(c), float(h), float(l), float(c), 1_000_000.0, ts.to_pydatetime())
        for ts, h, l, c in zip(idx, high, low, close)
    ]
    store = OHLCVStore(window=len(candles) + 5)
    store.replace_all("TEST", "1d", candles)
    last = candles[-1]
    snap = MarketSnapshot(
        symbol="TEST", timeframe="1d", timestamp=last.timestamp,
        candle=last, indicators={}, summary={}, oscillators={}, moving_avgs={},
    )
    signal = PivotPointsIndicator().analyze(snap, store)
    pivots = floor_pivots(110.0, 90.0, 100.0)
    assert signal is not None
    assert signal.action == "BUY"
    assert signal.stop_loss == pytest.approx(pivots.s1, abs=1e-4)
    assert signal.take_profit == pytest.approx(pivots.r1, abs=1e-4)


def test_each_indicator_accepts_a_reversing_series():
    open_, high, low, close = _series(n=260, seed=3)
    store, snap = _store(open_, high, low, close)
    for indicator in discover():
        signal = indicator.analyze(snap, store)
        if signal is None:
            continue
        assert signal.pattern == indicator.name
        assert signal.action in ("BUY", "SELL")
        if signal.take_profit is None:
            assert signal.stop_loss_on_close is True
            if signal.action == "BUY":
                assert signal.stop_loss < signal.price
            else:
                assert signal.price < signal.stop_loss
        elif signal.action == "BUY":
            assert signal.stop_loss < signal.price < signal.take_profit
        else:
            assert signal.take_profit < signal.price < signal.stop_loss


def test_indicators_only_skips_pattern_discovery():
    from analysis.chart_renderer import ChartRenderer
    from web.services import ExplorerService

    open_, high, low, close = _series(n=80, seed=2)
    idx = pd.bdate_range("2023-01-02", periods=len(close))
    candles = [
        OHLCVCandle(float(o), float(h), float(l), float(c), 1_000_000.0, ts.to_pydatetime())
        for ts, o, h, l, c in zip(idx, open_, high, low, close)
    ]

    class Dummy:
        name = "indicator_dummy"
        timeframes = ["1d", "1W"]
        MIN_BARS = 2

        def analyze(self, snapshot, store):
            return None

    with patch("web.services.fetch_ohlcv_candles", return_value=candles), \
         patch("web.services.discover_patterns", side_effect=AssertionError("patterns")), \
         patch("web.services.discover_indicators", return_value=[Dummy()]), \
         patch.object(ChartRenderer, "render", return_value=b"png"):
        service = ExplorerService()
        result = service.load_symbol(
            "AAA", "NASDAQ", "1d",
            run_patterns=True, indicators_only=True,
            volume_gate=False,
        )
    assert result["scan"] == "indicators"
    assert result["signals"] == []


def test_paper_scanner_loads_indicators_instead_of_patterns():
    from core.scanner import MarketScanner

    scanner = MarketScanner.__new__(MarketScanner)
    scanner._indicators_only = True
    scanner._disabled_patterns = set()
    scanner._discover_patterns()
    names = {item.name for item in scanner._patterns}
    assert "indicator_supertrend" in names
    assert scanner._version_set == {}
    assert all(name.startswith("indicator_") for name in names)


def test_indicators_only_does_not_touch_pattern_database(monkeypatch):
    """A down pattern editor must not stall or stop an indicators-only scan."""
    from core.pattern_editor_db import DatabaseUnavailable
    from core.scanner import MarketScanner

    def boom(*_args, **_kwargs):
        raise DatabaseUnavailable("PostgreSQL unavailable or operation timed out")

    monkeypatch.setattr("core.pattern_versions.PatternVersions.resolve", boom)
    monkeypatch.setattr("core.pattern_loader.acknowledge_worker", boom)

    scanner = MarketScanner.__new__(MarketScanner)
    scanner._indicators_only = True
    scanner._disabled_patterns = set()
    scanner._analyze_pool = None
    scanner._running = True
    scanner._discover_patterns()
    scanner._note_worker()
    scanner.stop()
    assert scanner._version_set == {}
    assert scanner._patterns


def test_indicators_only_book_cap_does_not_apply_to_patterns(monkeypatch):
    from core.paper_trader import PaperAccount
    from patterns.base_pattern import TradeSignal

    monkeypatch.setattr("core.paper_trader._max_open_per_symbol", lambda _name: None)

    def sig(pattern, symbol, key):
        return TradeSignal(
            symbol=symbol, action="BUY", pattern=pattern, timeframe="1d",
            confidence=1.0, price=50.0, qty=0.0, stop_loss=45.0,
            take_profit=60.0, setup_key=(key,),
        )

    candle = OHLCVCandle(
        open=50.0, high=51.0, low=49.0, close=50.0, volume=1_000,
        timestamp=datetime(2026, 3, 5),
    )

    class Empty:
        def get_df(self, symbol, timeframe, min_bars=1):
            return None

    capped = PaperAccount(initial_capital=100_000.0, market="us", slippage_pct=0.0, txn_cost_pct=0.0)
    capped.assume_session_open = True
    capped.indicators_only = True
    ok, _ = capped.open_position(sig("indicator_supertrend", "NVDA", 1), candle, Empty())
    assert ok
    ok, message = capped.open_position(sig("indicator_ott", "NVDA", 2), candle, Empty())
    assert not ok and "already has an open position" in message
    for i in range(9):
        ok, message = capped.open_position(sig("indicator_supertrend", f"S{i}", i), candle, Empty())
        assert ok, message
    ok, message = capped.open_position(sig("indicator_supertrend", "EXTRA", 99), candle, Empty())
    assert not ok and "already use the account" in message
    assert capped.open_count() == 10

    plain = PaperAccount(initial_capital=100_000.0, market="us", slippage_pct=0.0, txn_cost_pct=0.0)
    plain.assume_session_open = True
    for i in range(11):
        ok, message = plain.open_position(sig("pattern_002_double_top", f"P{i}", i), candle, Empty())
        assert ok, message
    assert plain.open_count() == 11


def test_indicator_stop_ratchets_only_when_indicators_only():
    from core.paper_trader import PaperAccount
    from core.scanner import MarketScanner
    from patterns.base_pattern import TradeSignal

    account = PaperAccount(initial_capital=100_000.0, market="us", slippage_pct=0.0)
    account.assume_session_open = True
    account.indicators_only = True
    signal = TradeSignal(
        symbol="NVDA", action="BUY", pattern="indicator_supertrend",
        timeframe="1d", confidence=1.0, price=50.0, qty=0.0,
        stop_loss=45.0, take_profit=None, stop_loss_on_close=True, setup_key=(1,),
    )
    candle = OHLCVCandle(
        open=50.0, high=52.0, low=49.0, close=50.0, volume=1_000,
        timestamp=datetime(2026, 3, 5),
    )

    class Empty:
        def get_df(self, symbol, timeframe, min_bars=1):
            return None

    ok, message = account.open_position(signal, candle, Empty())
    assert ok, message
    position = account.positions["NVDA"][0]
    account.indicators_only = False
    assert account.ratchet_indicator_stop(position, 48.0) is False
    assert position.stop_loss == pytest.approx(45.0, abs=1e-4)
    assert account.exit_indicator("NVDA", position, candle) is None
    assert account.open_count() == 1

    account.indicators_only = True
    assert account.ratchet_indicator_stop(position, 44.0) is False
    assert account.ratchet_indicator_stop(position, 48.0) is True
    assert position.stop_loss == pytest.approx(48.0, abs=1e-4)

    scanner = MarketScanner.__new__(MarketScanner)
    scanner._indicators_only = False
    scanner._paper = account
    scanner._patterns = []
    scanner._store = None
    scanner._trail_indicators("NVDA", "1d", candle)
    assert account.open_count() == 1


def test_far_stop_is_not_a_trade():
    # AEHL-style short: price 0.45, supertrend line 7.55, 2R target below zero.
    assert reward_bracket("SELL", 0.45, 7.5496) is None
    assert reward_bracket("BUY", 0.0001, -0.0012) is None
    stop, target = reward_bracket("SELL", 100.0, 110.0)
    assert stop == 110.0 and target > 0


def test_indicator_fill_skips_pattern_catalog_only_when_checked(monkeypatch):
    from core.paper_trader import PaperAccount
    from patterns.base_pattern import TradeSignal

    def boom(*_args, **_kwargs):
        raise AssertionError("pattern catalog")

    monkeypatch.setattr("core.paper_trader._max_open_per_symbol", boom)
    account = PaperAccount(initial_capital=100_000.0, market="us", slippage_pct=0.0)
    account.assume_session_open = True
    account.indicators_only = False
    signal = TradeSignal(
        symbol="NVDA", action="BUY", pattern="indicator_supertrend",
        timeframe="1d", confidence=1.0, price=50.0, qty=0.0,
        stop_loss=45.0, take_profit=60.0, setup_key=(1,),
    )
    candle = OHLCVCandle(
        open=50.0, high=51.0, low=49.0, close=50.0, volume=1_000,
        timestamp=datetime(2026, 3, 5),
    )

    class Empty:
        def get_df(self, symbol, timeframe, min_bars=1):
            return None

    with pytest.raises(AssertionError, match="pattern catalog"):
        account.open_position(signal, candle, Empty())

    account.indicators_only = True
    signal = TradeSignal(
        symbol="NVDA", action="BUY", pattern="indicator_supertrend",
        timeframe="1d", confidence=1.0, price=50.0, qty=0.0,
        stop_loss=45.0, take_profit=60.0, setup_key=(1,),
    )
    candle = OHLCVCandle(
        open=50.0, high=51.0, low=49.0, close=50.0, volume=1_000,
        timestamp=datetime(2026, 3, 5),
    )

    class Empty:
        def get_df(self, symbol, timeframe, min_bars=1):
            return None

    ok, message = account.open_position(signal, candle, Empty())
    assert ok, message


def test_paper_pages_have_indicators_only_checkbox():
    from pathlib import Path
    html = Path("web/templates/paper.html").read_text()
    script = Path("web/static/app.js").read_text()
    desktop = Path("ui/paper_dashboard.py").read_text()
    assert 'id="{{ book.id }}-indicators-only"' in html
    assert "Indicators only" in html
    assert "indicators_only" in script
    assert 'text="Indicators only"' in desktop


def test_explorer_template_has_indicators_only_checkbox():
    from pathlib import Path
    html = Path("web/templates/explorer.html").read_text()
    script = Path("web/static/app.js").read_text()
    desktop = Path("ui/app.py").read_text()
    assert 'id="indicators-only"' in html
    assert "Indicators only" in html
    assert "indicators_only" in script
    assert 'text="Indicators only"' in desktop


@pytest.mark.parametrize("direction", [1, -1])
def test_dii_dominance_transition_and_trailing(direction):
    from indicators.directional_imbalance_index import DirectionalImbalanceIndexIndicator
    detector = DirectionalImbalanceIndexIndicator()
    close = np.concatenate([np.full(100, 100.), 100 + direction * np.arange(1., 31.)])
    high, low = close + 1, close - 1
    signals = []
    for end in range(detector.MIN_BARS, len(close)+1):
        store, snap = _store(close[:end], high[:end], low[:end], close[:end])
        signal = detector.analyze(snap, store)
        if signal is not None:
            signals.append((end, signal))
    assert len(signals) == 1
    end, signal = signals[0]
    assert end == 101
    assert signal.action == ("BUY" if direction > 0 else "SELL")
    assert signal.pattern == detector.name
    assert signal.take_profit is None and signal.stop_loss_on_close
    store, _ = _store(close[:end], high[:end], low[:end], close[:end])
    df = store.get_df("TEST", "1d", min_bars=2)
    action, stop = detector.trail(df)
    assert action == signal.action and stop == signal.stop_loss
    assert stop < signal.price if direction > 0 else stop > signal.price
    assert "bulls" in signal.notes and "bears" in signal.notes
    assert detector.trail(df.iloc[:100]) is None  # balanced
    assert detector.trail(df.iloc[:40]) is None
    invalid = df.copy()
    invalid.loc[invalid.index[-1], "high"] = np.nan
    assert detector.trail(invalid) is None
    class Store:
        def get_df(self, *args, **kwargs): return invalid
    assert detector.analyze(snap, Store()) is None


def test_dii_unavailable_and_first_warm_reading_never_emit():
    from indicators.directional_imbalance_index import DirectionalImbalanceIndexIndicator
    detector = DirectionalImbalanceIndexIndicator()
    for n in (0, 40, 80, 82):
        close = np.arange(n, dtype=float) + 100
        class Store:
            def get_df(self, *args, **kwargs):
                return pd.DataFrame(dict(open=close, high=close+1, low=close-1, close=close, volume=1000))
        from types import SimpleNamespace
        assert detector.analyze(SimpleNamespace(symbol="TEST", timeframe="1d"), Store()) is None
    assert detector.timeframes == ["1d", "1W"]
