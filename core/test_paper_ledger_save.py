"""The paper ledger must keep writing when a numpy scalar reaches trade state.

DB-free on purpose: this is the failure that silently stopped every ledger
write of a live run (only the last good snapshot survived a refresh).
"""
from __future__ import annotations

from datetime import datetime, timezone

import numpy as np

from core.backtester import BacktestTrade
from core.paper_trader import PaperAccount


def _trade() -> BacktestTrade:
    return BacktestTrade(
        symbol="TEST", timeframe="1d", pattern="test", action="BUY",
        entry_date=datetime(2026, 1, 1, tzinfo=timezone.utc),
        exit_date=datetime(2026, 1, 1, tzinfo=timezone.utc),
        entry_price=100.0, exit_price=100.0, pnl=0.0, pnl_pct=0.0, qty=10.0,
    )


def test_save_coerces_numpy_scalars_in_trade_evidence(tmp_path):
    """json.dumps used to raise ``Object of type bool is not JSON serializable``.

    numpy.bool_ is not a bool subclass, so one leaked scalar aborted the whole
    write: the run's trades were never persisted and a browser refresh showed
    an empty blotter while the scan kept running.
    """
    acct = PaperAccount(market="us")
    t = _trade()
    t.execution_risk = {"budget_fit": {"passed": np.bool_(True), "atr_ratio": np.float64(2.5)}}
    acct.positions["TEST"] = [t]
    acct._last_price["TEST"] = t.entry_price

    path = tmp_path / "paper_us.json"
    acct.save(path)

    reloaded = PaperAccount.load(path, market="us")
    evidence = reloaded.positions["TEST"][0].execution_risk["budget_fit"]
    assert evidence["passed"] is True
    assert evidence["atr_ratio"] == 2.5


def test_save_still_rejects_a_genuinely_unserializable_value(tmp_path):
    """Coercion is for numpy scalars, not for hiding real schema mistakes."""
    acct = PaperAccount(market="us")
    t = _trade()
    t.execution_risk = {"bad": object()}
    acct.positions["TEST"] = [t]

    try:
        acct.save(tmp_path / "paper_us.json")
    except TypeError as exc:
        assert "not JSON serializable" in str(exc)
    else:  # pragma: no cover - the ledger must not write unknown objects
        raise AssertionError("an unserializable ledger value was silently written")
