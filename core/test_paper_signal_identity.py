"""The first accepted decision must link to its actual fill, including reloads."""

from __future__ import annotations

import asyncio
from copy import deepcopy

import pytest

pytestmark = pytest.mark.usefixtures("confirmed_buy_dii")

from analysis.paper_trade_review import signal_evidence
from core.paper_trader import PaperAccount
from core.scanner import MarketScanner
from core.test_patternsv2_exits import candle, signal
from utils.trade_export import build_paper_account_export


@pytest.mark.parametrize("short", [False, True])
@pytest.mark.parametrize("old_queue", [False, True])
def test_first_accepted_signal_keeps_identity_and_fill_economics_after_restart(short, old_queue, tmp_path):
    book = PaperAccount(market="us", txn_cost_pct=.001, slippage_pct=.002)
    book.assume_session_open = True
    scanner = MarketScanner(symbols=["TEST"], market="us", paper_account=book,
                            kronos_gate_mode="off", kronos_exit_mode="off")
    decision = signal(short)
    assert decision.signal_id is None
    # No shadow/rejected journal event has assigned an ID before queueing.
    asyncio.run(scanner._queue_entry(decision, candle(100, 101, 99, 100)))
    queued = scanner.signal_log_snapshot()[-1]
    assert queued["status"] == "queued"
    assert queued["signal_id"] == decision.signal_id == book.pending_entries[0].signal_id
    assert decision.signal_id
    requested = deepcopy(book.pending_entries[0].requested_rules)
    if old_queue:
        book.pending_entries[0].signal_id = None
    path = tmp_path / "book.json"
    book.save(path)
    scanner._paper = book = PaperAccount.load(path, market="us")
    book.assume_session_open = True
    opening = 98 if short else 102
    scanner._fill_pending_observation("TEST", "1d", candle(opening, opening, opening, opening, 1))
    position = book.open_for("TEST")[0]
    filled = scanner.signal_log_snapshot()[-1]
    assert filled["status"] == "filled"
    assert filled["signal_id"] == position.signal_id == queued["signal_id"]
    assert filled["trade_id"] == position.trade_id
    assert filled["execution_model"] == position.execution_model
    assert filled["entry_fee_paid"] == position.entry_fee_paid
    assert filled["decision_date"] == position.decision_date.isoformat()
    assert filled["resolved_rules"] == position.resolved_rules
    assert filled["price"] == pytest.approx(position.entry_price, abs=.00006)
    assert filled["requested_rules"] == position.requested_rules == requested
    # The original decision row stays a pre-fill snapshot.
    assert queued["resolved_rules"] is None and queued["trade_id"] is None
    exported = build_paper_account_export(book)["books"][0]
    assert not signal_evidence(exported)["journal"]["ledger_trades_without_matching_fill_ids"]
