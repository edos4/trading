from utils.trade_export import build_paper_trade_export


def _envelope():
    return {
        "clocks": {},
        "books": {
            "us": {
                "market": "us",
                "label": "US",
                "currency": "USD",
                "currency_symbol": "$",
                "long_only": False,
                "session": "RTH",
                "running": True,
                "cash": 90_000.1234,
                "equity": 102_410.0,
                "open_count": 1,
                "closed_count": 1,
                "exposure": {"long_pct": 10, "short_pct": 0, "net_pct": 10},
                "metrics": {"avg_r": 0.5, "equity_png_b64": "SHOULD_DROP"},
                "summary": "US summary",
                "scan_stats": {
                    "patterns_found": 3,
                    "trades_opened": 1,
                    "signals_rejected": 2,
                    "rejection_by_gate": {"kronos": 2},
                    "noise": "ignore",
                },
                "equity_png_b64": "nope",
                "positions": [
                    {
                        "market": "us",
                        "symbol": "SM",
                        "status": "open",
                        "action": "BUY",
                        "pattern": "pattern_003_double_bottom",
                        "qty": 10,
                        "entry": 10.0,
                        "current": 11.0,
                        "unrl_pct": 10.0,
                        "mtm": 10.0,
                        "r": 0.4,
                        "days": 2.0,
                        "bars": 2,
                        "stop": 9.0,
                        "target": 13.0,
                        "opened": "2026-01-01T00:00:00+00:00",
                        "timeframe": "1d",
                        "daily_marks": [
                            {
                                "date": "2026-01-01",
                                "sim_bar": "2026-01-01T00:00:00+00:00",
                                "close": 10.0,
                                "unrl_pct": 0.0,
                                "mtm": 0.0,
                                "r": 0.0,
                                "value": 100.0,
                                "status": "OPEN",
                                "bars": 0,
                                "stop": 9.0,
                                "target": 13.0,
                            },
                            {
                                "date": "2026-01-02",
                                "sim_bar": "2026-01-02T00:00:00+00:00",
                                "close": 11.0,
                                "unrl_pct": 10.0,
                                "mtm": 10.0,
                                "r": 0.4,
                                "value": 110.0,
                                "status": "OPEN",
                                "bars": 1,
                                "stop": 9.0,
                                "target": 13.0,
                            },
                        ],
                    }
                ],
                "closed": [
                    {
                        "market": "us",
                        "symbol": "AAPL",
                        "action": "BUY",
                        "pattern": "pattern_004_rounding_bottom",
                        "qty": 5,
                        "entry": 100.0,
                        "exit": 110.0,
                        "pnl": 50.0,
                        "pnl_pct": 10.0,
                        "r": 1.2,
                        "reason": "take_profit",
                        "stop": 94.0,
                        "target": 110.0,
                        "closed": "2026-01-10T00:00:00+00:00",
                        "timeframe": "1d",
                        "daily_marks": [
                            {
                                "date": "2026-01-05",
                                "close": 105.0,
                                "unrl_pct": 5.0,
                                "mtm": 25.0,
                                "r": 0.83,
                                "value": 525.0,
                                "status": "OPEN",
                                "bars": 2,
                                "stop": 94.0,
                                "target": 110.0,
                            }
                        ],
                    }
                ],
            },
            "ph": {
                "market": "ph",
                "label": "Philippines (PSE)",
                "currency": "PHP",
                "currency_symbol": "₱",
                "long_only": True,
                "session": "closed",
                "running": False,
                "cash": 1_000_000,
                "equity": 1_000_000,
                "open_count": 1,
                "closed_count": 0,
                "metrics": {},
                "summary": "No closed trades yet.",
                "scan_stats": None,
                "positions": [
                    {
                        "market": "ph",
                        "symbol": "SM",
                        "status": "open",
                        "action": "BUY",
                        "pattern": "pattern_003_double_bottom",
                        "qty": 100,
                        "entry": 900.0,
                        "current": 910.0,
                        "unrl_pct": 1.1,
                        "mtm": 1000.0,
                        "opened": "2026-01-02T00:00:00+00:00",
                        "timeframe": "1d",
                    }
                ],
                "closed": [],
            },
        },
    }


def test_export_includes_both_books_and_review_prompt():
    payload = build_paper_trade_export(_envelope())
    assert payload["purpose"] == "paper_trade_evaluation"
    assert payload["filter"] == "all"
    assert "Never combine USD" in payload["review_prompt"]
    assert [b["market"] for b in payload["books"]] == ["us", "ph"]
    us, ph = payload["books"]
    assert us["currency_symbol"] == "$"
    assert ph["currency_symbol"] == "₱"
    assert us["open_positions"][0]["symbol"] == "SM"
    assert ph["open_positions"][0]["symbol"] == "SM"
    assert us["open_positions"][0]["entry"] == 10.0
    assert ph["open_positions"][0]["entry"] == 900.0
    assert us["closed_trades"][0]["exit_reason"] == "take_profit"
    assert us["closed_trades"][0]["stop"] == 94.0
    assert len(us["open_positions"][0]["daily_marks"]) == 2
    assert us["open_positions"][0]["daily_marks"][-1]["close"] == 11.0
    assert us["closed_trades"][0]["daily_marks"][0]["date"] == "2026-01-05"
    assert "equity_png_b64" not in us
    assert "equity_png_b64" not in us["metrics"]
    assert us["scan_stats"]["rejection_by_gate"] == {"kronos": 2}
    assert "noise" not in us["scan_stats"]


def test_export_filter_ph_excludes_us():
    payload = build_paper_trade_export(_envelope(), market="ph")
    assert payload["filter"] == "ph"
    assert [b["market"] for b in payload["books"]] == ["ph"]
    assert payload["books"][0]["open_positions"][0]["symbol"] == "SM"


def test_export_rejects_bad_market():
    try:
        build_paper_trade_export(_envelope(), market="eu")
    except ValueError as exc:
        assert "market" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_export_from_paper_account():
    from datetime import datetime, timezone

    from core.backtester import BacktestTrade
    from core.paper_trader import PaperAccount
    from utils.trade_export import build_paper_account_export

    acct = PaperAccount(initial_capital=100_000.0, market="us", slippage_pct=0.0)
    now = datetime(2026, 1, 10, tzinfo=timezone.utc)
    closed = BacktestTrade(
        symbol="AAPL",
        timeframe="1d",
        pattern="pattern_003_double_bottom",
        action="BUY",
        entry_date=datetime(2026, 1, 2, tzinfo=timezone.utc),
        exit_date=now,
        entry_price=100.0,
        exit_price=110.0,
        pnl=10.0,
        pnl_pct=10.0,
        qty=5,
        stop_loss=94.0,
        take_profit=110.0,
        exit_reason="take_profit",
    )
    acct.closed.append(closed)
    open_t = BacktestTrade(
        symbol="MSFT",
        timeframe="1d",
        pattern="pattern_007_descending_channel",
        action="BUY",
        entry_date=datetime(2026, 1, 5, tzinfo=timezone.utc),
        exit_date=now,
        entry_price=50.0,
        exit_price=50.0,
        pnl=0.0,
        pnl_pct=0.0,
        qty=10,
        stop_loss=45.0,
        take_profit=60.0,
    )
    acct.positions["MSFT"] = open_t
    acct._last_price["MSFT"] = 55.0

    payload = build_paper_account_export(
        acct, scan_stats={"trades_opened": 2, "rejection_by_gate": {}},
    )
    assert payload["purpose"] == "paper_trade_evaluation"
    assert payload["filter"] == "us"
    assert len(payload["books"]) == 1
    book = payload["books"][0]
    assert book["market"] == "us"
    assert book["closed_trades"][0]["symbol"] == "AAPL"
    assert book["closed_trades"][0]["exit_reason"] == "take_profit"
    assert book["open_positions"][0]["symbol"] == "MSFT"
    assert book["open_positions"][0]["current"] == 55.0
    assert book["scan_stats"]["trades_opened"] == 2
