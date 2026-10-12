"""Money-path regressions using local synthetic bars, without a pattern database."""

from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
import json
import threading

import pytest

from core.backtest_params import settings_from_values
from core.backtester import (
    _apply_notional_sizing, _check_exit, _close_trade, _core_backtest_symbol, _open_trade,
    initial_risk_pct, trade_r_multiple, trade_risk_dollars,
)
from core.paper_trader import PaperAccount
from core.stream_backtest import StreamReplay
from data.tv_client import OHLCVCandle
from patterns.base_pattern import TradeSignal


def candle(price=100.0, index=0):
    return OHLCVCandle(price, price, price, price, 1000,
                      datetime(2026, 1, 1, 20, tzinfo=timezone.utc) + timedelta(days=index))


def signal(action="BUY", **overrides):
    values = dict(symbol="TEST", action=action, pattern="test", timeframe="1d",
                  confidence=1.0, price=100.0, qty=100.0)
    values.update(overrides)
    return TradeSignal(**values)


class AnchorPattern:
    name = "test"
    timeframes = ["1d"]
    HORIZON_BARS = 1

    def __init__(self, *, cap=None, action="BUY", keyed=True, entry_index=30):
        self.MAX_OPEN_PER_SYMBOL = cap
        self.action = action
        self.keyed = keyed
        self.entry_timestamp = candle(index=entry_index).timestamp
        self.calls = 0

    def analyze(self, snapshot, store):
        if snapshot.timestamp != self.entry_timestamp or self.calls >= 3:
            return None
        self.calls += 1
        return signal(self.action, price=snapshot.candle.close,
                      setup_key=(self.calls,) if self.keyed else None)


def replay(pattern, *, end=34, start=30, slippage=0.0, market="us", rows=None):
    if rows is None:
        rows = []
        for i in range(60):
            bar = candle(100.0 if i <= start else 110.0, i)
            rows.append([bar.timestamp.isoformat(), bar.open, bar.high, bar.low,
                         bar.close, bar.volume])
        rows[-1][1:5] = [999.0] * 4
    settings = settings_from_values({
        "mode": "historical-stream", "market": market, "symbols": ["TEST"],
        "start_date": candle(index=start).timestamp.date().isoformat(),
        "end_date": candle(index=end).timestamp.date().isoformat(),
        "warmup_bars": 30, "initial_capital": 100000.0, "position_notional": 10000.0,
        "txn_cost_pct": 0.001, "slippage_pct": slippage, "end_policy": "force-close",
    })
    return StreamReplay(dataset={"TEST": rows}, patterns=[pattern], settings=settings,
                        session_tz="America/New_York")


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf"), 0.0, -1.0])
def test_invalid_prices_cannot_create_or_poison_paper_positions(value):
    account = PaperAccount(market="us")
    account.assume_session_open = True
    account.indicators_only = True
    before = account.cash
    assert account.open_position(signal(price=value), candle(value), None)[0] is False
    assert account.on_bar("TEST", candle(value), "1d") == []
    assert account.cash == before
    assert account.equity() == before
    assert account.open_count() == 0
    assert account.bar_count("TEST", "1d") == 0


@pytest.mark.parametrize("field", ["price", "notional"])
@pytest.mark.parametrize("value", [float("nan"), float("inf"), 0.0, -1.0])
def test_sizing_rejects_invalid_price_or_budget(field, value):
    trade_signal = signal(price=value if field == "price" else 100.0)
    _apply_notional_sizing(trade_signal, value if field == "notional" else 10000.0)
    assert trade_signal.qty == 0.0


def test_close_action_cannot_open_a_short():
    account = PaperAccount(market="us")
    account.assume_session_open = True
    assert account.open_position(signal("CLOSE"), candle(), None)[0] is False
    with pytest.raises(ValueError, match="BUY or SELL"):
        _open_trade(signal("CLOSE"), candle(), 0)
    assert account.open_count() == 0


@pytest.mark.parametrize("price", [float("nan"), float("inf"), 0.0, -1.0])
def test_invalid_exit_leaves_trade_unchanged(price):
    position = _open_trade(signal(), candle(), 0)
    before = asdict(position)
    with pytest.raises(ValueError, match="exit price"):
        _close_trade(position, price, "time_exit", candle(index=1))
    assert asdict(position) == before


@pytest.mark.parametrize("action", ["BUY", "SELL"])
def test_slippage_marks_and_indicator_exit_accounting_survive_restart(tmp_path, action):
    account = PaperAccount(market="us", slippage_pct=0.01, txn_cost_pct=0.001)
    account.assume_session_open = True
    account.indicators_only = True
    assert account.open_position(signal(action), candle(), None)[0]
    assert account.equity() == pytest.approx(account.initial_capital - 100.0)
    assert account.open_for("TEST")[0].position_marks[-1]["mtm"] == pytest.approx(-100.0)
    path = tmp_path / "paper.json"
    account.save(path)
    account = PaperAccount.load(path, market="us")
    assert account.slippage_pct == 0.01
    account.indicators_only = True
    position = account.open_for("TEST")[0]
    account.on_bar("TEST", candle(110.0, 1), "1d")
    account.exit_indicator("TEST", position, candle(110.0, 1))
    assert position.exit_bar_idx == account.bar_count("TEST", "1d")
    assert account.cash == pytest.approx(account.initial_capital + position.pnl_usd)
    assert account.equity() == pytest.approx(account.cash)
    assert account.exit_indicator("TEST", position, candle(110.0, 1)) is None


@pytest.mark.parametrize("content", ['{broken', '[]', '{"cash": NaN}'])
def test_existing_bad_ledger_fails_closed_without_overwriting(tmp_path, content):
    path = tmp_path / "paper.json"
    path.write_text(content)
    with pytest.raises(ValueError, match="Paper account"):
        PaperAccount.load(path, market="us")
    assert path.read_text() == content


def test_concurrent_saves_cannot_overwrite_a_newer_balance(tmp_path, monkeypatch):
    from core import pattern_edit_store

    account = PaperAccount(market="us")
    first_started = threading.Event()
    release_first = threading.Event()
    second_done = threading.Event()
    original_write = pattern_edit_store.atomic_write
    path = tmp_path / "paper.json"

    def delayed_write(path, data):
        if threading.current_thread().name == "first-save":
            first_started.set()
            assert release_first.wait(3)
        original_write(path, data)

    monkeypatch.setattr(pattern_edit_store, "atomic_write", delayed_write)

    def save_new_balance():
        with account._lock:
            account.cash = 123456.0
        account.save(path)
        second_done.set()

    first = threading.Thread(target=account.save, args=(path,), name="first-save")
    second = threading.Thread(target=save_new_balance)
    first.start()
    assert first_started.wait(3)
    second.start()
    second_done.wait(0.1)
    release_first.set()
    first.join(3)
    second.join(3)
    assert not first.is_alive() and not second.is_alive()
    assert json.loads(path.read_text())["cash"] == 123456.0


@pytest.mark.parametrize("cap,keyed", [(1, True), (None, False)])
def test_each_engine_limits_fills_while_draining_a_signal_bar(cap, keyed):
    bars = [candle(index=i) for i in range(40)]
    trades, _, _, _ = _core_backtest_symbol(
        "TEST", "1d", bars, [AnchorPattern(cap=cap, keyed=keyed)], {"market": "us"},
    )
    assert len(trades) == 1
    assert trades[0].exit_bar_idx == len(bars) - 1
    artifacts = replay(AnchorPattern(cap=cap, keyed=keyed)).run()
    assert artifacts["metrics"]["trade_count"] == 1


def test_long_only_market_rejects_short_entries_in_both_backtest_modes():
    bars = [candle(index=i) for i in range(40)]
    trades, _, _, _ = _core_backtest_symbol(
        "TEST", "1d", bars, [AnchorPattern(action="SELL")], {"market": "ph"},
    )
    assert trades == []
    assert replay(AnchorPattern(action="SELL"), market="ph").run()["metrics"]["trade_count"] == 0


@pytest.mark.parametrize("action", ["BUY", "SELL"])
def test_stream_force_close_uses_last_replayed_bar_and_updates_net_equity(action):
    engine = replay(AnchorPattern(cap=1, action=action, entry_index=45),
                    start=45, end=49, slippage=0.01)
    artifacts = engine.run()
    trade = artifacts["trades"]["trades"][0]
    assert trade["entryPrice"] == pytest.approx(101.0 if action == "BUY" else 99.0)
    assert trade["exitPrice"] == pytest.approx(108.9 if action == "BUY" else 111.1)
    assert trade["exitDate"] == candle(index=49).timestamp.isoformat()
    assert trade["barsHeld"] == 4
    last = artifacts["equity"][-1]
    assert last["unrealized_pnl"] == 0.0
    assert last["realized_pnl"] == pytest.approx(artifacts["metrics"]["realized_pnl"])
    assert last["equity"] == pytest.approx(100000.0 + last["realized_pnl"])
    assert len({point["date"] for point in artifacts["equity"]}) == len(artifacts["equity"])


def test_stream_records_duplicate_and_unordered_bars_during_initialization():
    rows = [[bar.timestamp.isoformat(), bar.open, bar.high, bar.low, bar.close, bar.volume]
            for bar in (candle(index=i) for i in range(40))]
    rows.insert(5, list(rows[4]))
    rows.insert(9, list(rows[1]))
    engine = replay(AnchorPattern(cap=1), rows=rows)
    assert [event["status"] for event in engine.events] == ["duplicate-bar", "unordered-bar"]
    assert len(engine.series["TEST"].candles) == 40


@pytest.mark.parametrize("action,stop,ratcheted,price", [
    ("BUY", 90.0, 104.0, 110.0), ("SELL", 110.0, 96.0, 90.0),
])
def test_initial_risk_uses_fill_snapshot_and_effective_dual_stop(action, stop, ratcheted, price):
    position = _open_trade(signal(action, stop_loss=stop, stop_loss_pct_cap=0.05), candle(), 0)
    position.stop_loss = ratcheted
    assert trade_risk_dollars(position) == pytest.approx(500.0)
    assert trade_r_multiple(position, price) == pytest.approx(2.0)
    assert initial_risk_pct(position) == pytest.approx(0.05)


def test_pinned_positions_allow_known_identical_exit_runtime_but_reject_unknown_changes():
    position = _open_trade(signal(stop_loss=95.0, pattern_version_id="saved"), candle(), 0)
    position.resolved_rules["engine_sha256"] = (
        "f7826eea5c0be44afd549d892ddd4ca4d54860ce1dd1ae91935ddaa526d9c83b"
    )
    assert _check_exit(candle(90.0, 1), position, 1) == (95.0, "stop_loss")
    position.resolved_rules["engine_sha256"] = "unverified-engine"
    with pytest.raises(RuntimeError, match="runtime changed"):
        _check_exit(candle(90.0, 1), position, 1)


def test_historical_ph_lots_use_bar_date_in_all_execution_paths(monkeypatch):
    from core import market

    # The wall clock is after the migration, while this replay is before it.
    monkeypatch.setattr(market, "_PSETRADEX_LOT1_ON", date(2026, 3, 1))
    bars = [candle(137.0, i) for i in range(40)]
    trades, _, _, _ = _core_backtest_symbol(
        "TEST", "1d", bars, [AnchorPattern(cap=1)], {"market": "ph", "lot_round": True},
    )
    assert trades[0].qty == 70.0
    rows = [[bar.timestamp.isoformat(), bar.open, bar.high, bar.low, bar.close, bar.volume]
            for bar in bars]
    streamed = replay(AnchorPattern(cap=1), market="ph", rows=rows).run()
    assert streamed["trades"]["trades"][0]["shares"] == 70.0
    account = PaperAccount(market="ph")
    account.assume_session_open = True
    account.indicators_only = True
    assert account.open_position(signal(price=137.0), bars[30], None)[0]
    assert account.open_for("TEST")[0].qty == 70.0


@pytest.mark.parametrize("action", ["BUY", "SELL"])
def test_stream_regular_exit_applies_slippage(action):
    class TimedPattern(AnchorPattern):
        def analyze(self, snapshot, store):
            detected = super().analyze(snapshot, store)
            if detected is not None:
                detected.exit_bars_after_entry = 1
            return detected

    artifacts = replay(TimedPattern(cap=1, action=action), slippage=0.01).run()
    trade = artifacts["trades"]["trades"][0]
    assert trade["exitReason"] == "time_exit"
    assert trade["exitPrice"] == pytest.approx(108.9 if action == "BUY" else 111.1)
    assert trade["barsHeld"] == 1
