"""Causal swing/RSI decisions and the paper strategy selection path."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from unittest.mock import AsyncMock, Mock, patch

import numpy as np
import pandas as pd
import pytest

from core.execution import opening_candidate
from core.paper_books import PaperBook, PaperBookManager, paper_run_settings, paper_run_status
from core.paper_trader import PaperAccount
from core.rsi_divergence import POLICY, RSIDivergenceIndicator
from core.scanner import MarketScanner
from data.ohlcv_store import OHLCVStore
from data.tv_client import MarketSnapshot, OHLCVCandle


def _frame(action: str = "SELL") -> pd.DataFrame:
    close = np.interp(np.arange(55), [0, 10, 20, 30, 38, 44, 52, 54],
                      [96, 100, 96, 100, 125, 110, 128, 120])
    if action == "BUY":
        close = 250 - close
    return pd.DataFrame({"open": close - 0.1, "high": close + 0.4,
                         "low": close - 0.4, "close": close, "volume": 1_000_000.0},
                        index=pd.bdate_range("2022-01-03", periods=len(close)))


def _context(df: pd.DataFrame, timeframe: str = "1d") -> tuple[OHLCVStore, MarketSnapshot]:
    candles = [OHLCVCandle(row.open, row.high, row.low, row.close, row.volume,
                          (date.tz_localize("UTC") + pd.Timedelta(hours=16)).to_pydatetime())
               for date, row in df.iterrows()]
    store = OHLCVStore(window=300)
    store.replace_all("TEST", timeframe, candles)
    last = candles[-1]
    return store, MarketSnapshot(symbol="TEST", timeframe=timeframe, timestamp=last.timestamp,
                                 candle=last, indicators={}, summary={}, oscillators={}, moving_avgs={})


def _signal(df: pd.DataFrame):
    store, snapshot = _context(df)
    return RSIDivergenceIndicator().analyze(snapshot, store)


@pytest.mark.parametrize("action", ["SELL", "BUY"])
def test_real_wilder_divergence_has_a_confirmed_structural_bracket(action):
    df = _frame(action)
    signal = _signal(df)
    assert signal is not None and signal.action == action
    assert signal.provenance == "rsi_divergence"
    first, second = signal.pattern_evidence["pivots"]
    if action == "SELL":
        assert first["price"] < second["price"] and first["rsi"] > second["rsi"]
        assert signal.stop_loss > df.high.iloc[52]
        assert signal.take_profit == pytest.approx(df.low.iloc[39:52].min())
        assert signal.take_profit < signal.price < signal.stop_loss
    else:
        assert first["price"] > second["price"] and first["rsi"] < second["rsi"]
        assert signal.stop_loss < df.low.iloc[52]
        assert signal.take_profit == pytest.approx(df.high.iloc[39:52].max())
        assert signal.stop_loss < signal.price < signal.take_profit
    assert signal.stop_loss_on_close is False
    assert signal.trailing_stop_pct is None
    assert signal.pattern_evidence["confirmed_date"] == df.index[-1].strftime("%Y-%m-%d")
    assert signal.pattern_evidence["policy"] == POLICY
    assert signal.chart_annotations[-1]["label"] == "RSI Divergence"


@pytest.mark.parametrize("action", ["SELL", "BUY"])
def test_no_signal_before_second_pivot_is_confirmed_and_no_repeat(action):
    df = _frame(action)
    assert _signal(df.iloc[:53]) is None
    assert _signal(df.iloc[:54]) is None
    original = _signal(df)
    assert original is not None
    future = pd.concat([df, df.iloc[[-1]].set_axis([df.index[-1] + pd.Timedelta(days=3)])])
    assert _signal(future) is None
    assert _signal(future.iloc[:55]) == original


@pytest.mark.parametrize("action", ["SELL", "BUY"])
def test_waits_for_first_reversal_close_and_expires(action):
    df = _frame(action)
    close = 127.8 if action == "SELL" else 122.2
    df.iloc[-1, :4] = [close, close + 0.1, close - 0.1, close]
    assert _signal(df) is None
    extra = df.iloc[[-1]].set_axis([df.index[-1] + pd.Timedelta(days=3)])
    df = pd.concat([df, extra])
    df.iloc[-1, :4] = [124, 125, 123, 124] if action == "SELL" else [126, 127, 125, 126]
    assert _signal(df) is not None
    # Let a separate setup wait beyond the five-candle confirmation window.
    waiting = pd.concat([df.iloc[:-1], *[extra.set_axis([extra.index[0] + pd.Timedelta(days=i)])
                                      for i in range(5)]])
    waiting.iloc[-1, :4] = df.iloc[-1, :4]
    assert _signal(waiting) is None


@pytest.mark.parametrize("action", ["SELL", "BUY"])
def test_rejects_passed_target_and_new_extreme(action):
    df = _frame(action)
    df.iloc[-1, :4] = [100, 101, 99, 100] if action == "SELL" else [150, 151, 149, 150]
    assert _signal(df) is None
    df = _frame(action)
    df.iloc[-1, df.columns.get_loc("high" if action == "SELL" else "low")] = (
        130 if action == "SELL" else 120)
    assert _signal(df) is None


def test_rsi_must_diverge_and_bad_bars_do_not_generate_signals():
    df = _frame()
    with patch("core.rsi_divergence.IndicatorEngine.rsi_wilder", return_value=pd.Series(np.full(len(df), 70.0))):
        assert _signal(df) is None
    df.iloc[25, df.columns.get_loc("close")] = np.nan
    assert _signal(df) is None
    assert _signal(_frame().iloc[:20]) is None


def test_paper_chart_uses_wilder_rsi_and_saved_oscillator_pivots():
    from analysis.chart_renderer import build_trade_viewer_payload
    from analysis.indicator_engine import IndicatorEngine
    df = _frame()
    signal = _signal(df)
    payload = build_trade_viewer_payload(df, symbol="TEST", pattern=signal.pattern,
                                        annotations=signal.chart_annotations)
    expected = IndicatorEngine(df).rsi_wilder(14)
    assert payload["rsi14"][-1]["value"] == pytest.approx(expected.iloc[-1])
    assert len(payload["rsi_segments"]) == 1
    assert [point["value"] for point in payload["rsi_segments"][0]["data"]] == [
        pivot["rsi"] for pivot in signal.pattern_evidence["pivots"]]
    assert len(payload["segments"]) == 1
    # A weekly inspection must not present the saved daily RSI as weekly RSI.
    signal.chart_annotations[-1]["timeframe"] = "1W"
    assert build_trade_viewer_payload(df, symbol="TEST", pattern=signal.pattern,
                                     annotations=signal.chart_annotations)["rsi_segments"] == []


def test_scanner_selects_only_divergence_and_preserves_its_stop(monkeypatch):
    from config import settings
    monkeypatch.setattr(settings, "stop_loss_pct", 0.03)
    scanner = MarketScanner(symbols=["TEST"], market="us", volume_gate=False,
                            rsi_divergence=True)
    scanner._discover_patterns()
    assert [p.name for p in scanner._patterns] == ["indicator_rsi_divergence"]
    assert scanner._indicators_only and not scanner._patterns_v2 and not scanner._pattern_only
    scanner._open_analyze_pool()
    assert scanner._analyze_pool is None
    signal = _signal(_frame())
    before = deepcopy(signal)
    scanner._apply_fixed_stop(signal, None)
    assert signal == before


@pytest.mark.parametrize("weekly", [False, True])
def test_rsi_scan_stays_off_the_pattern_detectors(monkeypatch, weekly):
    scanner = MarketScanner(symbols=["TEST"], market="us", rsi_divergence=True)
    scanner._discover_patterns()
    df = _frame()
    df = pd.concat([df.iloc[[0] * 10], df]).set_axis(pd.bdate_range("2021-12-20", periods=65))
    store, snapshot = _context(df, "1W" if weekly else "1d")
    scanner._store = store
    detector = Mock(side_effect=AssertionError("patterns must not scan an RSI book"))
    monkeypatch.setattr("patterns_v2.signals.detect_raw", detector)
    monkeypatch.setattr("core.pattern_loader.discover", detector)
    n_eval, hits = scanner._analyze_local(snapshot)
    assert n_eval == 1 and hits[0].provenance == "rsi_divergence"
    scanner._emit_signals = AsyncMock()
    asyncio.run(scanner._run_analyzes([snapshot]))
    assert scanner._emit_signals.call_args.args[1] == hits
    detector.assert_not_called()


def test_rsi_book_admits_divergence_without_pattern_catalog(monkeypatch):
    from config import settings
    monkeypatch.setattr(settings, "short_entries_enabled", False)
    monkeypatch.setattr("core.paper_trader._max_open_per_symbol",
                        Mock(side_effect=AssertionError("RSI has no pinned pattern catalog")))
    book = PaperAccount(market="us", txn_cost_pct=0, slippage_pct=0)
    book.assume_session_open = True
    scanner = MarketScanner(symbols=["TEST"], market="us", paper_account=book,
                            rsi_divergence=True)
    scanner._discover_patterns()
    scanner._store, snapshot = _context(_frame())
    signal = RSIDivergenceIndicator().analyze(snapshot, scanner._store)
    scanner._finish_signal = AsyncMock()
    asyncio.run(scanner._process_signal(signal, scanner._patterns[-1], snapshot.candle))
    scanner._finish_signal.assert_awaited_once()
    assert book.indicators_only and not scanner._patterns_v2
    ok, reason = book.queue_position(signal, snapshot.candle, scanner._store)
    assert ok, reason
    opening = deepcopy(snapshot.candle)
    opening.timestamp += pd.Timedelta(days=3)
    outcomes = book.fill_pending("TEST", "1d", opening, scanner._store)
    assert len(outcomes) == 1 and outcomes[0][1], outcomes
    trade = book.positions["TEST"][0]
    assert trade.provenance == "rsi_divergence"
    assert trade.stop_loss == signal.stop_loss
    assert trade.take_profit == signal.take_profit


@pytest.mark.parametrize("action", ["SELL", "BUY"])
def test_next_open_keeps_levels_and_rejects_open_beyond_stop(action):
    signal = _signal(_frame(action))
    signal.signal_bar_timestamp = pd.Timestamp("2022-03-18", tz="UTC").to_pydatetime()
    candle = OHLCVCandle(signal.price, signal.price + 1, signal.price - 1, signal.price,
                         1_000_000, pd.Timestamp("2022-03-21", tz="UTC").to_pydatetime())
    candidate = opening_candidate(signal, candle, 10_000, "us", slippage=0.0005)
    assert candidate.stop_loss == signal.stop_loss
    assert candidate.take_profit == signal.take_profit
    assert candidate.pattern_evidence == signal.pattern_evidence
    candle.open = signal.stop_loss + (1 if action == "SELL" else -1)
    with pytest.raises(ValueError):
        opening_candidate(signal, candle, 10_000, "us", slippage=0.0005)


def test_run_settings_export_frozen_strategy_and_source_hashes():
    from core.run_manifest import build_run_manifest
    cfg = paper_run_settings(market="us", n_symbols=10, use_stream=False, interval_seconds=60,
                             rsi_divergence=True, patterns_v2=True, pattern_only=True)
    assert cfg["rsi_divergence"]
    assert cfg["indicators_only"]
    assert cfg["patterns_v2"] is False
    assert cfg["pattern_only"] is False
    assert cfg["patternsv2_enabled_patterns"] == []
    assert "RSI Divergence=ON" in paper_run_status(cfg)
    assert "Patternsv2=OFF" in paper_run_status(cfg)
    manifest = build_run_manifest(market="us", patterns_v2=False, rsi_divergence=True)
    assert manifest["rsi_divergence_policy"] == POLICY
    assert "analysis/indicator_engine.py" in manifest["source_sha256"]
    assert "patterns_v2/signals.py" not in manifest["source_sha256"]
    cfg["rsi_divergence_policy"]["rsi_period"] = 2
    assert POLICY["rsi_period"] == 14


def test_book_thread_receives_rsi_alone_and_refuses_pattern_modes():
    book = PaperBook("us")
    with patch("core.paper_books.threading.Thread") as thread:
        assert book.start(10, use_stream=False, volume_gate=False,
                          rsi_divergence=True) is None
    args = thread.call_args.kwargs["args"]
    assert args[-1] is True
    assert args[4:7] == (False, True, False)
    for extra in ({"patterns_v2": True}, {"pattern_only": True}):
        refused = PaperBook("us")
        with patch("core.paper_books.threading.Thread") as thread:
            message = refused.start(10, use_stream=False, volume_gate=False,
                                    rsi_divergence=True, **extra)
        assert message and "cannot be combined" in message
        thread.assert_not_called()
    manager = PaperBookManager()
    with patch.object(manager, "start", return_value=None) as start:
        assert manager.start_both({mid: {"n_symbols": 10, "rsi_divergence": True,
                                        "patterns_v2": False}
                                   for mid in ("us", "ph")}) == {}
    assert len(start.call_args_list) == 2
    assert all(call.kwargs["rsi_divergence"] for call in start.call_args_list)
    assert all(call.kwargs["patterns_v2"] is False for call in start.call_args_list)


@pytest.mark.parametrize("other", [
    {"rule_id": "test"},
    {"tv_stack_id": "test"},
    {"patterns_v2": True},
    {"pattern_only": True},
])
def test_conflicting_strategy_refused_before_start(other):
    book = PaperBook("us")
    assert "cannot be combined" in book.start(10, use_stream=False, volume_gate=False,
                                               rsi_divergence=True, **other)
    with pytest.raises(ValueError, match="cannot be combined"):
        MarketScanner(symbols=["TEST"], rsi_divergence=True, **other)
