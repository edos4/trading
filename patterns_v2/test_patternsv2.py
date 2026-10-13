"""Patternsv2 checkbox wiring and detector-signal adaptation."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import numpy as np
import pytest

from data.tv_client import MarketSnapshot, OHLCVCandle
from patterns.base_pattern import TradeSignal
from patterns_v2.signals import MIN_BARS, detect_trade_signals


def _frame(n: int = 80) -> pd.DataFrame:
    idx = pd.bdate_range("2024-01-02", periods=n)
    close = pd.Series(range(100, 100 + n), index=idx, dtype=float)
    return pd.DataFrame(
        {
            "open": close,
            "high": close + 1,
            "low": close - 1,
            "close": close,
            "volume": 1_000.0,
        },
        index=idx,
    )


def _raw(**overrides):
    raw = {
        "pattern": "double_bottom",
        "direction": 1,
        "start": 10,
        "end": 40,
        "signal": 50,
        "level": 150.0,
        "score": 5.0,
        "lines": [(10, 120.0, 40, 140.0)],
        "points": [(10, 120.0, "low 1"), (25, 140.0, "neck"), (40, 122.0, "low 2")],
    }
    raw.update(overrides)
    return raw


def test_adapts_breakout_into_trade_signal():
    df = _frame()
    hits = detect_trade_signals(
        df, symbol="AAPL", timeframe="1d", lookback=30,
        detect=lambda h, l, c: [_raw()],
    )
    assert len(hits) == 1
    sig = hits[0]
    assert sig.pattern == "double_bottom"
    assert sig.action == "BUY"
    assert sig.price == float(df["close"].iloc[50])
    assert sig.stop_loss == 120.0
    assert sig.take_profit == 180.0
    assert sig.provenance == "patternsv2"
    assert sig.stop_loss_pct_cap == 0.05
    assert sig.trailing_stop_pct == 0.025
    assert sig.trailing_activation_pct == 0.05
    assert sig.trailing_stop_mode == "highest_close"
    assert sig.trailing_ref_after_check
    assert any(a.get("pattern_version_id") == "patternsv2" for a in sig.chart_annotations)
    assert "Patternsv2 double_bottom" in sig.notes


def test_flag_stop_is_the_flag_rail_not_the_pole():
    df = _frame()
    # Pole runs 100 -> 150. Flag rails at the breakout bar are 148 and 130.
    raw = _raw(
        pattern="bull_flag",
        signal=50,
        level=150.0,
        points=[(10, 100.0, "pole"), (40, 150.0, "pole top")],
        lines=[
            (10, 100.0, 40, 150.0),
            (41, 145.0, 50, 150.0),
            (41, 132.0, 50, 130.0),
        ],
    )
    hits = detect_trade_signals(df, symbol="AAPL", timeframe="1d", detect=lambda h, l, c: [raw])
    assert hits[0].stop_loss == 130.0
    assert hits[0].take_profit == 200.0


def test_cup_stop_is_the_handle_not_the_bowl():
    df = _frame()
    raw = _raw(
        pattern="cup_handle",
        signal=50,
        level=150.0,
        points=[
            (10, 148.0, "left rim"),
            (30, 100.0, "cup"),
            (50, 149.0, "right rim"),
            (60, 140.0, "handle"),
        ],
        lines=[(50, 150.0, 70, 150.0)],
    )
    hits = detect_trade_signals(df, symbol="AAPL", timeframe="1d", detect=lambda h, l, c: [raw])
    assert hits[0].stop_loss == 140.0
    assert hits[0].take_profit == 200.0


def test_channel_stop_is_the_unbroken_rail():
    df = _frame()
    raw = _raw(
        pattern="descending_channel",
        direction=-1,
        start=5,
        end=18,
        signal=20,
        level=100.0,
        points=[(5, 160.0, "high 1"), (8, 110.0, "low 1"), (14, 150.0, "high 2"), (18, 105.0, "low 2")],
        lines=[
            (5, 160.0, 20, 140.0),
            (8, 116.0, 20, 100.0),
        ],
    )
    hits = detect_trade_signals(df, symbol="AAPL", timeframe="1d", detect=lambda h, l, c: [raw])
    assert hits[0].action == "SELL"
    assert hits[0].trailing_stop_mode == "lowest_close"
    assert hits[0].stop_loss == 140.0
    assert hits[0].take_profit == 80.0


def test_breakout_under_one_r_keeps_structural_levels():
    df = _frame()
    # Close 150 is already through level 140, so the measured height (20)
    # no longer covers the stop at 120.
    hits = detect_trade_signals(
        df, symbol="AAPL", timeframe="1d",
        detect=lambda h, l, c: [_raw(level=140.0)],
    )
    assert len(hits) == 1
    assert hits[0].stop_loss == 120.0
    assert hits[0].take_profit == 170.0


@pytest.mark.parametrize("bear", [False, True])
def test_actual_double_breakout_survives_on_signal_bar(bear):
    close = np.interp(np.arange(80), [0, 20, 35, 50, 65, 80],
                      [100, 140, 90, 110, 91, 115])
    if bear:
        close = 250 - close
    df = pd.DataFrame({"open": close, "high": close + 1, "low": close - 1,
                       "close": close, "volume": 1000.0},
                      index=pd.bdate_range("2024-01-02", periods=len(close)))
    hits = detect_trade_signals(df, symbol="TEST", timeframe="1d", latest_bar_only=True)
    name = "double_top" if bear else "double_bottom"
    signal = next(s for s in hits if s.pattern == name)
    assert signal.action == ("SELL" if bear else "BUY")
    assert signal.price == pytest.approx(136.6 if bear else 113.4)
    assert signal.stop_loss == pytest.approx(161.0 if bear else 89.0)
    assert signal.take_profit == pytest.approx(114.6 if bear else 135.4)
    assert 0 < abs(signal.take_profit - signal.price) < abs(signal.stop_loss - signal.price)


@pytest.mark.parametrize("raw", [
    _raw(points=[]),
    _raw(level=float("nan")),
    _raw(points=[(10, -1.0, "low 1")]),
    _raw(level=100.0),  # Failure pivot is on the wrong side of the neckline.
    _raw(pattern="double_top", direction=-1, level=151.0,
         points=[(10, 400.0, "high 1")]),  # Measured target is below zero.
])
def test_invalid_structural_levels_are_still_dropped(raw):
    assert detect_trade_signals(
        _frame(), symbol="TEST", timeframe="1d", detect=lambda *args: [raw],
    ) == []


@pytest.mark.parametrize("bear", [False, True])
def test_actual_cup_breakout_keeps_handle_stop_and_full_bowl_target(bear):
    """Exercise the real detector: closes strictly beyond the rim must survive."""
    close = np.interp(np.arange(100), [0, 20, 30, 40, 50, 70, 78, 83, 99],
                      [50, 100, 80, 75, 77, 100, 94, 106, 110])
    if bear:
        close = 200 - close
    df = pd.DataFrame({"open": close, "high": close + 1, "low": close - 1,
                       "close": close, "volume": 1000.0},
                      index=pd.bdate_range("2024-01-02", periods=len(close)))
    hits = detect_trade_signals(df, symbol="TEST", timeframe="1d")
    name = "inv_cup_handle" if bear else "cup_handle"
    signal = next(s for s in hits if s.pattern == name)
    assert signal.action == ("SELL" if bear else "BUY")
    assert signal.price == pytest.approx(98.8 if bear else 101.2)
    assert signal.stop_loss == pytest.approx(107.0 if bear else 93.0)
    assert signal.take_profit == pytest.approx(71.8 if bear else 128.2)
    assert abs(signal.take_profit - signal.price) >= abs(signal.stop_loss - signal.price)
    signal_bar = 81
    prefix_hits = detect_trade_signals(df.iloc[:signal_bar + 1], symbol="TEST", timeframe="1d",
                                       latest_bar_only=True)
    assert any(s.pattern == name and s.price == signal.price for s in prefix_hits)


def test_converging_pattern_uses_full_width_for_target():
    df = _frame()
    raw = _raw(pattern="symmetrical_triangle", signal=50, start=10, end=40,
               level=149.0, points=[],
               lines=[(10, 160.0, 50, 149.0), (10, 120.0, 50, 140.0)])
    hits = detect_trade_signals(df, symbol="TEST", timeframe="1d", detect=lambda *a: [raw])
    assert len(hits) == 1
    assert hits[0].stop_loss == 140.0
    assert hits[0].take_profit == 190.0


def test_latest_bar_only_drops_older_breakouts():
    df = _frame()
    hits = detect_trade_signals(
        df, symbol="AAPL", timeframe="1d", latest_bar_only=True,
        detect=lambda h, l, c: [
            _raw(signal=70),
            _raw(
                pattern="inv_cup_handle", signal=len(df) - 1, direction=-1, level=180.0,
                points=[(40, 210.0, "cup"), (70, 190.0, "handle")],
            ),
        ],
    )
    assert [s.pattern for s in hits] == ["inv_cup_handle"]
    assert hits[0].action == "SELL"


def test_short_history_returns_nothing():
    assert detect_trade_signals(
        _frame(MIN_BARS - 1), symbol="AAPL", timeframe="1d", detect=lambda *a: [_raw()],
    ) == []


def test_loader_keeps_bot_patterns_package():
    import patterns as bot_patterns
    from patterns_v2.loader import detect_fn

    detect = detect_fn()
    import patterns as still_bot
    import numpy as np

    assert still_bot is bot_patterns
    assert detect_fn() is detect
    loaded = Path(detect.__code__.co_filename).resolve()
    local = Path(__file__).resolve().parent / "patterns" / "__init__.py"
    assert loaded == local
    close = 100 + np.cumsum(np.random.default_rng(0).normal(0, 1, 80))
    assert isinstance(detect(close + 1, close - 1, close), list)


def test_detector_reuses_pivots_and_skips_disabled_patterns(monkeypatch):
    from patterns_v2 import patterns as detectors

    original = detectors.zigzag
    calls = []

    def count(*args):
        calls.append(1)
        return original(*args)

    def disabled(*args, **kwargs):
        raise AssertionError("disabled head-and-shoulders must not be scanned")

    monkeypatch.setattr(detectors, "zigzag", count)
    monkeypatch.setattr(detectors.inv_head_shoulders, "detect", disabled)
    close = 100 + np.cumsum(np.random.default_rng(0).normal(0, 1, 80))
    assert isinstance(detectors.detect(close + 1, close - 1, close), list)
    assert len(calls) == 2


def test_checkboxes_exist_on_web_and_desktop():
    root = Path(__file__).resolve().parents[1]
    explorer = (root / "web/templates/explorer.html").read_text()
    paper = (root / "web/templates/paper.html").read_text()
    script = (root / "web/static/app.js").read_text()
    desktop = (root / "ui/app.py").read_text()
    desk_paper = (root / "ui/paper_dashboard.py").read_text()
    assert 'id="patterns-v2"' in explorer
    assert "Patternsv2" in explorer
    assert 'id="{{ book.id }}-patterns-v2"' in paper
    assert "patterns_v2" in script
    assert 'text="Patternsv2"' in desktop
    assert 'text="Patternsv2"' in desk_paper


def test_scanner_patterns_v2_uses_detector(monkeypatch):
    from core.scanner import MarketScanner

    seen = {}

    def fake(df, **kwargs):
        seen.update(kwargs)
        return [TradeSignal(
            symbol="AAPL", action="BUY", pattern="double_bottom", timeframe="1d",
            confidence=0.5, price=10.0, qty=0.0, provenance="patternsv2",
        )]

    monkeypatch.setattr("patterns_v2.signals.detect_trade_signals", fake)
    scanner = MarketScanner.__new__(MarketScanner)
    scanner._patterns_v2 = True
    scanner._indicators_only = False
    scanner._patterns = []

    class Store:
        def available(self, symbol, timeframe):
            return MIN_BARS

        def get_df(self, symbol, timeframe, min_bars=2):
            return _frame()

    scanner._store = Store()
    snap = MarketSnapshot(
        symbol="AAPL", timeframe="1d", timestamp=datetime.now(timezone.utc),
        candle=OHLCVCandle(1, 1, 1, 1, 1, datetime.now(timezone.utc)),
        indicators={}, summary={}, oscillators={}, moving_avgs={},
    )
    n_eval, hits = scanner._analyze_local(snap)
    assert n_eval == 1
    assert hits[0].pattern == "double_bottom"
    assert seen["latest_bar_only"] is True
    assert seen["symbol"] == "AAPL"


def test_patternsv2_paper_open_skips_pattern_catalog(monkeypatch):
    """A Patternsv2 fill must not open the pattern-editor database.

    That lookup timed out on the Sept 2026 paper replay, aborted the scan
    after the bar was already marked processed, and the breakout was skipped.
    """
    from core.paper_trader import PaperAccount

    def boom(*_a, **_k):
        raise AssertionError("patternsv2 must not consult the pattern catalog")

    monkeypatch.setattr("core.pattern_versions.PatternVersions.catalog", boom)
    acct = PaperAccount(initial_capital=100_000.0, market="us", slippage_pct=0.0)
    acct.assume_session_open = True
    candle = OHLCVCandle(
        open=8.42, high=8.43, low=8.33, close=8.42, volume=8_000,
        timestamp=datetime(2026, 9, 15, tzinfo=timezone.utc),
    )
    signal = TradeSignal(
        symbol="FGBI", action="BUY", pattern="falling_wedge", timeframe="1d",
        confidence=0.92, price=8.42, qty=0.0, setup_key=(10, 40, 50),
        provenance="patternsv2",
    )
    ok, msg = acct.open_position(signal, candle, store=None)
    assert ok, msg
    assert acct.open_count() == 1
    assert acct.open_for("FGBI")[0].pattern == "falling_wedge"


def test_catalog_outage_does_not_abort_paper_open(monkeypatch):
    from core.paper_trader import PaperAccount
    from core.pattern_editor_db import DatabaseUnavailable

    def boom(*_a, **_k):
        raise DatabaseUnavailable("PostgreSQL unavailable or operation timed out")

    monkeypatch.setattr("core.pattern_versions.PatternVersions.catalog", boom)
    acct = PaperAccount(initial_capital=100_000.0, market="us", slippage_pct=0.0)
    acct.assume_session_open = True
    candle = OHLCVCandle(
        open=17.47, high=18.01, low=17.43, close=17.47, volume=2_000_000,
        timestamp=datetime(2026, 9, 25, tzinfo=timezone.utc),
    )
    signal = TradeSignal(
        symbol="RLAY", action="SELL", pattern="double_top", timeframe="1d",
        confidence=0.36, price=17.47, qty=0.0, setup_key=(12, 55, 70),
    )
    ok, msg = acct.open_position(signal, candle, store=None)
    assert ok, msg
    assert acct.open_for("RLAY")[0].action == "SELL"
