"""Patternsv2 pre-placement rule filter tests."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import numpy as np
import pytest

from core.rule_contracts import RulePayload
from core.scanner import MarketScanner
from data.tv_client import OHLCVCandle


def _candles(last_close: float, n: int = 120) -> list[OHLCVCandle]:
    out = []
    for index in range(n):
        close = last_close if index == n - 1 else 100.0
        stamp = datetime(2025, 1, 1, tzinfo=timezone.utc) + timedelta(days=index)
        out.append(OHLCVCandle(open=close, high=close + 1, low=close - 1,
                               close=close, volume=1_000_000.0, timestamp=stamp))
    return out


def _rule() -> RulePayload:
    return RulePayload.model_validate({
        "name": "above 110",
        "root": {"kind": "group", "op": "AND", "children": [{
            "kind": "condition",
            "left": {"source": "indicator", "id": "close"},
            "operator": "gt",
            "right": {"source": "value", "value": 110},
        }]},
    })


def _scanner(**kwargs) -> MarketScanner:
    return MarketScanner(
        symbols=["TEST"], market="us", volume_gate=False, **kwargs)


def _snapshot() -> SimpleNamespace:
    return SimpleNamespace(symbol="TEST", timeframe="1d")


def test_before_skips_patternsv2_detection_when_rule_is_false() -> None:
    scanner = _scanner(patterns_v2=True)
    scanner._rule_filter = _rule()
    scanner._rule_filter_mode = "filter"
    scanner._rule_filter_position = "before"
    scanner._store.replace_all("TEST", "1d", _candles(100.0))
    calls = []
    scanner._analyze_patterns_v2 = lambda snapshot: (calls.append(snapshot), (1, ["hit"]))[1]

    assert scanner._analyze_local(_snapshot()) == (0, [])
    assert calls == []
    assert scanner.stats["rule_filter_prefiltered"] == 1

    scanner._store.replace_all("TEST", "1d", _candles(120.0))
    assert scanner._analyze_local(_snapshot()) == (1, ["hit"])
    assert len(calls) == 1


def test_before_requires_filter_mode() -> None:
    with pytest.raises(ValueError, match="filter mode"):
        _scanner(patterns_v2=True, rule_filter_id="x",
                 rule_filter_mode="shadow", rule_filter_position="before")


def test_before_degrades_to_after_without_patternsv2() -> None:
    scanner = _scanner(patterns_v2=False, rule_filter_position="before")
    assert scanner._rule_filter_position == "after"


def test_after_still_gates_signals() -> None:
    scanner = _scanner(patterns_v2=True)
    scanner._rule_filter = _rule()
    scanner._rule_filter_mode = "filter"
    scanner._rule_filter_position = "after"
    scanner._store.replace_all("TEST", "1d", _candles(100.0))
    # The post-signal gate is exercised by the evidence call; the scanner's
    # _finish_signal uses the same method.
    evidence = scanner._rule_filter_evidence("TEST", "1d")
    assert evidence["matched"] is False
    assert evidence["warm"] is True


def test_target_pattern_limits_before_placement() -> None:
    scanner = _scanner(patterns_v2=True)
    scanner._rule_filter = _rule()
    scanner._rule_filter_mode = "filter"
    scanner._rule_filter_position = "before"
    scanner._rule_filter_pattern = "double_bottom"
    scanner._store.replace_all("TEST", "1d", _candles(100.0))
    hits = [SimpleNamespace(pattern="double_bottom"),
            SimpleNamespace(pattern="symmetrical_triangle")]
    scanner._analyze_patterns_v2 = lambda snapshot: (2, list(hits))

    _, returned = scanner._analyze_local(_snapshot())
    assert [item.pattern for item in returned] == ["symmetrical_triangle"]
    assert scanner.stats["rule_filter_prefiltered"] == 1

    scanner._store.replace_all("TEST", "1d", _candles(120.0))
    _, returned = scanner._analyze_local(_snapshot())
    assert [item.pattern for item in returned] == [
        "double_bottom", "symmetrical_triangle"]


def test_pattern_target_filter() -> None:
    scanner = _scanner(patterns_v2=True)
    scanner._rule_filter_pattern = "double_bottom"
    assert scanner._rule_filter_matches_pattern("double_bottom") is True
    assert scanner._rule_filter_matches_pattern("flag") is False
    scanner._rule_filter_pattern = "all"
    assert scanner._rule_filter_matches_pattern("flag") is True
