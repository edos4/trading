"""Pattern-only skips structure filters; Kronos/volume stay independent."""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from unittest.mock import patch

from core.backtester import Backtester
from core.engine_defaults import ENGINE, structure_filters_enabled
from core.paper_trader import PaperAccount
from core.scanner import MarketScanner
import core.signal_log_store as sls
from patterns.base_pattern import TradeSignal


def _sell(**kw) -> TradeSignal:
    base = dict(
        symbol="ICT", timeframe="1d", pattern="pattern_003_double_bottom",
        action="SELL", price=2.0, confidence=0.20, qty=10,
        stop_loss=2.4, take_profit=1.4,
    )
    base.update(kw)
    return TradeSignal(**base)


def _scanner(*, pattern_only: bool) -> MarketScanner:
    paper = PaperAccount(initial_capital=1_000_000.0, market="ph", slippage_pct=0.0)
    paper.assume_session_open = True
    return MarketScanner(
        symbols=["ICT"],
        paper_account=paper,
        data_feed=object(),
        kronos_gate=False,
        volume_gate=False,
        kronos_rank=False,
        market="ph",
        pattern_only=pattern_only,
    )


def test_structure_filters_helper():
    assert structure_filters_enabled(False) is True
    assert structure_filters_enabled(True) is False
    assert ENGINE.pattern_only is False


def test_backtester_config_carries_pattern_only():
    bt = Backtester(["AAPL"], pattern_only=True, kronos_gate=False, volume_gate=False)
    assert bt._pattern_only is True
    bt_off = Backtester(["AAPL"])
    assert bt_off._pattern_only is False


def test_scanner_pattern_only_skips_structure_gates():
    prev = sls._log_dir
    sls._log_dir = Path(tempfile.mkdtemp())
    try:
        with patch("core.scanner.describe_risk_gate_rejection", return_value=None):
            off = _scanner(pattern_only=False)
            asyncio.run(off._process_signal(_sell()))
            assert off.stats["signals_rejected"] >= 1
            reasons = " ".join(e.get("reason") or "" for e in off.signal_log_snapshot())
            assert "Long-only" in reasons or "Min-confidence" in reasons

            on = _scanner(pattern_only=True)
            asyncio.run(on._process_signal(_sell()))
            assert on.stats["signals_rejected"] == 0
            assert on._paper.pattern_only is True
            statuses = [e["status"] for e in on.signal_log_snapshot()]
            assert "accepted" in statuses
    finally:
        sls._log_dir = prev
