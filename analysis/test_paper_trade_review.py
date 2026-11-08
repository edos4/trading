"""Portable exit-evidence review: no ledger, mutable export or database needed."""

from __future__ import annotations

from copy import deepcopy
from datetime import timedelta
import json

import pytest

from analysis.paper_trade_review import (
    boundary_objective_comparison, replay_trade, review_export, signal_evidence, signal_funnel, summarize,
)
from core.backtester import _open_trade
from core.test_patternsv2_exits import candle, signal


def test_signal_funnel_keeps_repeat_replays_separate_and_deduplicates_within_run():
    row = dict(signal_id="same-decision", action="BUY", status="rejected", reason="DII outside band")
    rows = [{**row, "run_id": "first"}, {**row, "run_id": "first"},
            {**row, "run_id": "second", "status": "filled"}]
    assert signal_funnel(rows)["BUY"] == {
        "observed_decisions": 2, "outcomes": {"DII": 1, "filled": 1}}


@pytest.mark.parametrize("short", [False, True])
def test_boundary_objective_comparison_uses_saved_geometry_and_keeps_input(short):
    trade = saved_trade(short)
    sign = -1 if short else 1
    trade["requested_rules"] = {"price": 100 + sign * 25,
                               "take_profit": trade["resolved_rules"]["take_profit"]}
    trade["chart_annotations"] = [{"type": "hline", "label": "level", "price": 100}]
    original = deepcopy(trade)
    result = boundary_objective_comparison([trade])
    assert not result["excluded"]
    assert result["rows"][0]["exit_reason"] == "take_profit"
    assert result["rows"][0]["exit"] == (95 if short else 105)
    assert trade == original
    trade["requested_rules"]["price"] = 100 + sign * 31
    assert boundary_objective_comparison([trade])["excluded"][0]["reason"] == "objective exhausted before entry"


def test_review_flags_unrelated_logs_instead_of_explaining_scan_rejections_with_them():
    book = {
        "scan_stats": {"run_started_at": "2026-11-08T09:05:00+00:00",
                       "run_finished_at": "2026-11-08T09:15:00+00:00",
                       "patterns_found": 0, "signals_rejected": 0,
                       "run_totals": {"patterns_found": 26, "signals_rejected": 25,
                                      "trades_opened": 1, "rejection_by_gate": {"dii": 20, "kronos": 5}}},
        "signal_logs": [{"ts": "2026-11-08T09:26:00+00:00", "run_id": "other",
                         "status": "filled", "symbol": "AAA", "trade_id": None}],
        "closed_trades": [{"trade_id": "shop-trade", "signal_id": None}],
    }
    evidence = signal_evidence(book)
    assert evidence["run_funnel"]["detected"] == 26
    assert evidence["run_funnel"]["rejected"] == 25
    assert evidence["run_funnel"]["rejection_rate_pct"] == pytest.approx(96.153846)
    assert evidence["journal"]["rows_within_scan_window"] == 0
    assert evidence["journal"]["rows_outside_scan_window"] == 1
    assert evidence["journal"]["ledger_trades_without_matching_fill_ids"] == ["shop-trade"]
    assert len(evidence["warnings"]) == 3


@pytest.mark.parametrize("short", [False, True])
@pytest.mark.parametrize("slip", [0, .002])
def test_new_protected_exports_replay_without_mutating_saved_evidence(short, slip):
    from core.paper_trader import PaperAccount
    from data.ohlcv_store import OHLCVStore
    from utils.trade_export import build_paper_account_export

    book = PaperAccount(market="us", txn_cost_pct=.001, slippage_pct=slip)
    book.assume_session_open = True
    store = OHLCVStore()
    assert book.queue_position(signal(short), candle(100, 100, 100, 100), store)[0]
    first = candle(100, 101, 99, 100, 1)
    book.fill_pending("TEST", "1d", first, store)
    assert book.on_bar("TEST", first, "1d") == []
    assert book.on_bar("TEST", candle(100, 110, 90, 100, 2), "1d")
    trade = build_paper_account_export(book)["books"][0]["closed_trades"][0]
    original = deepcopy(trade)
    row = replay_trade(trade)
    assert row["pnl"] == pytest.approx(-.03 * trade["resolved_rules"]["price"] * trade["qty"])
    assert not row['paper_trailing_armed']
    assert row["net_r"] == pytest.approx(trade["pnl"] / row["initial_risk"], abs=.00001)
    assert trade == original
    trade["paper_protection"]["giveback_pct"] = .08
    with pytest.raises(ValueError, match="prior orders"):
        replay_trade(trade)


@pytest.mark.parametrize("short", [False, True])
def test_bounded_protection_comparison_keeps_inputs_and_saved_external_exit(short):
    trade = saved_trade(short)
    original = deepcopy(trade)
    row = replay_trade(trade, paper_loss_protection=True)
    assert row["exit_reason"] == "trailing_stop"
    assert row["pnl"] >= trade["pnl"] - .000001  # A nearer pattern trail can still win.
    assert not row["coverage_exhausted"]
    assert trade == original
    trade = saved_trade(short, opened=True)
    # Preserve an observed next-open external exit without re-running Kronos.
    last = deepcopy(trade["daily_marks"][0])
    last["sim_bar"] = candle(100, 101, 99, 100, 1).timestamp.isoformat()
    last["exit_check"]["orders_before_bar"]["pending_exit_reason"] = "kronos_exit"
    last["exit_check"]["ohlc"] = dict(open=94 if short else 106, high=110, low=90, close=100)
    trade["daily_marks"].append(last)
    row = replay_trade(trade, paper_loss_protection=True)
    assert row["exit_reason"] == "kronos_exit"
    assert row["exit"] == (94 if short else 106)


def saved_trade(short=False, *, opened=False, slip=0):
    s = signal(short)
    s.stop_loss = 103 if short else 97
    s.stop_loss_pct_cap = .03
    s.trailing_stop_pct = .03
    s.trailing_activation_pct = .0525
    first = candle(100/(1-slip if short else 1+slip), 101 if short else 107,
                   93 if short else 99, 94 if short else 106)
    entry = candle(100, 100, 100, 100)
    p = _open_trade(s, entry, 0, execution_model="causal-v1")
    orders = {
        "hard_stop": s.stop_loss, "hard_stop_on_close": False,
        "trailing_stop": None, "trailing_stop_on_close": False,
        "trailing_activated": False, "target": s.take_profit,
        "target_on_close": False, "pending_exit_reason": None,
    }
    def mark(bar, before, fill=None):
        return {
            "date": bar.timestamp.date().isoformat(), "sim_bar": bar.timestamp.isoformat(),
            "close": fill if fill else bar.close, "mtm": 590,
            "exit_check": {
                "orders_before_bar": before,
                "ohlc": {k: getattr(bar, k) for k in ("open", "high", "low", "close")},
                "raw_fill": fill, "reason": "trailing_stop" if fill else None,
            },
        }
    trade = {
        "trade_id": p.trade_id, "symbol": "TEST", "timeframe": "1d", "pattern": s.pattern,
        "action": s.action, "provenance": "patternsv2", "execution_model": "causal-v1",
        "resolved_rules": p.resolved_rules, "entry_fee_paid": 10,
        "entry": 100, "qty": 100, "sim_opened": entry.timestamp.isoformat(),
        "decision_date": (entry.timestamp-timedelta(days=1)).isoformat(),
        "daily_marks": [mark(first, orders)], "mtm": 590,
    }
    if not opened:
        last = candle(95, 98, 94, 97, 1) if short else candle(105, 106, 102, 103, 1)
        trail = 96.82 if short else 102.82
        orders = {**orders, "trailing_stop": trail, "trailing_activated": True}
        trade["daily_marks"].append(mark(last, orders, trail))
        fill = trail * (1+slip if short else 1-slip)
        fee = (100+fill)*.001*100
        net = ((100-fill if short else fill-100)*100)-fee
        trade.update(exit_reason="trailing_stop", sim_closed=last.timestamp.isoformat(),
                     exit=fill, pnl=net, hold_bars=2)
        trade["daily_marks"][-1]["exit_check"]["fees"] = fee
        trade["daily_marks"][-1]["mtm"] = net
    return trade


@pytest.mark.parametrize("short", [False, True])
@pytest.mark.parametrize("slip", [0, .002])
def test_baseline_reproduces_directional_fills_costs_and_close_activated_trail(short, slip):
    trade = saved_trade(short, slip=slip)
    original = deepcopy(trade)
    row = replay_trade(trade)
    assert row["pnl"] == pytest.approx(trade["pnl"])
    assert row["slippage_pct_per_side"] == pytest.approx(slip)
    assert row["txn_cost_pct_per_side"] == pytest.approx(.001)
    assert row["trailing_armed"] and row["bars"] == 2
    assert row["net_r"] == pytest.approx(trade["pnl"]/300)
    assert trade == original


@pytest.mark.parametrize("short,expected", [(False, 579.4), (True, 580.6)])
def test_2r_cap_executes_on_first_session_without_using_later_bar(short, expected):
    trade = saved_trade(short)
    capped = replay_trade(trade, target_max_r=2)
    assert capped["pnl"] == pytest.approx(expected)
    assert capped["exit_reason"] == "take_profit" and capped["same_session_exit"]
    assert capped["bars"] == 1 and not capped["trailing_armed"]
    assert trade["resolved_rules"]["take_profit"] == (70 if short else 130)


@pytest.mark.parametrize("short", [False, True])
def test_open_mark_is_separate_from_hypothetical_liquidation(short):
    row = replay_trade(saved_trade(short, opened=True))
    assert row["exit_reason"] == "open" and row["marked_open_pnl"] == 590
    assert row["open_liquidation_pnl"] == pytest.approx(580.6 if short else 579.4)


@pytest.mark.parametrize("field", ["pnl", "exit", "hold_bars", "sim_closed"])
def test_changed_saved_close_is_rejected(field):
    trade = saved_trade()
    trade[field] = "changed" if field == "sim_closed" else trade[field] + 1
    with pytest.raises(ValueError, match="does not reproduce"):
        replay_trade(trade)


def test_runtime_drift_missing_bars_and_changed_orders_are_rejected():
    trade = saved_trade()
    trade["resolved_rules"]["engine_sha256"] = "changed"
    with pytest.raises(ValueError, match="runtime differs"):
        replay_trade(trade)
    trade = saved_trade()
    trade["daily_marks"][0]["exit_check"] = None
    with pytest.raises(ValueError, match="OHLC exit evidence"):
        replay_trade(trade)
    trade = saved_trade()
    trade["daily_marks"][1]["exit_check"]["orders_before_bar"]["trailing_stop"] += 1
    with pytest.raises(ValueError, match="prior orders"):
        replay_trade(trade)


def export_book(closed, opened, *, market="us", currency="USD"):
    rows = [replay_trade(t) for t in closed+opened]
    return {
        "market": market, "currency": currency, "closed_trades": closed,
        "open_positions": opened, "signal_logs": [],
        "metrics": {
            "realized_pnl_dollars": sum(t["pnl"] for t in closed),
            "unrealized_pnl_dollars": sum(t["mtm"] for t in opened),
            "fees": sum(r["fees_paid"] for r in rows),
        },
    }


def test_review_keeps_currencies_open_trades_and_counterfactuals_separate(tmp_path):
    path = tmp_path/"export.json"
    data = {"exported_at": "2026-01-07T00:00:00+00:00", "books": [
        export_book([saved_trade()], [saved_trade(True, opened=True)]),
        export_book([saved_trade()], [], market="ph", currency="PHP"),
    ]}
    path.write_text(json.dumps(data))
    original = path.read_bytes()
    result = review_export(path)
    assert [b["currency"] for b in result["books"]] == ["USD", "PHP"]
    us = result["books"][0]
    assert us["baseline_verified"] and us["summary"]["count"] == 1
    assert us["open_marked_pnl"] == 590
    assert us["comparisons"]["long_only_closed"]["count"] == 1
    # Cap can also realize a previously open position; its count is explicit.
    assert us["comparisons"]["target_cap_2r_closed"]["count"] == 2
    assert us["extra_friction_per_side_stress"]["0.001"] < us["summary"]["net_pnl"]
    json.dumps(result, allow_nan=False)
    assert path.read_bytes() == original
    data["books"][0]["metrics"]["fees"] += 1
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="total fees"):
        review_export(path)


def test_empty_and_zero_pnl_samples_are_not_reported_as_winners():
    assert summarize([])["win_rate_pct"] is None
    summary = summarize([{"pnl": 0, "net_r": 0, "exit_reason": "time_exit"}])
    assert summary["wins"] == summary["losses"] == 0
    assert summary["profit_factor"] is None


def test_inconsistent_ranges_require_explicit_opt_in_and_are_never_repaired(tmp_path):
    trade = saved_trade()
    trade['daily_marks'][0]['exit_check']['ohlc']['open'] = 98
    with pytest.raises(ValueError, match='Inconsistent saved OHLC'):
        replay_trade(trade)
    # A different malformed bar leaves the saved fills unchanged. The opt-in
    # path must still verify those fills and fees against the raw evidence.
    trade = saved_trade()
    trade['daily_marks'][1]['exit_check']['ohlc']['high'] = 104
    original = deepcopy(trade)
    row = replay_trade(trade, allow_inconsistent_ohlc=True)
    assert row['pnl'] == pytest.approx(trade['pnl'])
    assert len(row['ohlc_anomalies']) == 1
    assert trade == original
    data = {'exported_at': '2026-01-07T00:00:00+00:00', 'books': [{
        'market': 'us', 'currency': 'USD', 'closed_trades': [trade], 'open_positions': [],
        'metrics': {'realized_pnl_dollars': trade['pnl'], 'unrealized_pnl_dollars': 0,
                    'fees': row['fees_paid']},
    }]}
    path = tmp_path/'export.json'
    path.write_text(json.dumps(data))
    result = review_export(path, allow_inconsistent_ohlc=True)['books'][0]
    assert result['baseline_reproduced'] and not result['baseline_verified']
    assert result['ohlc_anomalies'][0]['ohlc']['high'] == 104


def test_looser_trail_is_censored_instead_of_inventing_later_prices():
    trade = saved_trade(True)
    # The old reference trail at 96.82 exits on this bar. A fixed $3
    # giveback trails the prior 94 close at 97 and is not touched.
    trade['daily_marks'][1]['exit_check']['ohlc']['high'] = 96.9
    trade['daily_marks'][1]['exit_check']['ohlc']['close'] = 96
    row = replay_trade(trade, trailing_stop_basis='entry')
    assert row['coverage_exhausted'] and row['exit_reason'] == 'open'


def test_signal_funnel_deduplicates_queue_outcomes_and_identifies_disabled_shorts():
    buy = dict(symbol="TEST", timeframe="1d", pattern="double_bottom", action="BUY",
               sim_bar="2026-01-05T00:00:00+00:00", status="queued", reason="Queued")
    sell = {**buy, "action": "SELL", "pattern": "double_top", "status": "rejected",
            "reason": "Short entries are disabled (SHORT_ENTRIES_ENABLED=false)."}
    result = signal_funnel([sell, {**buy, "status": "filled"}, buy])
    assert result["BUY"] == {"observed_decisions": 1, "outcomes": {"filled": 1}}
    assert result["SELL"] == {"observed_decisions": 1, "outcomes": {"shorts_disabled": 1}}
    # Buy-to-cover exit events retain SELL action but are not new entries.
    assert signal_funnel([sell, {**sell, "status": "exited", "sim_bar": "later"}]) == signal_funnel([sell])
    result = signal_funnel([buy, {**buy, "status": "rejected", "reason": "Opening gap crossed the target"}])
    assert result["BUY"]["outcomes"] == {"opening_gap": 1}


def test_funnel_separates_model_opposition_abstention_and_data_errors():
    reasons = ['Kronos oppose: 2/3 forecasts against the BUY',
               'Kronos no_opinion: no majority of 2 in either direction',
               'Kronos no_opinion: Kronos forecast date differs from the decision bar',
               'Kronos no_opinion: Kronos worker error for TEST: Invalid OHLC context']
    rows = [dict(symbol=str(i), action='BUY', status='rejected', reason=reason)
            for i, reason in enumerate(reasons)]
    assert signal_funnel(rows)['BUY']['outcomes'] == dict(
        Kronos_oppose=1, Kronos_no_opinion=1, Kronos_context_error=1, Kronos_worker_error=1)


def test_quote_exit_cannot_be_certified_by_completed_bar_replay():
    trade = saved_trade()
    trade['daily_marks'][-1]['exit_check']['observation_kind'] = 'live_quote'
    with pytest.raises(ValueError, match='Live quote exits require tick evidence'):
        replay_trade(trade)


@pytest.mark.parametrize('short', [False, True])
def test_review_counts_the_paper_trail_even_when_the_pattern_trail_never_arms(short):
    from core.paper_trader import PaperAccount
    from data.ohlcv_store import OHLCVStore
    from utils.trade_export import build_paper_account_export

    book = PaperAccount(market='us', txn_cost_pct=.001)
    book.assume_session_open = True
    store = OHLCVStore()
    assert book.queue_position(signal(short), candle(100, 100, 100, 100), store)[0]
    first = candle(100, 101, 99, 99 if short else 101, 1)
    book.fill_pending('TEST', '1d', first, store)
    assert book.on_bar('TEST', first, '1d') == []
    assert book.on_bar('TEST', candle(100, 110, 90, 100, 2), '1d')
    trade = build_paper_account_export(book)['books'][0]['closed_trades'][0]
    row = replay_trade(trade)
    assert row['trailing_armed'] and row['paper_trailing_armed']
    assert not row['pattern_trailing_armed']
