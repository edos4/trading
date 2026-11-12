"""Observed-price exits protect both sides without inventing intrabar paths."""

from __future__ import annotations

from copy import deepcopy
from datetime import timedelta

import pytest

pytestmark = pytest.mark.usefixtures("confirmed_buy_dii")

from core.paper_trader import PaperAccount
from core.backtester import _check_exit, _make_snapshot, _open_trade
from core.scanner import MarketScanner
from core.test_position_protection import book_with_position
from core.test_patternsv2_exits import candle, signal
from utils.trade_export import build_paper_account_export
from data.ohlcv_store import OHLCVStore


@pytest.mark.parametrize("short", [False, True])
def test_live_entry_cannot_be_stopped_by_the_day_range_before_its_fill(monkeypatch, short):
    book = PaperAccount(market="us", txn_cost_pct=.001, slippage_pct=.0005)
    monkeypatch.setattr("core.paper_trader.may_assume_fill", lambda market: True)
    daily = candle(100, 115, 85, 100)
    assert book.open_position(signal(short), daily, OHLCVStore())[0]
    p = book.positions["TEST"][0]
    assert book.on_bar("TEST", daily, "1d") == []
    assert p.position_marks[-1]["exit_check"]["observation_kind"] == "live_bar_close"
    # A fresh observed move after entry remains executable.
    price = 104 if short else 96
    assert book.on_quote("TEST", candle(price, price, price, price, 1), "1d",
                         observed_at=p.entry_date + timedelta(days=1)) == [p]
    assert p.exit_price == pytest.approx(price * (1.0005 if short else .9995))


@pytest.mark.parametrize("short", [False, True])
def test_forming_bar_stop_fills_at_observed_price_with_costs_and_settles_once(short):
    book, p = book_with_position(short, slip=.002)
    price = 104 if short else 96
    quote = candle(100, 120, 80, price, 1)
    now = p.entry_date + timedelta(seconds=5)
    count = book.bar_count("TEST", "1d")
    assert book.on_quote("TEST", quote, "1d", observed_at=now) == [p]
    assert p.exit_reason == "stop_loss"
    assert p.exit_price == pytest.approx(price * (1.002 if short else .998))
    assert p.pnl_pct < -3  # Do not fabricate a fill at a crossed stop.
    assert book.cash == pytest.approx(book.initial_capital + p.pnl_usd)
    assert book.equity() == pytest.approx(book.cash)
    assert book.bar_count("TEST", "1d") == count
    assert book.on_quote("TEST", quote, "1d", observed_at=now) == []
    trade = build_paper_account_export(book)["books"][0]["closed_trades"][0]
    check = trade["daily_marks"][-1]["exit_check"]
    assert check["observation_kind"] == "live_quote"
    assert check["raw_fill"] == price
    assert check["ohlc"] == dict.fromkeys(("open", "high", "low", "close"), price)


@pytest.mark.parametrize("short", [False, True])
def test_live_trail_uses_observed_price_without_using_forming_extremes_or_arming_pattern(short):
    book, p = book_with_position(short)
    original = deepcopy(p.paper_protection)
    marks = deepcopy(p.position_marks)
    now = p.entry_date + timedelta(seconds=5)
    quote = candle(100, 115, 85, 99 if short else 101, 1)
    assert book.on_quote("TEST", quote, "1d", observed_at=now) == []
    assert p.paper_protection["stop"] < original["stop"] if short else p.paper_protection["stop"] > original["stop"]
    assert 0 < p.paper_protection["peak_net_pct"] < .01
    assert p.paper_protection["last_quote"]["price"] == quote.close
    assert len(p.paper_protection["quote_ratchets"]) == 1
    assert p.position_marks == marks
    assert not p._trailing_activated


@pytest.mark.parametrize("short", [False, True])
@pytest.mark.parametrize("fee,slip", [(0, 0), (.001, .002)])
def test_observed_profit_ratchets_then_reversal_closes_with_three_point_net_giveback(short, fee, slip, tmp_path):
    book, p = book_with_position(short, fee=fee, slip=slip)
    now = p.entry_date + timedelta(seconds=5)
    price = 90 if short else 110
    # The day's unobserved extreme must neither stop nor ratchet the trade.
    assert book.on_quote("TEST", candle(100, 140, 60, price, 1), "1d", observed_at=now) == []
    peak = p.paper_protection["peak_net_pct"] * 100
    stop = p.paper_protection["stop"]
    path = tmp_path / "account.json"
    book.save(path)
    book = PaperAccount.load(path, market="us")
    p = book.positions["TEST"][0]
    assert p.paper_protection["stop"] == stop
    assert book.on_quote("TEST", candle(stop, stop, stop, stop, 1), "1d",
                         observed_at=now + timedelta(seconds=5)) == [p]
    assert p.pnl_pct == pytest.approx(peak - 3)
    assert p.exit_reason == "trailing_stop"
    assert book.cash == pytest.approx(book.initial_capital + p.pnl_usd)
    assert book.open_count() == 0
    exported = build_paper_account_export(book)["books"][0]["closed_trades"][0]
    assert exported["paper_protection"]["quote_ratchets"][0]["price"] == price


@pytest.mark.parametrize("short", [False, True])
@pytest.mark.parametrize("runtime", ["causal-v1", "legacy", "unavailable"])
@pytest.mark.parametrize("timestamp_drift", [False, True])
def test_same_bar_range_cannot_retroactively_fill_a_quote_ratchet(short, runtime, timestamp_drift):
    book, p = book_with_position(short, fee=0)
    if runtime == "legacy":
        p.execution_model = "legacy"
    elif runtime == "unavailable":
        p.resolved_rules["engine_sha256"] = "unavailable"
    now = p.entry_date + timedelta(minutes=5)
    price = 90 if short else 110
    quote = candle(100, 112, 88, price, 1)
    assert book.on_quote("TEST", quote, "1d", observed_at=now) == []
    stop = p.paper_protection["stop"]
    # Both the old open and earlier range cross the new quote-based stop.
    completed = candle(100, 112, 88, 91 if short else 109, 1)
    if timestamp_drift:
        completed.timestamp += timedelta(hours=2)
    assert book.on_bar("TEST", completed, "1d") == []
    assert p.paper_protection["stop"] == stop
    check = p.position_marks[-1]["exit_check"]
    assert check["observation_kind"] == "live_bar_close"
    assert check["ohlc"]["open"] == 100
    assert check["resting_ohlc"]["open"] == completed.close
    # Tomorrow's order was active before the new range began.
    following = candle(91, 99, 90, 95, 2) if short else candle(109, 110, 101, 105, 2)
    assert book.on_bar("TEST", following, "1d") == [p]
    assert p.exit_price == stop
    assert p.pnl_pct == pytest.approx(7)


@pytest.mark.parametrize("short", [False, True])
@pytest.mark.parametrize("reason", ["stop_loss", "trailing_stop"])
def test_live_legacy_close_orders_use_observed_price_with_no_synthetic_stop_fill(short, reason):
    s = signal(short)
    s.provenance = "indicator"
    if reason == "stop_loss":
        s.stop_loss = 101 if short else 99
        s.stop_loss_on_close = True
    else:
        s.trailing_activation_pct = 0
        s.trailing_stop_pct = .01
        s.trailing_stop_on_close = True
    p = _open_trade(s, candle(100, 100, 100, 100), 0)
    close = 102 if short else 98
    full = candle(100, 110, 90, close, 1)
    point = candle(close, close, close, close, 1)
    assert _check_exit(full, p, 1, resting_candle=point) == (close, reason)


@pytest.mark.parametrize("short", [False, True])
def test_close_beyond_live_trail_fills_observed_close_without_inventing_intraday_touch(short):
    book, p = book_with_position(short, fee=0, slip=.002)
    now = p.entry_date + timedelta(minutes=5)
    price = 90 if short else 110
    assert book.on_quote("TEST", candle(100, 112, 88, price, 1), "1d", observed_at=now) == []
    completed = candle(100, 112, 88, 98 if short else 102, 1)
    assert book.on_bar("TEST", completed, "1d") == [p]
    assert p.exit_price == pytest.approx(completed.close * (1.002 if short else .998))
    assert p.exit_reason == "trailing_stop"
    assert book.cash == pytest.approx(book.initial_capital + p.pnl_usd)


@pytest.mark.parametrize("short", [False, True])
def test_stale_quotes_and_bars_cannot_reuse_or_loosen_a_new_live_order(short):
    book, p = book_with_position(short, fee=0)
    now = p.entry_date + timedelta(minutes=5)
    price = 90 if short else 110
    assert book.on_quote("TEST", candle(100, 112, 88, price, 2), "1d", observed_at=now) == []
    policy = deepcopy(p.paper_protection)
    equity, count = book.equity(), book.bar_count("TEST", "1d")
    crossed = candle(100, 120, 80, 115 if short else 85, 1)
    assert book.on_quote("TEST", crossed, "1d", observed_at=now + timedelta(seconds=5)) == []
    crossed.timestamp = candle(0, 0, 0, 0, 2).timestamp
    assert book.on_quote("TEST", crossed, "1d", observed_at=now - timedelta(seconds=1)) == []
    assert book.on_bar("TEST", candle(100, 120, 80, 100, 1), "1d") == []
    assert p.paper_protection == policy
    assert book.equity() == equity
    assert book.bar_count("TEST", "1d") == count


@pytest.mark.parametrize("short", [False, True])
def test_saved_trail_protects_forming_bar_after_restart(short, tmp_path):
    book, p = book_with_position(short)
    completed = candle(100, 101, 88, 90, 1) if short else candle(100, 112, 99, 110, 1)
    assert book.on_bar("TEST", completed, "1d") == []
    path = tmp_path / "account.json"
    book.save(path)
    book = PaperAccount.load(path, market="us")
    p = book.positions["TEST"][0]
    stop = p.paper_protection["stop"]
    price = stop + .1 if short else stop - .1
    quote = candle(price, price, price, price, 2)
    assert book.on_quote("TEST", quote, "1d", observed_at=p.entry_date + timedelta(minutes=5)) == [p]
    assert p.exit_reason == "trailing_stop"
    assert p.pnl_pct > 6
    assert book.cash == pytest.approx(book.initial_capital + p.pnl_usd)


@pytest.mark.parametrize("short", [False, True])
def test_close_decision_waits_for_next_bar_then_fills_first_observed_price(short):
    book, p = book_with_position(short)
    assert book.request_exit(p, "kronos_exit")
    now = p.entry_date + timedelta(minutes=5)
    same = candle(100, 101, 99, 100)
    same.timestamp += timedelta(hours=2)  # A new print is still the same daily bar.
    assert book.on_quote("TEST", same, "1d", observed_at=now) == []
    following = candle(100, 101, 99, 100, 1)
    assert book.on_quote("TEST", following, "1d", observed_at=now + timedelta(days=1)) == [p]
    assert p.exit_reason == "kronos_exit"
    assert p.exit_price == 100


@pytest.mark.parametrize("short", [False, True])
def test_wrong_timeframe_and_pre_entry_observations_cannot_close_a_position(short):
    book, p = book_with_position(short)
    quote = candle(100, 120, 80, 110 if short else 90, 1)
    assert book.on_quote("TEST", quote, "1W", observed_at=p.entry_date + timedelta(seconds=5)) == []
    assert book.on_quote("TEST", quote, "1d", observed_at=p.entry_date - timedelta(seconds=5)) == []
    assert book.open_count() == 1


@pytest.mark.parametrize("short", [False, True])
def test_scanner_checks_forming_live_prices_before_pattern_or_model_work(monkeypatch, short):
    book, p = book_with_position(short)
    book.assume_session_open = False
    scanner = MarketScanner(symbols=['TEST'], market='us', paper_account=book,
                            volume_gate=False, kronos_gate_mode='off', kronos_exit_mode='off')
    scanner._scan_snapshot_symbols, scanner._scan_daily_dates = set(), set()
    monkeypatch.setattr('core.scanner.is_closed_session_bar', lambda *a, **kw: False)
    price = 104 if short else 96
    snapshot = _make_snapshot('TEST', '1d', candle(100, 120, 80, price, 1))
    snapshot.timestamp = p.entry_date + timedelta(minutes=5)
    count = book.bar_count('TEST', '1d')
    assert scanner._apply_bar('TEST', '1d', snapshot) == (False, None)
    assert book.closed == [p]
    assert book.bar_count('TEST', '1d') == count
    assert scanner._cooldown_tracker[(p.symbol, p.pattern)] == (count, True)


@pytest.mark.parametrize("short", [False, True])
def test_quote_respects_close_confirmed_pattern_orders_but_not_for_safety_stop(short):
    book, p = book_with_position(short)
    p.stop_loss = 101 if short else 99
    p.stop_loss_on_close = p.exit_fill_at_close = True
    now = p.entry_date + timedelta(minutes=5)
    price = 102 if short else 98
    assert book.on_quote('TEST', candle(price, price, price, price, 1), '1d', observed_at=now) == []
    price = 104 if short else 96
    assert book.on_quote('TEST', candle(price, price, price, price, 1), '1d', observed_at=now) == [p]
    assert p.exit_reason == 'stop_loss'
