"""Mandatory daily DII bulls confirmation across entry boundaries."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta
from unittest.mock import AsyncMock, Mock

import numpy as np
import pandas as pd
import pytest

from analysis.indicator_engine import IndicatorEngine
from analysis.pattern_dii import describe_dii_bulls_uptrend_rejection
from core.entry_policy import describe_buy_entry_rejection
from core.paper_trader import PaperAccount
from core.scanner import MarketScanner
from data.ohlcv_store import OHLCVStore
from data.tv_client import OHLCVCandle
from patterns.base_pattern import TradeSignal


def daily_candles(seed: int = 9) -> list[OHLCVCandle]:
    close = 100 + np.cumsum(np.random.default_rng(seed).normal(size=100))
    dates = pd.bdate_range("2024-01-02", periods=len(close), tz="UTC") + pd.Timedelta(hours=21)
    return [OHLCVCandle(c, c + 1, c - 1, c, 1_000_000, day.to_pydatetime())
            for c, day in zip(close, dates)]


def store_for(candles) -> OHLCVStore:
    store = OHLCVStore()
    store.replace_all("TEST", "1d", candles)
    return store


def candidate(candles, action="BUY", timeframe="1d") -> TradeSignal:
    price = candles[-1].close
    short = action == "SELL"
    return TradeSignal(
        symbol="TEST", timeframe=timeframe, pattern="double_top" if short else "double_bottom",
        action=action, confidence=1, price=price, qty=1,
        stop_loss=price * (1.2 if short else .8), take_profit=price * (.7 if short else 1.3),
        signal_bar_timestamp=candles[-1].timestamp, provenance="patternsv2",
        pattern_evidence={"breakout_level": price, "direction": -1 if short else 1},
    )


@pytest.mark.parametrize("values,accepted", [
    ([16, 20, 25, 30], True), ([60, 65, 70, 75], True),
    ([20, 25, 25, 30], True), ([20, 30, 25, 35], True),
    ([35, 30, 25, 20], False), ([30, 20, 25, 35], True),
    ([np.nan, 20, 25, 30], True), ([10, 20, np.inf, 30], False),
    ([20, 25, 30], True), ([20, 25], True),
    ([0, 0, 0, 0], False), ([20, 25, 25], False), ([25, 20], False),
    ([np.nan, 20], False), ([20, np.nan], False), ([30, np.inf], False),
    ([20], False), ([], False),
])
def test_requires_one_strict_increase_in_latest_bulls(monkeypatch, values, accepted):
    # Rising raw event counts cannot rescue flat/falling bulls percentages.
    monkeypatch.setattr(IndicatorEngine, "directional_imbalance_index", lambda *a: {
        "bulls": pd.Series(values), "up": pd.Series([1, 2, 3, 4]),
        "bears": pd.Series([90, 80, 70, 60]),
    })
    frame = pd.DataFrame({key: np.ones(61) for key in ("open", "high", "low", "close", "volume")})
    reason = describe_dii_bulls_uptrend_rejection(frame)
    assert (reason is None) == accepted
    if reason:
        assert reason.startswith("DII bulls")
        if len(values) < 2 or not np.isfinite(values[-2:]).all():
            assert "need 2 finite daily bulls readings" in reason
        else:
            assert "1 completed trading day before BUY" in reason
            readings = " → ".join(f"{value:.2f}%" for value in values[-2:])
            assert reason.endswith(f"latest readings: {readings}.")


@pytest.mark.parametrize("count", [0, 59, 60])
def test_short_daily_history_fails_closed(count):
    frame = store_for(daily_candles()[:count]).get_df("TEST", "1d", min_bars=1)
    assert "61 completed daily candles" in describe_dii_bulls_uptrend_rejection(frame)


def test_real_bulls_calculation_and_no_balance_fallback():
    frame = store_for(daily_candles()).get_df("TEST", "1d")
    values = IndicatorEngine(frame).directional_imbalance_index()["bulls"].iloc[-2:]
    assert values.to_numpy() == pytest.approx([40, 50])
    assert describe_dii_bulls_uptrend_rejection(frame) is None
    assert describe_dii_bulls_uptrend_rejection(frame.tail(61)) is None
    frame["high"], frame["low"] = 500 - np.arange(100.), np.arange(100.)
    assert "unavailable" in describe_dii_bulls_uptrend_rejection(frame)


@pytest.mark.parametrize("timeframe", ["1d", "1W"])
def test_weekly_and_daily_buys_use_daily_bulls(timeframe):
    candles = daily_candles()
    signal = candidate(candles, timeframe=timeframe)
    assert describe_buy_entry_rejection(signal, store_for(candles), market="us") is None
    weekly_only = OHLCVStore()
    weekly_only.replace_all("TEST", "1W", candles)
    assert "daily candles" in describe_buy_entry_rejection(signal, weekly_only, market="us")


def test_duplicate_sessions_cannot_supply_the_streak():
    candles = daily_candles()[:60]
    candles += [replace(candles[-1], close=candles[-1].close + .01)]
    assert "61 completed daily candles" in describe_buy_entry_rejection(
        candidate(candles), store_for(candles), market="us")


def test_stale_daily_history_cannot_confirm_a_later_buy():
    candles = daily_candles()
    signal = candidate(candles)
    signal.signal_bar_timestamp += timedelta(days=7)
    assert "session" in describe_buy_entry_rejection(signal, store_for(candles), market="us")


def test_opening_check_skips_weekends_and_us_holidays():
    candles = daily_candles()
    dates = pd.bdate_range(end="2024-03-28", periods=len(candles), tz="UTC") + pd.Timedelta(hours=21)
    candles = [replace(c, timestamp=d.to_pydatetime()) for c, d in zip(candles, dates)]
    # Good Friday (29 March) and the weekend have no daily candle.
    opening = pd.Timestamp("2024-04-01 13:30", tz="UTC").to_pydatetime()
    assert describe_buy_entry_rejection(candidate(candles), store_for(candles), market="us",
                                        as_of=opening, at_open=True) is None


def test_missing_store_is_a_rejection_instead_of_an_exception():
    assert "daily candles" in describe_buy_entry_rejection(candidate(daily_candles()), None, market="us")


@pytest.mark.parametrize("market", ["us", "ph"])
def test_future_daily_bars_are_excluded_and_utc_date_labels_are_kept(market):
    candles = [replace(c, timestamp=c.timestamp.replace(hour=0)) for c in daily_candles()]
    signal = candidate(candles)
    # A later crashing session breaks the streak but cannot affect this decision.
    future = replace(candles[-1], low=1, timestamp=candles[-1].timestamp + timedelta(days=1))
    store = store_for(candles + [future])
    assert describe_buy_entry_rejection(signal, store, market=market) is None
    assert describe_buy_entry_rejection(candidate(candles + [future]), store, market=market) is not None


@pytest.mark.parametrize("mode", ["shadow", "filter"])
@pytest.mark.parametrize("indicators_only", [False, True])
def test_scanner_cannot_shadow_or_bypass_failed_bulls(mode, indicators_only):
    scanner = MarketScanner(symbols=["TEST"], market="us", indicators_only=indicators_only,
                            paper_account=PaperAccount(market="us"), dii_gate_mode=mode, volume_gate=False)
    scanner._append_signal_log = Mock()
    scanner._finish_signal = AsyncMock()
    candles = daily_candles()
    asyncio.run(scanner._process_signal(candidate(candles), candle=candles[-1]))
    scanner._finish_signal.assert_not_awaited()
    assert scanner.stats["dii_rejected"] == 1
    assert scanner._append_signal_log.call_args.kwargs["status"] == "rejected"
    assert "daily candles" in scanner._append_signal_log.call_args.kwargs["reason"]


def test_scanner_allows_confirmed_bulls_in_shadow_and_records_policy(monkeypatch):
    candles = daily_candles()
    book = PaperAccount(market="us")
    scanner = MarketScanner(symbols=["TEST"], market="us", paper_account=book,
                            dii_gate_mode="shadow", volume_gate=False, pattern_only=True)
    scanner._store = store_for(candles)
    scanner._append_signal_log = Mock()
    scanner._finish_signal = AsyncMock()
    monkeypatch.setattr("core.scanner.describe_risk_gate_rejection", lambda *a, **kw: None)
    asyncio.run(scanner._process_signal(candidate(candles), candle=candles[-1]))
    scanner._finish_signal.assert_awaited_once()
    # 50% bulls fails the optional band, but passes the mandatory rising check.
    assert scanner.stats["dii_shadow"] == 1
    assert book.run_manifest["active_controls"]["dii_bulls_uptrend"] == {
        "mode": "filter", "timeframe": "1d", "days": 1}


@pytest.mark.parametrize("entry_path", ["queue_position", "open_position"])
def test_direct_paper_buys_require_confirmation_before_consuming_cash_or_setup(entry_path):
    candles = daily_candles()
    book = PaperAccount(market="us")
    book.assume_session_open = True
    ok, reason = getattr(book, entry_path)(candidate(candles), candles[-1], OHLCVStore())
    assert not ok and reason.startswith("DII bulls")
    assert book.cash == book.initial_capital
    assert not book.positions and not book.pending_entries and not book.consumed_anchors


def test_opening_recheck_excludes_execution_days_historical_range():
    candles = daily_candles()
    book = PaperAccount(market="us", slippage_pct=0)
    book.assume_session_open = True
    store = store_for(candles)
    assert book.queue_position(candidate(candles), candles[-1], store)[0]
    opening = replace(candles[-1], low=1, timestamp=candles[-1].timestamp + timedelta(days=1))
    store.apply_candle("TEST", "1d", opening)
    assert describe_buy_entry_rejection(candidate(candles + [opening]), store, market="us") is not None
    result = book.fill_pending("TEST", "1d", opening, store)
    assert len(result) == 1 and result[0][1], result
    assert book.open_count() == 1


def test_saved_order_cannot_bypass_missing_daily_history(tmp_path):
    candles = daily_candles()
    book = PaperAccount(market="us", slippage_pct=0)
    book.assume_session_open = True
    assert book.queue_position(candidate(candles), candles[-1], store_for(candles))[0]
    path = tmp_path / "book.json"
    book.save(path)
    book = PaperAccount.load(path, market="us")
    book.assume_session_open = True
    opening = replace(candles[-1], timestamp=candles[-1].timestamp + timedelta(days=1))
    result = book.fill_pending("TEST", "1d", opening, OHLCVStore())
    assert len(result) == 1 and not result[0][1]
    assert result[0][2].startswith("DII bulls")
    assert book.cash == book.initial_capital and not book.pending_entries and not book.positions


@pytest.mark.parametrize("action", ["SELL", "CLOSE"])
def test_buy_guard_does_not_gate_shorts_or_exits(action):
    signal = candidate(daily_candles(), action=action)
    assert describe_buy_entry_rejection(signal, OHLCVStore(), market="us") is None


def test_forming_daily_candle_cannot_complete_the_streak(monkeypatch):
    candles = daily_candles()
    monkeypatch.setattr("core.market.is_closed_session_bar",
                        lambda tf, ts, **kw: ts.date() != candles[-1].timestamp.date())
    assert describe_buy_entry_rejection(candidate(candles), store_for(candles), market="us") is not None


def test_utc_midnight_label_is_not_a_completed_bar_during_its_session(monkeypatch):
    # Only the final session supplies a rise: 0% -> 0% -> 6.25%.
    candles = [replace(c, timestamp=c.timestamp.replace(hour=0)) for c in daily_candles(seed=10)]
    assert describe_buy_entry_rejection(candidate(candles), store_for(candles), market="us") is None
    during_session = pd.Timestamp(candles[-1].timestamp.date(), tz="America/New_York") + pd.Timedelta(hours=10)
    monkeypatch.setattr("core.market._session_now", lambda *a, **kw: during_session.to_pydatetime())
    assert describe_buy_entry_rejection(candidate(candles), store_for(candles), market="us") is not None


@pytest.mark.parametrize("seed,accepted", [(9, True), (0, False)])
@pytest.mark.parametrize("collect_first", [0, 4])
def test_stream_replay_uses_the_same_daily_guard_and_causal_open(seed, accepted, collect_first):
    from core.backtest_params import settings_from_values
    from core.stream_backtest import StreamReplay

    candles = daily_candles(seed)
    decision = candles[-1]
    candles.append(replace(decision, low=1, timestamp=decision.timestamp + timedelta(days=1)))

    class Pattern:
        name = "double_bottom"
        timeframes = ["1d"]
        MIN_BARS = 2

        def analyze(self, snapshot, store):
            return candidate(candles[:100]) if snapshot.candle.timestamp == decision.timestamp else None

    rows = [[c.timestamp.isoformat(), c.open, c.high, c.low, c.close, c.volume] for c in candles]
    settings = settings_from_values(dict(
        mode="historical-stream", market="us", symbols=["TEST"],
        start_date=decision.timestamp.date().isoformat(),
        end_date=candles[-1].timestamp.date().isoformat(), warmup_bars=1,
        volume_gate=False, collect_first=collect_first,
    ))
    replay = StreamReplay(dataset={"TEST": rows}, patterns=[Pattern()], settings=settings,
                          session_tz="America/New_York")
    events = replay.run()["signals"]["events"]
    assert any(e["status"] == "filled" for e in events) == accepted
    assert any(e["status"] == "rejected" and e.get("reason", "").startswith("DII bulls")
               for e in events) == (not accepted)


@pytest.mark.parametrize("seed,accepted", [(9, True), (0, False)])
def test_offline_walk_uses_only_daily_history_at_the_decision(seed, accepted):
    from core.backtester import _core_backtest_symbol

    candles = daily_candles(seed)
    decision = candles[-1]
    candles.extend(replace(decision, low=1, timestamp=decision.timestamp + timedelta(days=i))
                   for i in range(1, 12))

    class Pattern:
        name = "double_bottom"
        timeframes = ["1d"]
        HORIZON_BARS = 1

        def analyze(self, snapshot, store):
            return candidate(candles[:100]) if snapshot.candle.timestamp == decision.timestamp else None

    trades, found, _, filtered = _core_backtest_symbol(
        "TEST", "1d", candles, [Pattern()], {"market": "us", "position_notional": 10_000})
    assert found == 1
    assert bool(trades) == accepted
    assert any(row["reason"].startswith("DII bulls") for row in filtered) == (not accepted)


@pytest.mark.parametrize("has_daily", [False, True])
def test_offline_weekly_walk_receives_separate_daily_context(has_daily):
    from core.backtester import _core_backtest_symbol

    daily = daily_candles()
    dates = pd.bdate_range(end="2024-05-17", periods=len(daily), tz="UTC") + pd.Timedelta(hours=21)
    daily = [replace(c, timestamp=d.to_pydatetime()) for c, d in zip(daily, dates)]
    decision = daily[-1]
    weeks = pd.date_range(end=decision.timestamp, periods=100, freq="W-FRI")
    weekly = [replace(decision, timestamp=d.to_pydatetime()) for d in weeks]
    weekly.append(replace(decision, low=1, timestamp=decision.timestamp + timedelta(days=7)))
    # Saved daily tape extends past the weekly decision; it still cannot look ahead.
    daily.append(replace(decision, low=1, timestamp=decision.timestamp + timedelta(days=3)))

    class Pattern:
        name = "double_bottom"
        timeframes = ["1W"]
        HORIZON_BARS = 1

        def analyze(self, snapshot, store):
            return candidate(daily[:100], timeframe="1W") if snapshot.candle.timestamp == decision.timestamp else None

    trades, found, _, filtered = _core_backtest_symbol(
        "TEST", "1W", weekly, [Pattern()], {"market": "us", "position_notional": 10_000},
        daily_candles=daily if has_daily else None,
    )
    assert found == 1 and bool(trades) == has_daily
    assert bool(filtered) == (not has_daily)


@pytest.mark.parametrize("timeframe", ["1d", "1W"])
@pytest.mark.parametrize("index,accepted", [(99, True), (100, False)])
@pytest.mark.parametrize("indicators_only", [False, True])
def test_explorer_reads_daily_bulls_at_each_detection(monkeypatch, timeframe, index, accepted, indicators_only):
    from web.services import ExplorerService

    candles = daily_candles()
    candles.append(replace(candles[-1], low=1, timestamp=candles[-1].timestamp + timedelta(days=1)))
    signal = candidate(candles[:index + 1], timeframe=timeframe)
    signal.signal_bar_idx = index
    calls = []

    def fetch(symbol, tf, **kwargs):
        calls.append(tf)
        return candles

    monkeypatch.setattr("web.services.fetch_ohlcv_candles", fetch)
    monkeypatch.setattr("web.services.latest_signals_over_lookback", lambda *a, **kw: [signal])
    monkeypatch.setattr("web.services.passes_min_confidence", lambda *a: True)
    monkeypatch.setattr("web.services.passes_regime_filter", lambda *a: True)
    # Keep the existing optional band permissive to isolate the required trend.
    monkeypatch.setattr("analysis.pattern_dii.LONG_MIN_BULLS", 40)
    monkeypatch.setattr("analysis.pattern_dii.LONG_MAX_BULLS", 60)
    service = ExplorerService()
    service._patterns = []
    service._indicators = []
    service._renderer.render = Mock(return_value=b"png")
    result = service.load_symbol("TEST", "NASDAQ", timeframe, indicators_only=indicators_only, volume_gate=False)
    assert bool(result["signals"]) == accepted
    assert calls == (["1d"] if timeframe == "1d" else ["1W", "1d"])


@pytest.mark.parametrize("batched", [False, True])
def test_weekly_scans_fetch_daily_context_first_without_daily_entries(monkeypatch, batched):
    from types import SimpleNamespace
    from core.backtester import _make_snapshot

    candles = daily_candles()
    requests, analyzed = [], []

    class Feed:
        async def fetch_snapshot(self, symbol, timeframe, *, store, **kwargs):
            requests.append(timeframe)
            store.replace_all(symbol, timeframe, candles)
            return _make_snapshot(symbol, timeframe, candles[-1])

    feed = Feed()
    if batched:
        async def fetch_many(symbols, timeframe, **kwargs):
            return {s: await feed.fetch_snapshot(s, timeframe, **kwargs) for s in symbols}
        feed.fetch_snapshots = fetch_many
    scanner = MarketScanner(symbols=["TEST"], market="us", data_feed=feed)
    monkeypatch.setattr(scanner, "_note_worker", lambda: None)
    scanner._patterns = [SimpleNamespace(timeframes=["1W"])]
    scanner._has_ready_pattern = lambda *a: True

    async def analyze(snapshots):
        analyzed.extend(s.timeframe for s in snapshots)

    monkeypatch.setattr(scanner, "_run_analyzes", analyze)
    asyncio.run(scanner._scan_all(feed_sessions=[object()]))
    assert requests == ["1d", "1W"]
    assert analyzed == ["1W"]


@pytest.mark.parametrize("seed,direction,accepted", [(9, "long", True), (0, "long", False), (0, "short", True)])
def test_saved_rule_replay_requires_bulls_before_long_fills(monkeypatch, seed, direction, accepted):
    import core.rule_backtest as replay
    from core.rule_contracts import RulePayload
    from core.test_rule_backtest import payload

    candles = daily_candles(seed)
    candles.append(replace(candles[-1], low=1, timestamp=candles[-1].timestamp + timedelta(days=1)))
    frame, dates = replay.frame_from_candles(candles)
    values = [False] * len(frame)
    values[99] = True
    monkeypatch.setattr(replay, "evaluate_series", lambda *a: pd.Series(values))
    raw = payload().model_dump()
    raw["direction"] = direction
    rule = RulePayload.model_validate(raw)
    trades = replay._replay("TEST", frame, dates, rule, notional=10_000, txn_cost=0, slippage=0,
                            start_date=None, end_date=None)
    assert bool(trades) == accepted
