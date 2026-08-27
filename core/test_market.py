"""Unit tests for Philippine (PSE) market profile plumbing."""

from datetime import date, datetime
from zoneinfo import ZoneInfo

from core.market import (
    PH,
    US,
    apply_lot_rounding,
    bar_identity,
    cash_session_closed,
    clock_payload,
    get_market,
    is_closed_session_bar,
    is_ph_holiday,
    last_closed_session_date,
    may_assume_fill,
    merge_extra_symbols,
    ohlcv_cache_key,
    parse_extra_symbols,
    pse_board_lot,
    pse_tick_size,
    resolve_market_id,
    round_price_to_tick,
    round_qty_to_lot,
    session_window,
    yahoo_chart_symbol,
)
from patterns.base_pattern import TradeSignal


def _sig(**kw) -> TradeSignal:
    base = dict(
        symbol="BDO", timeframe="1d", pattern="pattern_003_double_bottom",
        action="BUY", price=140.0, confidence=0.9, qty=337,
        stop_loss=131.37, take_profit=154.0,
    )
    base.update(kw)
    return TradeSignal(**base)


def test_resolve_and_profiles():
    assert resolve_market_id("PH") == "ph"
    assert resolve_market_id("philippines") == "ph"
    assert resolve_market_id("us") == "us"
    assert get_market("ph") is PH
    assert PH.long_only and PH.currency == "PHP"
    assert PH.tv_screener == "philippines" and PH.tv_exchange == "PSE"
    assert US.txn_cost_pct == 0.001
    assert US.default_n_symbols == 50
    assert US.min_adv == 20_000_000.0
    assert US.min_share_price == 5.0
    assert PH.min_share_price is None
    assert US.universe_order == "value"
    assert abs(PH.txn_cost_pct - 0.0035) < 1e-9
    assert PH.paper_account_path != US.paper_account_path


def test_yahoo_suffix():
    assert yahoo_chart_symbol("BDO", "ph") == "BDO.PS"
    assert yahoo_chart_symbol("PSE:BDO", screener="philippines") == "BDO.PS"
    assert yahoo_chart_symbol("BDO.PS", "ph") == "BDO.PS"
    assert yahoo_chart_symbol("AAPL", "us") == "AAPL"
    assert yahoo_chart_symbol("AAPL", screener="america") == "AAPL"
    assert ohlcv_cache_key("SM", "1d", "ph") == "SM.PS_1d"
    assert ohlcv_cache_key("SM", "1d", "us") == "SM_1d"


def test_ph_history_symbol():
    from core.market import is_ph_history_symbol, ph_history_symbol

    assert ph_history_symbol("BDO") == "BDO.PS"
    assert ph_history_symbol("bdo.ps") == "BDO.PS"
    assert ph_history_symbol("PSE:SM") == "SM.PS"
    assert is_ph_history_symbol("SM.PS")
    assert not is_ph_history_symbol("SM")


def test_board_lots_and_ticks():
    assert pse_board_lot(7.50, as_of=date(2026, 8, 13)) == 100
    assert pse_tick_size(7.50) == 0.01
    assert pse_board_lot(140.0, as_of=date(2026, 8, 13)) == 10
    assert round_qty_to_lot(337, 140.0, as_of=date(2026, 8, 13)) == 330
    assert round_price_to_tick(131.37) == 131.40
    # After PSETradeX go-live, lot is 1.
    assert pse_board_lot(7.50, as_of=date(2026, 11, 23)) == 1
    sig = _sig()
    assert apply_lot_rounding(sig, as_of=date(2026, 8, 13))
    assert sig.qty == 330
    tiny = _sig(qty=5, price=140.0)
    assert not apply_lot_rounding(tiny, as_of=date(2026, 8, 13))
    assert tiny.qty == 0


def test_session_clock_manila():
    tz = ZoneInfo("Asia/Manila")
    # Wednesday 2026-08-12 10:00 AM continuous
    am = datetime(2026, 8, 12, 10, 0, tzinfo=tz)
    assert session_window("ph", am) == "am"
    assert may_assume_fill("ph", am)
    recess = datetime(2026, 8, 12, 12, 15, tzinfo=tz)
    assert session_window("ph", recess) == "recess"
    assert not may_assume_fill("ph", recess)
    pm = datetime(2026, 8, 12, 13, 30, tzinfo=tz)
    assert session_window("ph", pm) == "pm"
    closed = datetime(2026, 8, 12, 16, 0, tzinfo=tz)
    assert session_window("ph", closed) == "closed"
    weekend = datetime(2026, 8, 15, 10, 0, tzinfo=tz)
    assert session_window("ph", weekend) == "closed"
    # US paper still fills any time.
    assert may_assume_fill("us", recess)


def test_ph_holiday_2026():
    assert is_ph_holiday(date(2026, 8, 21))
    assert is_ph_holiday(date(2026, 8, 31))
    assert not is_ph_holiday(date(2026, 8, 13))


def test_merge_extra_symbols_skips_screener_dupes():
    assert parse_extra_symbols("dhi, AT; PSE:BDO  at.ps") == ["DHI", "AT", "BDO"]
    merged = merge_extra_symbols(
        [("BDO", "PSE"), ("SM", "PSE")], "DHI, AT, bdo, SM", "ph",
    )
    names = [s for s, _ex in merged]
    assert names == ["BDO", "SM", "DHI", "AT"]
    assert all(ex == "PSE" for _s, ex in merged)
    us = merge_extra_symbols([("AAPL", "NASDAQ")], "MSFT", "us")
    assert us == [("AAPL", "NASDAQ"), ("MSFT", "NASDAQ")]
    assert merge_extra_symbols([("BDO", "PSE")], "", "ph") == [("BDO", "PSE")]


def test_us_daily_bar_identity_ignores_intraday_prints():
    tz = ZoneInfo("America/New_York")
    rth = datetime(2026, 8, 14, 10, 0, tzinfo=tz)
    t1 = datetime(2026, 8, 14, 10, 5, tzinfo=tz)
    t2 = datetime(2026, 8, 14, 15, 55, tzinfo=tz)
    assert not cash_session_closed("us", rth)
    assert not is_closed_session_bar("1d", t1, market="us", now=rth)
    assert bar_identity("1d", t1, market="us", now=rth) == bar_identity(
        "1d", t2, market="us", now=rth,
    )
    assert bar_identity("1d", t1, market="us", now=rth) == last_closed_session_date(
        "us", rth,
    )
    after = datetime(2026, 8, 14, 16, 5, tzinfo=tz)
    assert cash_session_closed("us", after)
    assert is_closed_session_bar("1d", t1, market="us", now=after)
    assert bar_identity("1d", t1, market="us", now=after) == date(2026, 8, 14)
    # Historical bar vs wall-clock now is always a closed bar.
    old = datetime(2024, 1, 2, 15, 0, tzinfo=tz)
    assert is_closed_session_bar("1d", old, market="us", now=after)
    assert bar_identity("1d", old, market="us", now=after) == date(2024, 1, 2)


def test_engine_kwargs_ph_overlay():
    from core.engine_defaults import backtest_kwargs, ENGINE

    us = backtest_kwargs(market="us")
    assert us["txn_cost_pct"] == ENGINE.txn_cost_pct
    assert us["long_only"] is False
    ph = backtest_kwargs(market="ph")
    assert ph["market"] == "ph"
    assert ph["long_only"] is True
    assert ph["txn_cost_pct"] == PH.txn_cost_pct
    assert ph["account_value"] == PH.paper_initial_capital


def test_paper_ledgers_do_not_mix():
    import tempfile
    from pathlib import Path

    from core.paper_trader import PaperAccount

    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        us_path = tmp / "paper_us.json"
        ph_path = tmp / "paper_ph.json"
        us = PaperAccount(market="us", initial_capital=100_000)
        us.cash = 90_000
        us.save(us_path)
        ph = PaperAccount(market="ph", initial_capital=1_000_000)
        ph.save(ph_path)
        loaded_ph = PaperAccount.load(ph_path, market="ph")
        assert loaded_ph.market == "ph"
        assert loaded_ph.initial_capital == 1_000_000
        mixed = PaperAccount.load(us_path, market="ph")
        assert mixed.cash == 1_000_000  # refused to mix USD ledger into PHP


def test_clock_payload():
    us = clock_payload("us")
    assert us["tz_name"] == "ET"
    assert us["session_open"] is True
    assert len(us["local_time"]) == 5
    sunday = datetime(2026, 8, 16, 12, 0, tzinfo=ZoneInfo("Asia/Manila"))
    ph = clock_payload("ph", now=sunday)
    assert ph["tz_name"] == "PHT"
    assert ph["session_open"] is False


def test_ph_skips_edgar():
    from datetime import date as d

    from data.edgar_client import (
        default_client,
        set_skip_edgar,
        skip_edgar_enabled,
    )

    set_skip_edgar(True)
    try:
        assert skip_edgar_enabled()
        assert default_client().has_earnings_in("SM", d(2026, 1, 1), d(2026, 12, 31)) is False
    finally:
        set_skip_edgar(False)
    assert not skip_edgar_enabled()


def demo():
    test_resolve_and_profiles()
    test_yahoo_suffix()
    test_board_lots_and_ticks()
    test_session_clock_manila()
    test_ph_holiday_2026()
    test_us_daily_bar_identity_ignores_intraday_prints()
    test_engine_kwargs_ph_overlay()
    test_paper_ledgers_do_not_mix()
    test_clock_payload()
    test_ph_skips_edgar()
    test_merge_extra_symbols_skips_screener_dupes()
    print("market: all checks passed")


if __name__ == "__main__":
    demo()
