"""Laya paper shadow/filter checks (docs/laya.md P5/P6 + section 15)."""
from __future__ import annotations

import asyncio
import tempfile
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from core import signal_log_store as sls
from core.paper_trader import PaperAccount
from core.scanner import MarketScanner
from data.tv_client import MarketSnapshot, OHLCVCandle
from patterns.base_pattern import BasePattern, TradeSignal


def _candle(close: float, ts: datetime) -> OHLCVCandle:
    return OHLCVCandle(open=close, high=close, low=close, close=close,
                       volume=1.0, timestamp=ts)


class _BuyOnce(BasePattern):
    name = "test_laya_pattern"
    fired = False

    @property
    def timeframes(self):
        return ["1d"]

    def analyze(self, snapshot, store):
        if self.fired:
            return None
        self.fired = True
        return TradeSignal(
            symbol=snapshot.symbol, action="BUY", pattern=self.name,
            timeframe="1d", confidence=0.9, price=snapshot.candle.close, qty=10,
            stop_loss=snapshot.candle.close * 0.90,
            take_profit=snapshot.candle.close * 1.20,
        )


class _Feed:
    def __init__(self, closes=(100.0, 110.0)):
        self.bar = 0
        tz = ZoneInfo("America/New_York")
        self.candles = [
            _candle(c, datetime(2024, 1, 2 + i, 16, 5, tzinfo=tz))
            for i, c in enumerate(closes)
        ]

    @asynccontextmanager
    async def mcp_session(self):
        yield None

    async def fetch_snapshot(self, symbol, timeframe, store=None, mcp_session=None):
        candle = self.candles[self.bar]
        snapshot = MarketSnapshot(
            symbol=symbol, timeframe=timeframe, timestamp=candle.timestamp,
            candle=candle, indicators={}, summary={}, oscillators={},
            moving_avgs={},
        )
        if store is not None:
            if self.bar == 0:
                tz = candle.timestamp.tzinfo
                # Flat warmup keeps the DII inside its 40–60% band, with 100+ candles.
                warmup = [
                    _candle(90.0, datetime(2023, 11, 20, 16, 5, tzinfo=tz) + timedelta(days=i))
                    for i in range(120)
                ]
                store.replace_all(symbol, timeframe, warmup + [candle])
            else:
                store.push(snapshot)
        return snapshot


def _scanner(feed, account, **kwargs):
    scanner = MarketScanner(
        symbols=["AAA"], paper_account=account, data_feed=feed,
        scan_interval_seconds=3600, market="us", volume_gate=False, **kwargs)
    account.assume_session_open = True
    scanner._patterns = [_BuyOnce()]
    scanner._pattern_files = {_BuyOnce.name: "test"}
    # No editor database locally: keep the pinned test pattern instead of
    # resolving versions from PostgreSQL, and skip worker bookkeeping.
    scanner._discover_patterns = lambda: None
    scanner._note_worker = lambda **_kw: None
    return scanner


def _run(scanner: MarketScanner) -> None:
    asyncio.run(scanner._scan_all())
    scanner._tv.bar = 1
    asyncio.run(scanner._scan_all())


class TestLayaPaper:
    @pytest.fixture(autouse=True)
    def _isolate_logs(self):
        prev = sls._log_dir
        sls._log_dir = Path(tempfile.mkdtemp())
        yield
        sls._log_dir = prev

    def test_shadow_failing_worker_preserves_fills(self) -> None:
        """Shadow on/off produces identical entries with a failing worker."""
        feed = _Feed()
        account = PaperAccount(market="us")
        scanner = _scanner(
            feed, account, laya_mode="shadow",
            laya_assess=lambda _snap: (_ for _ in ()).throw(RuntimeError("worker down")))
        _run(scanner)
        assert len(account.positions_snapshot()) == 1
        assert scanner._laya_shadow

    def test_filter_veto_blocks_entry(self) -> None:
        feed = _Feed()
        account = PaperAccount(market="us")
        scanner = _scanner(
            feed, account, laya_mode="filter", laya_policy_id="pol-1",
            laya_assess=lambda _snap: {"outcome": "veto", "reasons": ["test veto"]})
        _run(scanner)
        assert len(account.positions_snapshot()) == 0

    def test_filter_abstain_blocks_entry(self) -> None:
        feed = _Feed()
        account = PaperAccount(market="us")
        scanner = _scanner(
            feed, account, laya_mode="filter", laya_policy_id="pol-1",
            laya_assess=lambda _snap: {"outcome": "abstain", "reasons": ["no model"]})
        _run(scanner)
        assert len(account.positions_snapshot()) == 0

    def test_filter_allow_opens(self) -> None:
        feed = _Feed()
        account = PaperAccount(market="us")
        scanner = _scanner(
            feed, account, laya_mode="filter", laya_policy_id="pol-1",
            laya_assess=lambda _snap: {"outcome": "allow", "reasons": ["ok"],
                                       "assessment_id": "asm-1"})
        _run(scanner)
        assert len(account.positions_snapshot()) == 1
        _sym, trade = account.positions_snapshot()[0]
        # The gate verdict rides the fill for Paper/export/Replay attribution.
        assert trade.laya_assessment["assessment_id"] == "asm-1"
        assert trade.laya_assessment["mode"] == "filter"
        assert trade.laya_assessment["outcome"] == "allow"

    def test_off_baseline_unchanged(self) -> None:
        feed = _Feed()
        account = PaperAccount(market="us")
        scanner = _scanner(feed, account)
        _run(scanner)
        assert len(account.positions_snapshot()) == 1


class TestLayaPaperAssessor:
    @pytest.fixture(autouse=True)
    def _isolate_laya(self, tmp_path, monkeypatch):
        import config
        import core.laya_service as svc
        import core.laya_store as st

        monkeypatch.setattr(config.settings, "laya_enabled", True)
        monkeypatch.setattr(
            config.settings, "laya_store_path", str(tmp_path / "laya.sqlite3"))
        monkeypatch.setenv("LAYA_FAKE", "1")
        st._store = None
        svc.reset_service()
        yield
        st._store = None
        svc.reset_service()

    def _service(self):
        from core.laya_service import get_service

        return get_service()

    def _seed(self, policy, store=None) -> None:
        from core.laya_store import get_store

        target = store or get_store()
        target.seed_policy(
            policy.policy_id, policy.version, policy.model_dump(mode="json"),
            "2026-01-01T00:00:00+00:00")

    def _snapshot(self) -> dict:
        return {
            "symbol": "AAA", "timeframe": "1d",
            "pattern": "pattern_003_double_bottom", "action": "BUY",
            "price": 112.0, "stop": 100.0, "target": 130.0,
            "confidence": 0.8, "bar_ts": "2026-01-20T21:00:00+00:00",
            "closes": [100.0 + i * 0.5 for i in range(30)],
            "volumes": [1_000_000.0] * 30,
        }

    def _permissive(self):
        from core.laya_contracts import LayaPolicy

        return LayaPolicy(
            policy_id="test-permissive", task_id="candidate-review-v1",
            model_id="pinned", markets=("us",), min_answer_confidence=0.0,
            require_alignment=("supports", "mixed", "conflicts", "insufficient"),
            min_evidence_score=None)

    def test_filter_allow_with_permissive_policy(self) -> None:
        from core.laya_paper import make_assessor

        self._seed(self._permissive())
        assessor = make_assessor(market="us", policy_id="test-permissive",
                                 mode="filter", timeout_s=15,
                                 service=self._service())
        verdict = assessor(self._snapshot())
        assert verdict["outcome"] == "allow"
        assert (verdict["assessment_id"] or "").startswith("asm-paper-")
        assert verdict["reasons"]

    def test_filter_veto_with_strict_policy(self) -> None:
        from core.laya_contracts import LayaPolicy
        from core.laya_paper import make_assessor

        # The fake worker answers "insufficient" strength: a policy demanding
        # "strong" evidence vetoes.
        self._seed(LayaPolicy(
            policy_id="test-strict", task_id="candidate-review-v1",
            model_id="pinned", markets=("us",), min_answer_confidence=0.0,
            require_alignment=("supports",), min_evidence_score="strong"))
        assessor = make_assessor(market="us", policy_id="test-strict",
                                 mode="filter", timeout_s=15,
                                 service=self._service())
        verdict = assessor(self._snapshot())
        assert verdict["outcome"] == "veto"

    def test_rescan_of_same_bar_dedupes(self) -> None:
        from core.laya_paper import make_assessor

        self._seed(self._permissive())
        assessor = make_assessor(market="us", policy_id="test-permissive",
                                 mode="filter", timeout_s=15,
                                 service=self._service())
        first = assessor(self._snapshot())
        second = assessor(self._snapshot())
        assert first["outcome"] == "allow"
        assert second["outcome"] == "allow"
        # Same bar → same stored assessment, no duplicate inference.
        assert second["assessment_id"] == first["assessment_id"]

    def test_filter_abstains_when_worker_fails(self) -> None:
        from core.laya_paper import make_assessor
        from core.laya_service import LayaService
        from core.laya_store import LayaStore

        import tempfile
        from pathlib import Path

        def _boom(_payload):
            raise RuntimeError("worker down")

        service = LayaService(store=LayaStore(Path(tempfile.mkdtemp()) / "x.sqlite3"),
                              max_pending=4, timeout_s=5.0, worker_factory=_boom)
        self._seed(self._permissive(), store=service.store)
        assessor = make_assessor(market="us", policy_id="test-permissive",
                                 mode="filter", timeout_s=10, service=service)
        verdict = assessor(self._snapshot())
        assert verdict["outcome"] == "abstain"

    def test_shadow_returns_allow_immediately_despite_failure(self) -> None:
        import time

        from core.laya_paper import make_assessor
        from core.laya_service import LayaService
        from core.laya_store import LayaStore

        import tempfile
        from pathlib import Path

        def _boom(_payload):
            raise RuntimeError("worker down")

        service = LayaService(store=LayaStore(Path(tempfile.mkdtemp()) / "y.sqlite3"),
                              max_pending=4, timeout_s=30.0, worker_factory=_boom)
        assessor = make_assessor(market="us", policy_id="test-permissive",
                                 mode="shadow", service=service)
        started = time.monotonic()
        verdict = assessor(self._snapshot())
        elapsed = time.monotonic() - started
        assert verdict["outcome"] == "allow"
        assert elapsed < 5.0

    def test_readiness(self) -> None:
        import config
        from core.laya_paper import laya_readiness

        ready, _reason = laya_readiness()
        assert ready is True
        config.settings.laya_enabled = False
        ready, reason = laya_readiness()
        assert ready is False
        assert reason

    def _event_policy(self):
        from core.laya_contracts import LayaPolicy

        return LayaPolicy(policy_id="test-event", task_id="event-review-v1",
                          model_id="pinned", markets=("us",),
                          veto_on_material_risk_yes_prob=0.6,
                          veto_financing_min_confidence=0.35)

    def _filing(self):
        return {"symbol": "AAA", "form": "424B5", "filed": "2026-01-15",
                "accepted_at": "2026-01-15T13:05:00+00:00",
                "accession": "0000000000-26-000001", "revision_id": "x",
                "description": "PROSPECTUS",
                "doc_url": "https://example.invalid/filing.htm",
                "match": "offering-form"}

    def _filing_snapshot(self) -> dict:
        snap = self._snapshot()
        snap["bar_ts"] = "2026-01-20T21:00:00+00:00"
        return snap

    def test_event_allow_with_benign_filing(self, monkeypatch) -> None:
        import data.laya_filings as lf
        from core.laya_paper import make_assessor

        monkeypatch.setattr(lf, "relevant_filings", lambda *a, **k: [self._filing()])
        monkeypatch.setattr(lf, "fetch_filing_text",
                            lambda *a, **k: "Quarterly earnings beat expectations. Raised guidance.")
        svc = self._service()
        self._seed(self._event_policy(), store=svc.store)
        assessor = make_assessor(market="us", policy_id="test-event",
                                 mode="filter", timeout_s=15, service=svc)
        verdict = assessor(self._filing_snapshot())
        # Fake worker answers earnings/positive/no-risk: no danger -> allow.
        assert verdict["outcome"] == "allow"
        assert (verdict["assessment_id"] or "").startswith("asm-fil-")
        assert svc.store.decisions_for(verdict["assessment_id"])

    def test_event_veto_on_financing_filing(self, monkeypatch) -> None:
        import tempfile
        from pathlib import Path

        import data.laya_filings as lf
        from core.laya_paper import make_assessor
        from core.laya_service import LayaService
        from core.laya_store import LayaStore

        monkeypatch.setattr(lf, "relevant_filings", lambda *a, **k: [self._filing()])
        monkeypatch.setattr(lf, "fetch_filing_text",
                            lambda *a, **k: "Underwritten public offering of common shares.")

        def _canned(payload):
            answers = [
                {"qid": "event_type", "kind": "choice", "answer": "financing",
                 "distribution": [0.05, 0.05, 0.8, 0.02, 0.01, 0.01, 0.05, 0.01],
                 "answer_confidence": 0.8, "confidence": 0.5,
                 "yes_probability": None, "score_value": None},
                {"qid": "directional_tone", "kind": "choice", "answer": "neutral",
                 "distribution": [0.05, 0.05, 0.05, 0.7, 0.15],
                 "answer_confidence": 0.7, "confidence": 0.5,
                 "yes_probability": None, "score_value": None},
                {"qid": "material_risk_mentioned", "kind": "noul", "answer": "no",
                 "distribution": [], "answer_confidence": 0.9, "confidence": 0.9,
                 "yes_probability": 0.1, "score_value": None},
            ]
            return {"request_id": payload.get("request_id"), "ok": True,
                    "answers": answers, "raw": {"task_id": "event-review-v1"},
                    "raw_version": "t", "warnings": [], "truncated": False,
                    "options_collapsed": False, "elapsed_ms": 1}

        store = LayaStore(Path(tempfile.mkdtemp()) / "z.sqlite3")
        service = LayaService(store=store, max_pending=4, timeout_s=15.0,
                              worker_factory=_canned)
        self._seed(self._event_policy(), store=store)
        assessor = make_assessor(market="us", policy_id="test-event",
                                 mode="filter", timeout_s=15, service=service)
        verdict = assessor(self._filing_snapshot())
        assert verdict["outcome"] == "veto"
        assert "424B5" in " ".join(verdict["reasons"])

    def test_event_abstains_without_filings(self, monkeypatch) -> None:
        import data.laya_filings as lf
        from core.laya_paper import make_assessor

        monkeypatch.setattr(lf, "relevant_filings", lambda *a, **k: [])
        svc = self._service()
        self._seed(self._event_policy(), store=svc.store)
        assessor = make_assessor(market="us", policy_id="test-event",
                                 mode="filter", timeout_s=15, service=svc)
        verdict = assessor(self._filing_snapshot())
        assert verdict["outcome"] == "abstain"
