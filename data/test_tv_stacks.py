"""Stack rules and a paper fill from a fake TradingView series."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from core.paper_books import PaperBook
from core.paper_trader import PaperAccount
from data.tv_client import MarketSnapshot, OHLCVCandle
from data.tv_stacks import TvStackIndicator, save_stack, validate_stack
from data.tv_studies import TvStudyError


def _stack():
    return validate_stack({
        "name": "sma-of-rsi",
        "timeframe": "1d",
        "studies": [
            {"id": "STD;RSI", "source": {"type": "price", "series": "close"}},
            {"id": "STD;SMA", "source": {"type": "study", "study_index": 0, "plot": "plot_0"}},
        ],
        "signal": {
            "kind": "threshold", "plot": "plot_0", "study_index": 1,
            "level": 50, "direction": "above",
        },
        "stop": {"kind": "percent", "percent": 2},
        "exit_on_opposite": True,
    }, study_limit=9)


def _series(rows):
    bars = []
    parent = []
    child = []
    for ts, close, parent_value, child_value in rows:
        unix = int(ts.timestamp())
        bars.append({
            "time": unix, "open": close, "high": close + 1, "low": close - 1,
            "close": close, "volume": 1000,
        })
        parent.append({"time": unix, "plot_0": parent_value})
        child.append({"time": unix, "plot_0": child_value})
    return {
        "bars": bars,
        "studies": [
            {"index": 0, "id": "STD;RSI", "plots": parent, "strategy_entries": []},
            {"index": 1, "id": "STD;SMA", "plots": child, "strategy_entries": []},
        ],
    }


def _snapshot(when, price):
    candle = OHLCVCandle(price, price + 1, price - 1, price, 1000, when)
    return MarketSnapshot(
        symbol="AAPL", timeframe="1d", timestamp=when, candle=candle,
        indicators={}, summary={}, oscillators={}, moving_avgs={},
    )


def test_threshold_fires_only_on_the_crossing_bar():
    stack = _stack()
    first = datetime(2024, 1, 2, tzinfo=timezone.utc)
    second = datetime(2024, 1, 3, tzinfo=timezone.utc)
    fetched = _series([
        (first, 100, 30, 40),
        (second, 101, 70, 60),
    ])
    assert evaluate_action(stack, fetched, first) is None
    assert evaluate_action(stack, fetched, second) == "BUY"


def evaluate_action(stack, fetched, when):
    from data.tv_stacks import evaluate

    fired = evaluate(stack, fetched, when)
    return None if fired is None else fired["action"]


def test_fake_series_buys_then_trails_out(monkeypatch):
    stack = _stack()
    first = datetime(2024, 1, 2, tzinfo=timezone.utc)
    second = datetime(2024, 1, 3, tzinfo=timezone.utc)
    third = datetime(2024, 1, 4, tzinfo=timezone.utc)
    crossing = _series([(first, 100, 30, 40), (second, 100, 70, 60)])
    flipped = _series([(second, 100, 70, 60), (third, 99, 20, 40)])
    calls = {"n": 0}

    def fetch(symbol, exchange, bound):
        calls["n"] += 1
        return crossing if calls["n"] == 1 else flipped

    indicator = TvStackIndicator(stack, fetch=fetch)
    indicator.bind("AAPL", "1d", "NASDAQ")
    signal = indicator.analyze(_snapshot(second, 100), None)
    assert signal is not None
    assert signal.action == "BUY"
    assert signal.pattern == "sma-of-rsi"
    assert signal.provenance == "tv_indicator"

    account = PaperAccount(market="us")
    account.assume_session_open = True
    account.indicators_only = True
    opened, reason = account.open_position(signal, _snapshot(second, 100).candle, None)
    assert opened, reason
    position = account.positions["AAPL"][0]

    import pandas as pd
    df = pd.DataFrame({"close": [100.0, 99.0]}, index=pd.to_datetime([second, third]))
    assert indicator.trail(df) is None
    closed = account.exit_indicator("AAPL", position, _snapshot(third, 99).candle)
    assert closed is not None
    assert closed.exit_reason == "trailing_stop"
    assert account.open_count() == 0

    book = PaperBook("us")
    previous = book.account
    book.account = account
    book.status = "Running — US, TradingView=sma-of-rsi"
    try:
        snap = book.snapshot()
    finally:
        book.account = previous
    assert snap["closed"][0]["pattern"] == "sma-of-rsi"
    assert "sma-of-rsi" in snap["status"]


def test_missing_study_bar_skips_the_symbol():
    stack = _stack()
    when = datetime(2024, 6, 3, tzinfo=timezone.utc)
    other = datetime(2024, 1, 2, tzinfo=timezone.utc)
    fetched = _series([(other, 100, 30, 40), (datetime(2024, 1, 3, tzinfo=timezone.utc), 100, 70, 60)])
    indicator = TvStackIndicator(stack, fetch=lambda *args: fetched)
    indicator.bind("AAPL", "1d", "NASDAQ")
    assert indicator.analyze(_snapshot(when, 100), None) is None


def test_session_error_does_not_flatten_on_trail():
    stack = _stack()
    indicator = TvStackIndicator(stack, fetch=lambda *args: (_ for _ in ()).throw(
        TvStudyError("expired", "TradingView session expired — update TV_SESSIONID.")
    ))
    indicator.bind("AAPL", "1d", "NASDAQ")
    import pandas as pd
    df = pd.DataFrame({"close": [100.0]}, index=pd.to_datetime([datetime(2024, 1, 3, tzinfo=timezone.utc)]))
    with pytest.raises(TvStudyError):
        indicator.trail(df)


def test_save_rejects_child_studies_over_the_cap(tmp_path, monkeypatch):
    monkeypatch.setattr("data.tv_stacks.STACKS_PATH", tmp_path / "stacks.json")
    monkeypatch.setattr("data.tv_stacks.get_client", lambda: type("C", (), {"study_limit": 1})())
    raw = _stack()
    raw["studies"].append({
        "id": "STD;EMA", "version": "last", "inputs": {},
        "source": {"type": "study", "study_index": 1, "plot": "plot_0"},
    })
    with pytest.raises(ValueError, match="plan cap is 1"):
        save_stack(raw)


def test_scanner_loads_the_stack_instead_of_local_indicators(tmp_path, monkeypatch):
    monkeypatch.setattr("data.tv_stacks.STACKS_PATH", tmp_path / "stacks.json")
    stack = save_stack(_stack())
    from core.scanner import MarketScanner

    scanner = MarketScanner(symbols=["AAPL"], indicators_only=False, tv_stack_id=stack["id"])
    scanner._discover_patterns()
    assert [pattern.name for pattern in scanner._patterns] == ["sma-of-rsi"]
    assert scanner._indicators_only is True
    assert type(scanner._patterns[0]).__name__ == "TvStackIndicator"
