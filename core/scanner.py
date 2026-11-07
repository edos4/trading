"""
core/scanner.py — The market scanner. Replaces both the trading engine
and the webhook server from the previous architecture.

Every SCAN_INTERVAL_SECONDS it:
  1. Fetches fresh data from TradingView MCP for every (symbol, timeframe) pair
  2. Pushes the new candle into OHLCVStore
  3. Runs each registered pattern's analyze() method
  4. If a signal is returned:
        a. Renders a chart (if vision is enabled)
        b. Asks Claude vision to confirm the pattern
        c. Risk gates (ATR trail / R:R) then vision; queue pending
           for next closed bar fill (same deferral as the backtester)

Concurrency: symbols are processed in parallel across N feed sessions
(one session per worker, controlled by scanner_concurrency). Paper stream
uses batch snapshots (papertrade_stream_batch_size) so each worker's
round-trip covers many symbols; after the first fill only the new candle
is sent. pattern.analyze() runs in a spawn process pool
(scanner_analyze_workers) against a copied candle list — the paper ledger
and vision gates stay on this loop. A tqdm progress bar shows
scan progress on the CLI.
"""

from __future__ import annotations
import asyncio
import math
import threading
import time
from copy import deepcopy
from collections import deque
from contextlib import asynccontextmanager, AsyncExitStack
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo
from pathlib import Path

import pandas as pd

from patterns.base_pattern import BasePattern, TradeSignal

from data.tv_client import TVClient, MarketSnapshot
from data.ohlcv_store import OHLCVStore, DEFAULT_WINDOW
from data.stream_client import FetchSkip
from analysis.chart_renderer import ChartRenderer
from analysis.price_volume import volume_confirm_gate
from analysis.vision_checker import VisionChecker, VisionVerdict
from core.paper_trader import PaperAccount
from core.engine_defaults import ENGINE


# ── 2026-09 refactor: the live scan path now mirrors the offline backtester —
# a pattern's own analyze() is the only filter; there is no ML/confidence/
# regime/cooldown/volume/vision gauntlet. These stubs keep the (now dead)
# gate call sites harmless without a wider rewrite of this file.
def structure_filters_enabled(_pattern_only=None) -> bool:
    return False


def regime_filter_required(_pattern=None, _pattern_only=None) -> bool:
    return False


def passes_min_confidence(_signal, _min=None) -> bool:
    return True


def describe_confidence_rejection(_signal, _min=None) -> str:
    return ""


def describe_cooldown_rejection(_signal, _bar_idx=None, _tracker=None, *, cooldown_bars=None):
    return None


def describe_regime_rejection(_signal, _store=None, *, enabled=None, market=None):
    return None


def describe_risk_gate_rejection(*_a, **_k):
    return None


def risk_gate_kwargs(**_k) -> dict:
    return {}


def seed_cooldown_from_trades(_tracker, _trades) -> None:
    return None


def signal_reward_risk(signal) -> float | None:
    if (
        getattr(signal, "price", None) is None or signal.price <= 0
        or signal.stop_loss is None or signal.take_profit is None
    ):
        return None
    risk = abs(signal.price - signal.stop_loss)
    return abs(signal.take_profit - signal.price) / risk if risk > 0 else None


def _tail_column(store, signal, column: str, limit: int = 60) -> list[float] | None:
    """Last `limit` values of a store column for the Laya evidence snapshot."""
    try:
        df = store.get_df(signal.symbol, signal.timeframe, min_bars=2)
    except Exception:
        return None
    if df is None or len(df) == 0 or column not in df.columns:
        return None
    out: list[float] = []
    for value in df[column].tail(limit).tolist():
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if number == number:  # skip NaN
            out.append(number)
    return out or None

# TODO: re-enable IBKR when TWS/Gateway is available
# from broker.ibkr_client import IBKRClient
# from broker.order_manager import OrderManager
# from risk.risk_guard import RiskGuard, TradeIntent
from config import PATTERN_SCAN_HISTORY_BARS, settings
from core.market import (
    bar_identity,
    get_market,
    is_closed_session_bar,
    is_swing_timeframe,
    is_weekly_timeframe,
)
from core.signal_log_store import (
    SIGNAL_LOG_MAX,
    append_signal_log,
    load_signal_log,
)
from utils.logger import log

PATTERNS_DETECTED_FILE = Path("patterns_detected.md")
EXCLUDED_PATTERNS: set[str] = set()


def _protective_stop(stop, action: str, price: float) -> bool:
    """True when the indicator line still sits on the protective side of price."""
    try:
        stop_f = float(stop)
        price_f = float(price)
    except (TypeError, ValueError):
        return False
    if not math.isfinite(stop_f) or stop_f <= 0 or not math.isfinite(price_f):
        return False
    if action == "BUY":
        return stop_f < price_f
    if action == "SELL":
        return stop_f > price_f
    return False


class MarketScanner:
    def __init__(
        self,
        symbols: list[str] | None = None,
        exchange_overrides: dict[str, str] | None = None,
        paper_account: PaperAccount | None = None,
        disabled_patterns: list[str] | None = None,
        data_feed=None,
        scan_interval_seconds: int | None = None,
        volume_gate: bool | None = None,
        market: str | None = None,
        pattern_only: bool = False,
        indicators_only: bool = False,
        patterns_v2: bool = False,
        collect_first: bool | None = None,
        collect_first_top_n: int | None = None,
        duration_days: int | None = None,
        tv_stack_id: str = "",
        laya_mode: str = "off",
        laya_policy_id: str = "",
        laya_assess=None,
        rule_id: str = "",
        rule_filter_id: str = "",
        rule_filter_mode: str = "off",
        rule_filter_position: str = "after",
        kronos_gate_mode: str | None = None,
        kronos_gate_samples: int | None = None,
        kronos_exit_mode: str | None = None,
        kronos_exit_min_agree: int | None = None,
        stream_end: str | None = None,
    ):
        self._symbols = symbols or settings.symbols
        self._disabled_patterns = set(disabled_patterns or [])
        profile = get_market(market if market is not None else getattr(paper_account, "market", None))
        self._market = profile.id
        from data.edgar_client import set_skip_edgar

        set_skip_edgar(profile.skip_edgar)
        self._scan_interval = (
            profile.scan_interval_seconds
            if scan_interval_seconds is None
            else int(scan_interval_seconds)
        )
        self._volume_gate = settings.volume_gate_enabled if volume_gate is None else bool(volume_gate)
        self._volume_gate_rvol_min = settings.volume_gate_rvol_min
        self._volume_gate_obv_bars = settings.volume_gate_obv_bars
        self._tv_stack_id = (tv_stack_id or "").strip()
        self._rule_id = (rule_id or "").strip()
        self._rule_filter_id = (rule_filter_id or "").strip()
        self._rule_filter_mode = (rule_filter_mode or "off").strip().lower()
        if self._rule_filter_mode not in ("off", "shadow", "filter"):
            raise ValueError(f"Unsupported rule filter mode: {rule_filter_mode}")
        self._rule_filter_position = (rule_filter_position or "after").strip().lower()
        if self._rule_filter_position not in ("before", "after"):
            raise ValueError(f"Unsupported rule filter position: {rule_filter_position}")
        if self._rule_filter_position == "before" and self._rule_filter_mode == "shadow":
            raise ValueError("Before-placement rule filters require filter mode")
        self._indicators_only = (
            bool(indicators_only) or bool(self._tv_stack_id) or bool(self._rule_id)
        )
        self._patterns_v2 = bool(patterns_v2) and not self._indicators_only
        self._pattern_only = bool(pattern_only) and not self._indicators_only
        if self._rule_filter_position == "before" and not self._patterns_v2:
            # Before-placement is a Patternsv2 pre-scan gate. On any other
            # detector family it degrades to the normal post-signal filter.
            self._rule_filter_position = "after"
        self._collect_first = settings.collect_first_enabled if collect_first is None else bool(collect_first)
        self._collect_first_top_n = (
            settings.collect_first_top_n
            if collect_first_top_n is None
            else max(1, int(collect_first_top_n))
        )
        self._duration_days = (
            None if duration_days is None else max(1, int(duration_days))
        )
        self._stream_end=stream_end
        self._cutoff_reached=False
        self._tv = data_feed or TVClient(
            profile.tv_screener,
            profile.tv_exchange,
            exchange_overrides=exchange_overrides,
        )
        self._store = OHLCVStore(
            window=max(DEFAULT_WINDOW, settings.tv_history_days),
            session_tz=profile.session_tz,
        )
        self._renderer = ChartRenderer(save_to_disk=False, session_tz=profile.session_tz)
        self._vision = VisionChecker()
        # self._client   = IBKRClient()
        # self._orders   = OrderManager(self._client)
        # self._risk     = RiskGuard(self._client)
        self._paper = paper_account
        if self._paper is not None:
            self._paper.pattern_only = self._pattern_only
            self._paper.indicators_only = self._indicators_only
            from core.run_manifest import build_run_manifest
            self._paper.run_manifest = build_run_manifest(patterns_v2=self._patterns_v2, market=self._market)
        self._patterns: list[BasePattern] = []
        self._pattern_files: dict[str, str] = {}
        self._analyze_pool = None
        self._analyze_workers = 1
        self._running = False
        # Last *session* bar identity per (symbol, timeframe) — daily/weekly
        # keys are session dates, not last-print timestamps, so hourly scans
        # of a forming 1d candle do not count as new bars.
        self._last_bar_ts: dict[tuple[str, str], object] = {}
        # Persisted bar identities prevent a clean restart from processing
        # the same completed daily/weekly bar twice. Keep the in-memory object
        # representation above for existing tests, and compare a stable string
        # key against the persisted ledger state.
        self._persisted_bar_ids: dict[str, str] = (
            paper_account.processed_bar_identities_snapshot()
            if paper_account is not None
            and hasattr(paper_account, "processed_bar_identities_snapshot")
            else {}
        )
        self._decision_bar_ids = (paper_account.decision_bar_identities_snapshot()
                                  if paper_account is not None else {})
        self._pending_decision_ids: dict[str, str] = {}
        self._failed_decisions: set[str] = set()
        # Unique completed daily-session dates observed in this scanner run.
        # Counting per symbol/timeframe was wrong: 1,000 symbols would advance
        # the replay-day counter 1,000 times in a single market session.
        self._sim_day_keys: set[str] = set()
        self._sim_days: int = 0
        self._dead_symbols: set[str] = set()
        self._thin_logged: set[tuple[str, str]] = set()
        # One retry per symbol/timeframe/session day. A bar that is still
        # invalid on the next pass must not freeze the whole replay. The
        # fetch-skip path keys the same dict with ``(symbol, "invalid_candle",
        # replay_asof)`` — no timeframe is known there, and a real timeframe
        # never spells that code, so the two key spaces cannot collide.
        self._invalid_bar_tries: dict[tuple[str, str, str], int] = {}
        self._missing_execution_tries: dict[tuple, int] = {}
        self._replay_asof: str | None = None
        self._preprocessed_fills: set[tuple[str,str]] = set()
        self._kronos_retry_tries: dict[tuple, int] = {}
        self._kronos_failure_seen: set[tuple] = set()
        # Same (symbol, pattern) → (exit_bar_count, was_loss) cooldown map the
        # backtester uses — without this, paper re-entered losers immediately
        # while backtests waited cooldown_bars. Seed from the persisted ledger
        # so a scanner restart does not forget a just-stopped name.
        self._cooldown_tracker: dict[tuple[str, str], tuple[int, bool]] = {}
        if self._paper is not None:
            seed_cooldown_from_trades(self._cooldown_tracker, self._paper.closed)
        # Collect-first: chart-pattern signals gathered during a scan, ranked
        # after the scan completes, then only the top-N finished.
        self._collect_pool: list[tuple[TradeSignal, BasePattern | None, object]] = []
        # Monotonic start of the scan currently in flight (live clock).
        self._scan_started_monotonic: float | None = None
        # Scan-cycle health counters — surfaced by the paper trading UI/CLI
        # so a stalled or misbehaving scan is visible without reading logs.
        self.stats: dict = {
            "last_scan_at": None,
            "scan_duration_s": 0.0,
            # Scan clock — the paper tab renders these live: when the scan in
            # flight started, when it finished, and the run's total scan time.
            "scan_started_at": None,
            "scan_finished_at": None,
            "scan_in_progress": False,
            "scan_total_s": 0.0,
            "scans_completed": 0,
            "run_started_at": None,
            "run_finished_at": None,
            "patterns_found": 0,
            "signals_rejected": 0,
            "volume_gate_rejected": 0,
            "dii_rejected": 0,
            "kronos_rejected": 0,
            "kronos_exit_shadow": 0,
            "kronos_exits": 0,
            "collect_first_ranked": 0,
            "collect_first_selected": 0,
            "rule_filter_shadow": 0,
            "rule_filter_rejected": 0,
            "rule_filter_prefiltered": 0,
            "trades_opened": 0,
            "sim_days": 0,
            # Rejection counts are grouped by the first gate that vetoed the
            # signal. This makes a high aggregate "Rejected" count actionable
            # without requiring a user to grep bot.log.
            "rejection_by_gate": {},
            # Scan-health counters make a suspiciously fast "0 patterns"
            # cycle diagnosable without reading the raw log.
            "symbols_total": len(self._symbols),
            "symbols_with_snapshot": 0,
            "snapshot_errors": 0,
            "timeframe_requests": 0,
            "timeframe_snapshots_ok": 0,
            "new_bars": 0,
            "pattern_evaluations": 0,
            "daily_date_skew": 0,
            "daily_dates_seen": 0,
            "asof_skipped": 0,
            "invalid_bar_skipped": 0,
            "dead_symbols": 0,
        }
        # Ring buffer of per-signal accept/reject decisions. Persisted to
        # logs/paper_signals_{market}.jsonl — the web Paper Logs tab reads
        # that file, not this deque. Thread-safe — scan workers run concurrently.
        self._signal_log: deque[dict] = deque(
            load_signal_log(self._market), maxlen=SIGNAL_LOG_MAX,
        )
        self._signal_log_lock = threading.Lock()
        # Laya paper integration (docs/laya.md P5/P6). Frozen per book start;
        # mode changes require stopping and restarting that book. Off preserves
        # baseline behavior. Shadow snapshots eligible candidates and assesses
        # asynchronously with no effect on fills. Filter requires a valid
        # assessment under a pinned policy (paper/simulation entries only).
        if laya_mode not in ("off", "research", "shadow", "filter"):
            raise ValueError(f"Unsupported Laya mode: {laya_mode}")
        self._laya_mode = laya_mode
        self._laya_policy_id = (laya_policy_id or "").strip()
        self._laya_assess = laya_assess
        self._laya_shadow: list[dict] = []
        # Visual rule filter (Builder tab). Off unless a rule id + non-off mode
        # were supplied; the rule is loaded once, and per-bar evidence is cached
        # because several pattern families can fire on the same symbol/bar.
        self._rule_filter = None
        self._rule_filter_cache: dict = {}
        if self._rule_filter_id and self._rule_filter_mode != "off":
            self._load_rule_filter()
        # Kronos 1-day confirmation (docs/kronos-3-preds.md). Off constructs no
        # gate at all, so existing runs pay no import or subprocess cost.
        self._kronos_mode = (
            settings.kronos_gate_mode
            if kronos_gate_mode is None else str(kronos_gate_mode).strip().lower()
        )
        self._kronos_exit_mode = (
            settings.kronos_exit_mode
            if kronos_exit_mode is None else str(kronos_exit_mode).strip().lower()
        )
        self._kronos_gate = None
        self._kronos_pending: list = []
        self._kronos_exit_pending: list = []
        if self._kronos_mode != "off" or self._kronos_exit_mode != "off":
            from core.kronos_confirm import KronosConfirmGate

            self._kronos_gate = KronosConfirmGate(
                mode=self._kronos_mode,
                market=self._market,
                samples=(
                    settings.kronos_gate_samples
                    if kronos_gate_samples is None else int(kronos_gate_samples)
                ),
                model=settings.kronos_gate_model,
                sideways_atr=settings.kronos_gate_sideways_atr,
                sideways_min_pct=settings.kronos_gate_sideways_min_pct,
                sideways_max_pct=settings.kronos_gate_sideways_max_pct,
                min_agree=settings.kronos_gate_min_agree,
                exit_mode=self._kronos_exit_mode,
                exit_min_agree=(
                    settings.kronos_exit_min_agree
                    if kronos_exit_min_agree is None else int(kronos_exit_min_agree)
                ),
                cache_minutes=settings.kronos_gate_cache_minutes,
                timeout_s=settings.kronos_gate_timeout_s,
                max_checks_per_scan=settings.kronos_gate_max_checks_per_scan,
            )
            self._kronos_pending = self._kronos_gate.pending
            self._kronos_exit_pending = self._kronos_gate.pending_exits
        if self._paper is not None:
            self._paper.run_manifest['active_controls']={
                'account_model':self._paper.account_model,'volume_gate':self._volume_gate,
                'dii_mode':'shadow' if settings.dii_gate_shadow else settings.paper_dii_gate_mode,
                'vision':settings.vision_confirmation_enabled,'collect_first':self._collect_first,
                'kronos_entry':self._kronos_mode,'kronos_exit':self._kronos_exit_mode,
                'inactive_compatibility_gates':['minimum_confidence','SMA200_regime','post_loss_cooldown','generic_RR'],
                'geometry_and_net_target_checks':True,
                'liquidity_history_required':settings.paper_require_liquidity_history,
                'max_daily_share_participation':settings.paper_max_volume_participation,
            }

    def _load_rule_filter(self) -> None:
        from core.rule_contracts import RulePayload
        from core.rules_store import open_rules_store

        row = open_rules_store().get_rule(self._rule_filter_id)
        if row is None:
            raise ValueError(f"Unknown indicator rule: {self._rule_filter_id}")
        payload = dict(row.get("payload") or {})
        payload["name"] = row.get("name") or payload.get("name") or "rule filter"
        payload.setdefault("schema_version", 1)
        self._rule_filter = RulePayload.model_validate(payload)
        self._rule_filter_pattern = (
            self._rule_filter.attachments.patternsv2.pattern or "all"
        ).strip() or "all"

    def _rule_filter_matches_pattern(self, pattern: str) -> bool:
        target = getattr(self, "_rule_filter_pattern", "all")
        return target in ("", "all") or pattern == target

    def _rule_filter_evidence(self, symbol: str, timeframe: str) -> dict:
        from analysis.rule_engine import evaluate_bar, required_bars

        rule = self._rule_filter
        if rule is None:
            return {"matched": True, "warm": True, "conditions": []}
        df = self._store.get_df(symbol, timeframe, min_bars=required_bars(rule))
        if df is None:
            return {"matched": False, "warm": False, "conditions": [],
                    "reason": "insufficient history for the rule"}
        key = (symbol, timeframe, df.index[-1], len(df),
               float(df["close"].iloc[-1]))
        cached = self._rule_filter_cache.get(key)
        if cached is not None:
            return cached
        evidence = evaluate_bar(rule, df, -1)
        self._rule_filter_cache[key] = evidence
        return evidence

    def _rule_prefilter(self, symbol: str, timeframe: str) -> bool:
        """Before-placement Patternsv2 gate: true when detection may run."""
        rule = getattr(self, "_rule_filter", None)
        if rule is None:
            return True
        if (getattr(self, "_rule_filter_mode", "off") != "filter"
                or getattr(self, "_rule_filter_position", "after") != "before"):
            return True
        evidence = self._rule_filter_evidence(symbol, timeframe)
        if evidence.get("matched"):
            return True
        self.stats["rule_filter_prefiltered"] = (
            self.stats.get("rule_filter_prefiltered", 0) + 1)
        failed = [
            f"{item['label']} (got {item.get('left')})"
            for item in evidence.get("conditions", [])
            if not item.get("pass")
        ] or [evidence.get("reason") or "rule not satisfied"]
        log.debug(
            f"Rule prefilter SKIP | {symbol} {timeframe} | "
            + "; ".join(failed)
        )
        return False

    def signal_log_snapshot(self) -> list[dict]:
        with self._signal_log_lock:
            return list(self._signal_log)

    def clear_signal_log_memory(self) -> None:
        with self._signal_log_lock:
            self._signal_log.clear()

    def _append_signal_log(
        self,
        signal: TradeSignal,
        *,
        status: str,
        reason: str,
    ) -> None:
        if not signal.signal_id:
            import hashlib
            key = repr((self._market,signal.symbol,signal.timeframe,signal.pattern,
                        signal.action,signal.signal_bar_timestamp))
            signal.signal_id = hashlib.sha256(key.encode()).hexdigest()
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "market": self._market,
            "symbol": signal.symbol,
            "timeframe": signal.timeframe,
            "action": signal.action,
            "pattern": signal.pattern,
            "confidence": round(float(signal.confidence), 4),
            "price": round(float(signal.price), 4) if signal.price is not None else None,
            "status": status,
            "reason": reason,
            "sim_bar": (
                signal.signal_bar_timestamp.isoformat()
                if signal.signal_bar_timestamp is not None else None
            ),
            "sim_bar_idx": signal.signal_bar_idx,
            "chart_annotations": deepcopy(signal.chart_annotations),
            "kronos_evidence": deepcopy(getattr(signal, "kronos_evidence", None)),
            **__import__("core.pattern_provenance", fromlist=["payload"]).payload(signal),
            'run_id': (getattr(self, '_paper', None).run_manifest.get('run_id')
                       if getattr(self, '_paper', None) is not None else None),
        }
        if signal.signal_bar_timestamp is not None:
            bars = [c for c in self._store.copy_candles(signal.symbol, signal.timeframe)
                    if c.timestamp is not None and c.timestamp.tzinfo is not None
                    and c.timestamp <= signal.signal_bar_timestamp][-60:]
            entry['decision_context'] = [
                {**{k:getattr(c,k) for k in ('open','high','low','close','volume')},
                 'timestamp':c.timestamp.isoformat()} for c in bars]
        with self._signal_log_lock:
            self._signal_log.append(entry)
        try:
            append_signal_log(self._market, entry)
        except OSError:
            log.exception(f"Signal log | failed to persist {self._market} row")
            self._failed_decisions.add(f'{signal.symbol}|{signal.timeframe}')
            self.stats['snapshot_errors'] += 1
            raise

        if status == "rejected":
            reason_lower = reason.lower()
            if reason_lower.startswith("short entries are disabled"):
                gate = "short_disabled"
            elif reason_lower.startswith("dii "):
                gate = "dii"
            elif reason_lower.startswith("opening gap"):
                gate = "opening_gap"
            elif reason_lower.startswith(("entry price", "stop and target", "stop cap")):
                gate = "entry_geometry"
            elif "kronos" in reason_lower:
                gate = "kronos"
            elif reason_lower.startswith("execution r:r rejection"):
                gate = "execution_rr"
            elif "portfolio full" in reason_lower:
                gate = "max_positions"
            elif "gross exposure cap" in reason_lower:
                gate = "gross_exposure"
            elif "insufficient cash" in reason_lower:
                gate = "cash"
            elif "lot rounding" in reason_lower:
                gate = "lot_rounding"
            elif "session " in reason_lower:
                gate = "session"
            elif "collect-first" in reason_lower:
                gate = "collect_first"
            elif "confidence" in reason_lower:
                gate = "confidence"
            elif "regime" in reason_lower or "sma200" in reason_lower:
                gate = "regime"
            elif "cooldown" in reason_lower:
                gate = "cooldown"
            elif "long-only" in reason_lower:
                gate = "long_only"
            elif "volume" in reason_lower:
                gate = "volume"
            elif "vision" in reason_lower:
                gate = "vision"
            elif "risk gate" in reason_lower:
                gate = "risk"
            else:
                gate = "other"
            stats = getattr(self, "stats", None)
            if not isinstance(stats, dict):
                return
            by_gate = stats.setdefault("rejection_by_gate", {})
            by_gate[gate] = by_gate.get(gate, 0) + 1

    def _append_exit_log(self, position) -> None:
        """Record a Kronos-driven close in the Paper Logs JSONL."""
        evidence = deepcopy(getattr(position, "kronos_exit_evidence", None)) or {}
        reason = (
            f"Kronos exit: {evidence.get('reason') or 'forecasts opposed the position'}"
        )
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "market": self._market,
            "symbol": position.symbol,
            "timeframe": position.timeframe,
            "action": position.action,
            "pattern": position.pattern,
            "confidence": round(float(position.confidence or 0.0), 4),
            "price": (
                round(float(position.exit_price), 4)
                if position.exit_price is not None else None
            ),
            "status": "exited",
            "reason": reason,
            "exit_reason": position.exit_reason,
            "sim_bar": (
                position.sim_exit_date.isoformat()
                if position.sim_exit_date is not None else None
            ),
            "sim_bar_idx": position.exit_bar_idx,
            "chart_annotations": deepcopy(position.chart_annotations),
            "kronos_evidence": deepcopy(getattr(position, "kronos_evidence", None)),
            "kronos_exit_evidence": evidence,
            **__import__("core.pattern_provenance", fromlist=["payload"]).payload(position),
        }
        with self._signal_log_lock:
            self._signal_log.append(entry)
        try:
            append_signal_log(self._market, entry)
        except OSError:
            log.exception(
                f"Signal log | failed to persist Kronos exit for {position.symbol}"
            )

    # ── Lifecycle ──────────────────────────────────────────────────────────────
    def start(self) -> None:
        # self._client.connect()
        self._discover_patterns()
        self._init_patterns_detected_file()
        for p in self._patterns:
            p.on_start()
        self._open_analyze_pool()
        self._running = True
        log.info(
            f"Scanner started | "
            f"symbols={self._symbols} | "
            f"patterns={[p.name for p in self._patterns]} | "
            f"volume_gate={'ON' if self._volume_gate else 'OFF'} | "
            f"collect_first={'ON' if self._collect_first else 'OFF'} | "
            f"duration_days="
            f"{self._duration_days if self._duration_days is not None else 'unlimited'} | "
            f"analyze_workers={self._analyze_workers} | "
            f"interval={self._scan_interval}s"
        )

    def stop(self) -> None:
        from core.pattern_editor_db import DatabaseUnavailable
        self._running = False
        getattr(self,'stats',{}).setdefault('termination_reason', 'stopped')
        self._close_analyze_pool()
        for p in self._patterns:
            p.on_stop()
        try:
            self._note_worker(stopped=True)
        except DatabaseUnavailable:
            # Worker bookkeeping must not turn a stop into a crash.
            log.warning(
                "Scanner | worker acknowledgement skipped — editor database unavailable"
            )
        # self._client.disconnect()
        log.info("Scanner stopped")

    def _note_worker(self, *, stopped: bool = False) -> None:
        """Publish the pinned pattern set. Indicators are not pattern versions."""
        if getattr(self, "_indicators_only", False) or getattr(self, "_patterns_v2", False):
            return
        from core.pattern_loader import acknowledge_worker
        acknowledge_worker(self, stopped=stopped)

    def _stop_if_duration_reached(self) -> bool:
        """End the scan loop after N unique market sessions (sim_days)."""
        if self._duration_days is None:
            return False
        if not self.stats.get("scan_complete", True):
            return False
        if self._sim_days < self._duration_days:
            return False
        log.info(
            f"Scanner | duration-days={self._duration_days} reached "
            f"(sim_days={self._sim_days}) — stopping"
        )
        self._running = False
        self.stats['termination_reason'] = 'duration_reached'
        return True

    def _open_analyze_pool(self) -> None:
        from core.pattern_jobs import (
            analyze_worker_count,
            make_analyze_pool,
            make_v2_pool,
        )

        self._close_analyze_pool()
        if self._indicators_only:
            # Indicators run in-process. They are not pinned versions.
            self._analyze_workers = 1
            self._analyze_pool = None
            return
        if getattr(self, "_patterns_v2", False):
            n = analyze_worker_count()
            self._analyze_workers = n
            self._analyze_pool = make_v2_pool(n)
            if self._analyze_pool is not None:
                log.info(f"Scanner | patternsv2 analyze pool: {n} spawn workers")
            return
        n = analyze_worker_count()
        self._analyze_workers = n
        self._analyze_pool = make_analyze_pool(
            disabled=self._disabled_patterns,
            session_tz=get_market(self._market).session_tz,
            skip_edgar=get_market(self._market).skip_edgar,
            window=max(DEFAULT_WINDOW, settings.tv_history_days),
            workers=n,
            version_set=self._version_set,
        )
        if self._analyze_pool is not None:
            log.info(f"Scanner | pattern analyze pool: {n} spawn workers")

    def _close_analyze_pool(self) -> None:
        pool = self._analyze_pool
        self._analyze_pool = None
        if pool is None:
            return
        try:
            pool.shutdown(wait=False, cancel_futures=True)
        except TypeError:
            pool.shutdown(wait=False)

    async def _sleep_until_next_scan(self) -> None:
        """Sleep only the unused remainder of the scan interval.

        Stream replay already advances once per completed scan. Interval 0
        means scan-paced (no leftover sleep). Sleeping a full 60s after a
        long scan just idles the replay.
        """
        if self._scan_interval <= 0:
            if not self.stats.get("scan_complete", True):
                await asyncio.sleep(1.0)
            return
        elapsed = float(self.stats.get("scan_duration_s") or 0.0)
        remaining = self._scan_interval - elapsed
        if remaining > 0:
            await asyncio.sleep(remaining)
            return
        log.info(
            f"Scan | skip interval sleep — {elapsed:.1f}s >= {self._scan_interval}s"
        )

    def scan_timing(self) -> dict[str, Any]:
        """Live scan clock: the scan in flight, plus the book's total time.

        Cumulative time/count are read from the paper account so the desk shows
        the entire book's scanning time (across start/stop runs), not just the
        last cycle. The paper tab polls this every couple of seconds and ticks
        the in-flight scan locally, so a 30-second scan is visible while it
        runs instead of only showing up as a finished figure.
        """
        now = time.monotonic()
        started = getattr(self, "_scan_started_monotonic", None)
        in_progress = bool(self.stats.get("scan_in_progress"))
        paper = getattr(self, "_paper", None)
        if paper is not None:
            clock = paper.scan_clock()
        else:
            # Bare scanners (CLI/tests) have no book to accumulate into.
            scans = int(self.stats.get("scans_completed") or 0)
            total = round(float(self.stats.get("scan_total_s") or 0.0), 2)
            clock = {
                "scan_total_s": total,
                "scans_completed": scans,
                "avg_scan_s": round(total / scans, 2) if scans else None,
                "last_scan_duration_s": round(
                    float(self.stats.get("scan_duration_s") or 0.0), 2
                ),
                "last_scan_at": self.stats.get("last_scan_at"),
            }
        run_started_at = self.stats.get("run_started_at")
        run_finished_at = self.stats.get("run_finished_at")
        run_elapsed = None
        if run_started_at:
            try:
                start_dt = datetime.fromisoformat(str(run_started_at))
                end_dt = (
                    datetime.fromisoformat(str(run_finished_at))
                    if run_finished_at else datetime.now(timezone.utc)
                )
                run_elapsed = round((end_dt - start_dt).total_seconds(), 1)
            except ValueError:
                run_elapsed = None
        return {
            **clock,
            "run_started_at": run_started_at,
            "run_finished_at": run_finished_at,
            "run_elapsed_s": run_elapsed,
            "scan_started_at": self.stats.get("scan_started_at"),
            "scan_finished_at": self.stats.get("scan_finished_at"),
            "scan_in_progress": in_progress,
            "scan_elapsed_s": (
                round(now - started, 1)
                if in_progress and started is not None else None
            ),
        }

    def _reference_symbol(self) -> str | None:
        if not self._symbols:
            return None
        wanted = {s.upper() for s in self._symbols}
        refs = (
            ("BDO", "SM", "ICT", "ALI", "BPI")
            if self._market == "ph"
            else ("SPY", "AAPL", "MSFT", "QQQ")
        )
        for symbol in refs:
            if symbol in wanted:
                return next(s for s in self._symbols if s.upper() == symbol)
        return self._symbols[0]

    def _note_dead_symbol(self, symbol: str, reason: str) -> None:
        if symbol in self._dead_symbols:
            return
        self._dead_symbols.add(symbol)
        self.stats["dead_symbols"] = len(self._dead_symbols)
        log.warning(f"Scan | drop {symbol} for this run — {reason}")

    def _note_fetch_skip(self, symbol: str, exc: FetchSkip) -> None:
        if self._paper is not None and self._paper.open_for(symbol):
            self._note_missing_execution(symbol, str(exc))
            return
        if exc.code == "no_data":
            self._note_dead_symbol(symbol, str(exc))
        elif exc.code == "asof_mismatch":
            self.stats["asof_skipped"] += 1
        elif exc.code == "invalid_candle":
            # A degenerate bar (zero/NaN prices) is a data defect, not a
            # transport failure: re-reading the same session cannot repair it.
            # Count one error so the cycle retries once, then treat the symbol
            # as having no bar this session. Counting it every scan would keep
            # scan_complete False forever, and an incomplete scan never
            # advances the replay date — the book would sit on one session.
            key = (symbol, exc.code, str(self._replay_asof or ""))
            tries = self._invalid_bar_tries.get(key, 0) + 1
            self._invalid_bar_tries[key] = tries
            if tries == 1:
                self.stats["snapshot_errors"] += 1
                log.warning(
                    f"Scan | invalid candle for {symbol}: {exc}; retrying"
                )
            else:
                self.stats["invalid_bar_skipped"] = (
                    self.stats.get("invalid_bar_skipped", 0) + 1
                )
        else:
            # history_unavailable and other transients: skip this cycle.
            self.stats["snapshot_errors"] += 1

    def _note_missing_execution(self, symbol: str, reason: str) -> None:
        if self._paper is None or not self._paper.open_for(symbol):
            return
        self.stats['snapshot_errors'] += 1
        self.stats['held_coverage_errors'] = self.stats.get('held_coverage_errors', 0) + 1
        key = (symbol, self._replay_asof)
        tries = self._missing_execution_tries.get(key, 0) + 1
        self._missing_execution_tries[key] = tries
        for p in self._paper.open_for(symbol):
            self._paper.record_execution_gap(symbol, p.timeframe, self._replay_asof, reason)
        if tries >= 3 and self._paper.assume_session_open:
            self.stats['termination_reason'] = 'missing_execution_bars'
            self._running = False
            log.error(f'Replay stopped: missing execution evidence for held {symbol}: {reason}')

    def _feed_worker_count(self) -> int:
        n = max(len(self._symbols), 1)
        conc = min(settings.scanner_concurrency, n)
        fetch_many = getattr(self._tv, "fetch_snapshots", None)
        batch = int(getattr(self._tv, "snapshot_batch_size", 1) or 1)
        if fetch_many is None or batch <= 1:
            return conc
        n_batches = max(1, (n + batch - 1) // batch)
        return min(conc, n_batches)

    async def _preload_feed(self, feed_sessions: list | None) -> None:
        preload = getattr(self._tv, "preload_universe", None)
        if preload is None:
            return
        session = feed_sessions[0] if feed_sessions else None
        try:
            summary = await preload(self._symbols, session)
        except Exception:
            log.exception("Scanner | universe preload failed — scan will lazy-load")
            return
        if summary:
            log.info(
                f"Scan | preloaded tapes loaded={summary.get('loaded')} "
                f"empty={summary.get('empty')} unavailable={summary.get('unavailable')}"
            )

    def _pin_candidates(self) -> list[str]:
        """Liquid names first; skip symbols already dropped as dead."""
        ordered: list[str] = []
        seen: set[str] = set()
        primary = self._reference_symbol()
        if primary:
            ordered.append(primary)
            seen.add(primary.upper())
        for symbol in self._symbols:
            key = symbol.upper()
            if key in seen or symbol in self._dead_symbols:
                continue
            ordered.append(symbol)
            seen.add(key)
            if len(ordered) >= 12:
                break
        return ordered

    async def _pin_replay_asof(self, feed_sessions: list | None) -> None:
        pin = getattr(self._tv, "pin_replay_asof", None)
        if pin is None or not feed_sessions:
            return
        for ref in self._pin_candidates():
            asof_day = await pin(ref, feed_sessions[0])
            if asof_day:
                self._replay_asof = asof_day
                log.info(f"Scan | replay asof pinned to {asof_day} via {ref}")
                return

    # ── Main async loop ────────────────────────────────────────────────────────
    async def _start_when_ready(self) -> None:
        """Resolve pattern versions, waiting out a transient database outage.

        Pinned versions live in PostgreSQL, so one connection blip at start
        used to end the book outright. Retry until they resolve, or until the
        run is cancelled (PaperBook.stop cancels this task).
        """
        from core.pattern_editor_db import DatabaseUnavailable

        delay = min(max(self._scan_interval, 5.0), 60.0)
        while True:
            try:
                self.start()
                return
            except DatabaseUnavailable as exc:
                log.warning(
                    f"Scanner | pattern versions unavailable ({exc}) — "
                    f"retrying in {delay:.0f}s"
                )
            await asyncio.sleep(delay)

    async def run(self) -> None:
        await self._start_when_ready()
        self.stats["run_started_at"] = datetime.now(timezone.utc).isoformat()
        self.stats["run_finished_at"] = None
        n_workers = self._feed_worker_count()
        try:
            while self._running:
                try:
                    async with self._open_feed_sessions(n_workers) as sessions:
                        await self._preload_feed(sessions)
                        while self._running:
                            try:
                                if self._paper is not None:
                                    self._paper.tick()
                                await self._scan_all(feed_sessions=sessions)
                                if self._paper is not None:
                                    self._paper.save()
                                if self._stop_if_duration_reached():
                                    continue
                            except Exception:
                                # Broken MCP/stdio pipe: drop the pool and
                                # reopen rather than reuse a dead session.
                                log.exception(
                                    "Scanner | scan cycle failed — restarting data sessions"
                                )
                                break
                            await self._sleep_until_next_scan()
                except Exception:
                    log.exception(
                        "Scanner | failed to open data sessions — retrying next interval"
                    )
                    await asyncio.sleep(
                        self._scan_interval if self._scan_interval > 0 else 1.0
                    )
        finally:
            self.stats["run_finished_at"] = datetime.now(timezone.utc).isoformat()
            self.stats["scan_in_progress"] = False
            self.stop()

    @asynccontextmanager
    async def _open_feed_sessions(self, n: int):
        """Keep N MCP/stream sessions for the whole scan loop, not each cycle.

        Opening and killing tradingview-mcp on every scan races the SDK's
        killpg() against an already-exited child (ESRCH spam in the tqdm bar).
        """
        async with AsyncExitStack() as stack:
            sessions = [
                await stack.enter_async_context(self._tv.mcp_session())
                for _ in range(n)
            ]
            yield sessions

    # ── Scan cycle ─────────────────────────────────────────────────────────────
    async def _scan_all(self, feed_sessions: list | None = None) -> None:
        self._note_worker()
        """Run one full scan across all symbols x timeframes x patterns.

        Symbols are processed concurrently with a progress bar. Each worker
        uses its own MCP session so there is no contention on the stdio pipe.
        """
        scan_start = time.monotonic()
        self._scan_started_monotonic = scan_start
        self.stats["scan_started_at"] = datetime.now(timezone.utc).isoformat()
        self.stats["scan_finished_at"] = None
        self.stats["scan_in_progress"] = True
        self._collect_pool = []
        self._kronos_pending.clear()
        self._kronos_exit_pending.clear()
        self.stats["patterns_found"] = 0
        self.stats["signals_rejected"] = 0
        self.stats["trades_opened"] = 0
        self.stats["rejection_by_gate"] = {}
        self.stats["scan_complete"] = False
        self._pending_decision_ids.clear()
        self._failed_decisions.clear()
        self._kronos_failure_seen.clear()
        self.stats["daily_date_skew"] = 0
        snapshot_errors_before = self.stats["snapshot_errors"]

        all_timeframes: set[str] = set()
        for p in self._patterns:
            all_timeframes.update(p.timeframes)

        fetch_many = getattr(self._tv, "fetch_snapshots", None)
        batch_size = 1
        if fetch_many is not None:
            batch_size = max(1, int(getattr(self._tv, "snapshot_batch_size", 1) or 1))
        concurrency = settings.scanner_concurrency
        log.info(
            f"Scan | {len(self._symbols)} symbols x {len(all_timeframes)} timeframes "
            f"({sorted(all_timeframes)}) x {len(self._patterns)} patterns | "
            f"concurrency={concurrency}"
            + (f" batch={batch_size}" if batch_size > 1 else "")
            + (
                f" analyze_pool={self._analyze_workers}"
                if self._analyze_pool is not None else " analyze=inline"
            )
        )

        # Latest detected signal per (symbol, timeframe) — unused for disk
        # charts (those render on dashboard click) but kept for vision checks.
        new_closed_daily = False
        funded = self._paper is not None and self._paper.account_model == 'funded'
        funded_snapshots = []
        self._preprocessed_fills.clear()
        self._scan_snapshot_symbols: set[str] = set()
        self._scan_daily_dates: set[str] = set()
        self.stats["asof_skipped"] = 0
        self.stats["invalid_bar_skipped"] = 0
        await self._pin_replay_asof(feed_sessions)

        from tqdm import tqdm

        # Fill a work queue with every symbol
        queue: asyncio.Queue[str] = asyncio.Queue()
        for s in self._symbols:
            queue.put_nowait(s)

        pbar = tqdm(total=len(self._symbols), desc="Scanning", unit="sym", ncols=80)

        def _take_batch() -> list[str]:
            batch: list[str] = []
            take = batch_size if fetch_many is not None else 1
            while len(batch) < take:
                try:
                    symbol = queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if symbol in self._dead_symbols:
                    pbar.update(1)
                    continue
                batch.append(symbol)
            return batch

        async def _apply_result(symbol: str, timeframe: str, result) -> MarketSnapshot | None:
            nonlocal new_closed_daily
            self.stats["timeframe_requests"] += 1
            if isinstance(result, FetchSkip):
                self._note_fetch_skip(symbol, result)
                return None
            if result is None:
                self.stats["snapshot_errors"] += 1
                self._note_missing_execution(symbol, 'snapshot unavailable')
                return None
            if self._stream_end:
                from data.chart_dates import chart_session_day
                day=chart_session_day(result.candle.timestamp,get_market(self._market).session_tz)
                if day is not None and day.date().isoformat()>self._stream_end:
                    self._cutoff_reached=True
                    return None
            if funded:
                funded_snapshots.append((symbol,timeframe,result))
                return None
            closed, pending = self._apply_bar(symbol, timeframe, result)
            if closed:
                new_closed_daily = True
            return pending

        async def _drain(mcp) -> None:
            while True:
                batch = _take_batch()
                if not batch:
                    return
                pending: list[MarketSnapshot] = []
                if fetch_many is not None:
                    for timeframe in all_timeframes:
                        try:
                            results = await fetch_many(
                                batch, timeframe,
                                store=self._store, mcp_session=mcp,
                            )
                        except ConnectionError:
                            if pending:
                                await self._run_analyzes(pending)
                            raise
                        except Exception as exc:
                            self.stats["snapshot_errors"] += len(batch)
                            log.warning(f"Scan | batch snapshot failed: {exc}")
                            break
                        for symbol in batch:
                            snap = await _apply_result(
                                symbol, timeframe, (results or {}).get(symbol),
                            )
                            if snap is not None:
                                pending.append(snap)
                else:
                    for symbol in batch:
                        for timeframe in all_timeframes:
                            try:
                                snapshot = await self._tv.fetch_snapshot(
                                    symbol, timeframe,
                                    store=self._store, mcp_session=mcp,
                                )
                            except ConnectionError:
                                if pending:
                                    await self._run_analyzes(pending)
                                raise
                            except FetchSkip as exc:
                                self.stats["timeframe_requests"] += 1
                                self._note_fetch_skip(symbol, exc)
                                continue
                            except Exception as exc:
                                self.stats["timeframe_requests"] += 1
                                self.stats["snapshot_errors"] += 1
                                log.warning(
                                    f"Scan | snapshot failed {symbol} {timeframe}: {exc}"
                                )
                                continue
                            snap = await _apply_result(symbol, timeframe, snapshot)
                            if snap is not None:
                                pending.append(snap)
                if pending:
                    await self._run_analyzes(pending)
                pbar.update(len(batch))

        async def _owned_session_worker() -> None:
            async with self._tv.mcp_session() as mcp:
                await _drain(mcp)

        n_workers = min(self._feed_worker_count(), max(len(self._symbols), 1))
        if feed_sessions:
            workers = [_drain(mcp) for mcp in feed_sessions[:n_workers]]
        else:
            workers = [_owned_session_worker() for _ in range(n_workers)]
        tasks = [asyncio.create_task(worker) for worker in workers]
        try:
            # Applied bars must finish analysis before reconnecting; cancelling
            # a healthy worker here would lose signals on its processed bars.
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for result in results:
                if isinstance(result, BaseException):
                    if self._collect_first:
                        await self._flush_collect_first()
                    await self._flush_kronos_confirm()
                    raise result
        finally:
            # Drain cancellation too: no workers may outlive this scan.
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            pbar.close()

        if funded:
            from core.candle_validation import candle_error
            ordered = sorted(funded_snapshots,key=lambda item:(item[0],item[1]))
            # All opening fills happen before any full-bar exits, so an
            # earlier symbol's future intrabar proceeds cannot fund a later
            # symbol's opening order. Feed completion order cannot pick seats.
            for symbol,timeframe,snapshot in ordered:
                if not candle_error(snapshot.candle):
                    self._paper.mark_opening_price(symbol,snapshot.candle.open)
            for symbol,timeframe,snapshot in ordered:
                identity=bar_identity(timeframe,snapshot.candle.timestamp,market=self._market)
                if (not candle_error(snapshot.candle)
                        and is_closed_session_bar(timeframe,snapshot.candle.timestamp,market=self._market)
                        and self._persisted_bar_ids.get(f'{symbol}|{timeframe}') != repr(identity)):
                    self._fill_pending_observation(symbol,timeframe,snapshot.candle)
                    self._preprocessed_fills.add((symbol,timeframe))
            pending=[]
            for symbol,timeframe,snapshot in ordered:
                closed,snap=self._apply_bar(symbol,timeframe,snapshot)
                new_closed_daily = new_closed_daily or closed
                if snap is not None:
                    pending.append(snap)
            await self._run_analyzes(pending)

        if self._collect_first:
            await self._flush_collect_first()
        # One batched Kronos call per scan. collect-first may have added
        # pendings, so this must run after that flush.
        await self._flush_kronos_confirm()

        # Management is already idempotently committed; decisions commit only
        # after detection and the gate batch have reached a recorded outcome.
        for key, identity in self._pending_decision_ids.items():
            if key not in self._failed_decisions:
                self._decision_bar_ids[key] = identity
                if self._paper is not None:
                    self._paper.mark_decision_processed(key, identity)

        # Mark equity against the completed simulated bar BEFORE advancing the
        # replay. PaperAccount deduplicates marks to one point per market
        # session, so Sharpe is a daily/session statistic rather than a
        # function of the scanner's 60-second polling interval.
        if self._paper is not None and (self._paper.sim_now() is not None or not self._cutoff_reached):
            self._paper.mark_to_market(self._paper.sim_now())

        # Historical stream replay is advanced only after every symbol has
        # been scanned. A 1,000-symbol scan can take >60s, so a wall-clock
        # stream timer would otherwise make different symbols see different
        # simulated dates within the same scan.
        advance_replay = getattr(self._tv, "advance_replay", None)
        self.stats["scan_complete"] = self.stats["snapshot_errors"] == snapshot_errors_before
        if self._cutoff_reached:
            self.stats['termination_reason']='stream_end_reached'
            self._running=False
        elif advance_replay is not None and not self.stats["scan_complete"]:
            log.warning("Scanner | retrying replay date after incomplete snapshots")
        elif advance_replay is not None:
            control_session = feed_sessions[0] if feed_sessions else None
            ok, reached_end = await advance_replay(control_session)
            if not ok:
                log.warning("Scanner | paper replay did not advance after scan")
            elif reached_end:
                log.info(
                    "Scanner | paper replay reached end of tape — stopping "
                    f"after {self._sim_days} session(s)"
                )
                self._running = False
                self.stats['termination_reason'] = 'end_of_tape'


        self.stats["symbols_with_snapshot"] = len(self._scan_snapshot_symbols)
        self.stats["daily_dates_seen"] = len(self._scan_daily_dates)
        self.stats["dead_symbols"] = len(self._dead_symbols)
        if len(self._scan_daily_dates) > 1:
            # The stream server is supposed to advance all loaded tapes
            # atomically. Missing/irregular symbol histories can still cause a
            # date skew; surface it rather than silently mixing market days.
            self.stats["daily_date_skew"] = (
                len(self._scan_daily_dates) - 1
            )
            log.warning(
                f"Scan | replay date skew detected: "
                f"daily dates={sorted(self._scan_daily_dates)}"
            )
        duration = round(time.monotonic() - scan_start, 2)
        self.stats["last_scan_at"] = datetime.now(timezone.utc).isoformat()
        self.stats["scan_duration_s"] = duration
        self.stats["scan_finished_at"] = self.stats["last_scan_at"]
        self.stats["scan_in_progress"] = False
        self.stats["scans_completed"] = int(self.stats.get("scans_completed") or 0) + 1
        totals = self.stats.setdefault('run_totals', {})
        for name in ('patterns_found','signals_rejected','trades_opened'):
            totals[name] = totals.get(name,0) + self.stats[name]
        by_gate = totals.setdefault('rejection_by_gate', {})
        for gate, count in self.stats['rejection_by_gate'].items():
            by_gate[gate] = by_gate.get(gate,0) + count
        self.stats["scan_total_s"] = round(
            float(self.stats.get("scan_total_s") or 0.0) + duration, 2
        )
        if self._paper is not None:
            self._paper.record_scan(duration)
        log.info(f"Scan complete in {duration:.1f}s")

    def _apply_bar(self, symbol: str, timeframe: str, snapshot) -> tuple[bool, MarketSnapshot | None]:
        """Ledger + bar identity on the scan loop. Snapshot if patterns should run."""
        candle = snapshot.candle
        from core.candle_validation import candle_error
        invalid = candle_error(candle)
        if invalid:
            self._note_missing_execution(symbol, invalid)
            stamp = candle.timestamp
            day = stamp.date().isoformat() if isinstance(stamp, datetime) else ""
            key = (symbol, timeframe, day)
            tries = self._invalid_bar_tries.get(key, 0) + 1
            self._invalid_bar_tries[key] = tries
            if tries == 1:
                self.stats["snapshot_errors"] += 1
                log.warning(
                    f"Scan | invalid candle for {symbol} {timeframe}: {invalid}; retrying"
                )
            elif tries == 2:
                log.warning(
                    f"Scan | skip {symbol} {timeframe} — invalid candle prices after retry"
                )
            return False, None
        self.stats["timeframe_snapshots_ok"] += 1
        self._scan_snapshot_symbols.add(symbol)

        bar_key = (symbol, timeframe)
        bar_ts = snapshot.candle.timestamp
        identity = bar_identity(
            timeframe, bar_ts, market=self._market,
        )
        closed_bar = is_closed_session_bar(
            timeframe, bar_ts, market=self._market,
        )
        identity_key = repr(identity) if identity is not None else None
        persisted_key = self._persisted_bar_ids.get(f"{symbol}|{timeframe}")
        is_new_bar = (
            closed_bar
            and identity is not None
            and self._last_bar_ts.get(bar_key) != identity
            and identity_key != persisted_key
        )
        new_closed_daily = False
        if is_new_bar:
            self._rule_filter_cache.clear()
        if is_new_bar and bar_ts is not None:
            self.stats["new_bars"] += 1
            if is_swing_timeframe(timeframe) and not is_weekly_timeframe(timeframe):
                local_ts = bar_ts.astimezone(
                    ZoneInfo(get_market(self._market).session_tz)
                )
                session_date = local_ts.strftime("%Y-%m-%d")
                self._scan_daily_dates.add(session_date)
                self._sim_day_keys.add(session_date)
                self._sim_days = len(self._sim_day_keys)
                self.stats["sim_days"] = self._sim_days
                new_closed_daily = True

        if self._paper is not None:
            if is_new_bar and self._paper.assume_session_open:
                if (symbol,timeframe) not in self._preprocessed_fills:
                    self._fill_pending_observation(symbol,timeframe,snapshot.candle)
            closed_positions = self._paper.on_bar(
                symbol, snapshot.candle, timeframe, is_new_bar,
            )
            if not closed_bar and not self._paper.assume_session_open:
                closed_positions += self._paper.on_quote(
                    symbol, snapshot.candle, timeframe, observed_at=snapshot.timestamp,
                )
            for closed in closed_positions:
                bar_idx = self._paper.bar_count(
                    closed.symbol, closed.timeframe,
                )
                self._cooldown_tracker[
                    (closed.symbol, closed.pattern)
                ] = (bar_idx, closed.pnl < 0)
                if closed.exit_reason == "kronos_exit":
                    log.info(
                        f"Kronos EXIT FILLED | {closed.symbol} "
                        f"{closed.pattern} | pnl={closed.pnl_pct:+.2f}%"
                    )
                    self._append_exit_log(closed)
            if is_new_bar:
                self._queue_kronos_exits(symbol, timeframe, snapshot.candle)
                self._trail_indicators(symbol, timeframe, snapshot.candle)

        if identity is not None and closed_bar:
            self._last_bar_ts[bar_key] = identity
            if identity_key is not None and self._paper is not None:
                self._paper.mark_bar_processed(symbol, timeframe, identity_key)
                self._persisted_bar_ids[f"{symbol}|{timeframe}"] = identity_key
        decision_key = f'{symbol}|{timeframe}'
        needs_decision = (closed_bar and identity_key is not None
                          and self._decision_bar_ids.get(decision_key) != identity_key)
        if not needs_decision:
            return new_closed_daily, None
        self._pending_decision_ids[decision_key] = identity_key
        if not is_new_bar:
            # An unassessed exit can retry its completed context without
            # replaying stops, counters or entries on that same candle.
            self._queue_kronos_exits(symbol,timeframe,snapshot.candle)
        if self._has_ready_pattern(symbol, timeframe):
            return new_closed_daily, snapshot
        return new_closed_daily, None

    def _analysis_failed(self, snapshot) -> None:
        self._failed_decisions.add(f'{snapshot.symbol}|{snapshot.timeframe}')
        self.stats['snapshot_errors'] += 1
        self.stats['analysis_errors'] = self.stats.get('analysis_errors', 0) + 1

    def _fill_pending_observation(self, symbol: str, timeframe: str, candle) -> None:
        if self._paper is None or not self._paper.assume_session_open:
            return
        for signal,opened,reason in self._paper.fill_pending(
                symbol,timeframe,candle,self._store,recheck=self._laya_check,
                entry_check=self._check_kronos_entry):
            key='trades_opened' if opened else 'signals_rejected'
            self.stats[key]+=1
            self._append_signal_log(signal,status='filled' if opened else 'rejected',reason=reason)

    def _queue_kronos_exits(self, symbol: str, timeframe: str, candle) -> None:
        """Queue one open-position vote per symbol/timeframe/new bar."""
        gate = self._kronos_gate
        if gate is None or gate.exit_mode == "off" or self._paper is None:
            return
        positions = [
            position
            for position in self._paper.positions.get(symbol, [])
            if position.timeframe == timeframe and not position.pending_exit_reason
        ]
        if not positions:
            return
        df = self._store.get_df(symbol, timeframe, min_bars=2)
        gate.prepare_exit(positions, candle, df)

    def _trail_indicators(self, symbol: str, timeframe: str, candle) -> None:
        """Move open indicator stops to each indicator's own line.

        Runs only for an Indicators-only scan, after the bar's exit check, so
        this bar is judged on yesterday's line and tomorrow uses today's.
        Pattern scans never call this.
        """
        if not self._indicators_only or self._paper is None or not self._paper.indicators_only:
            return
        by_name = {pattern.name: pattern for pattern in self._patterns}
        for position in list(self._paper.positions.get(symbol, [])):
            if position.timeframe != timeframe:
                continue
            indicator = by_name.get(position.pattern)
            if indicator is None or not getattr(indicator, "trails", False):
                continue
            self._bind_indicator(indicator, symbol)
            min_bars = int(getattr(indicator, "MIN_BARS", 2) or 2)
            df = self._store.get_df(symbol, timeframe, min_bars=min_bars)
            if df is None:
                continue
            try:
                state = indicator.trail(df)
            except Exception:
                log.exception(
                    f"Scanner | {symbol} {timeframe} {position.pattern} trail failed"
                )
                if getattr(indicator, "error", ""):
                    self.stats["tv_error"] = indicator.error
                continue
            if (
                state is None
                or state[0] != position.action
                or not _protective_stop(state[1], position.action, candle.close)
            ):
                closed = self._paper.exit_indicator(symbol, position, candle)
                if closed is not None:
                    bar_idx = self._paper.bar_count(closed.symbol, closed.timeframe)
                    self._cooldown_tracker[(closed.symbol, closed.pattern)] = (
                        bar_idx, closed.pnl < 0,
                    )
                continue
            self._paper.ratchet_indicator_stop(position, float(state[1]))

    def _has_ready_pattern(self, symbol: str, timeframe: str) -> bool:
        n_bars = self._store.available(symbol, timeframe)
        logged = False
        ready = False
        for pattern in self._patterns:
            if timeframe not in pattern.timeframes:
                continue
            min_bars = max(
                int(getattr(pattern, "MIN_BARS", 2) or 2),
                PATTERN_SCAN_HISTORY_BARS,
            )
            if n_bars < min_bars:
                if (
                    not logged
                    and (symbol, timeframe) not in self._thin_logged
                ):
                    self._thin_logged.add((symbol, timeframe))
                    log.debug(
                        f"Scan | {symbol} {timeframe} has {n_bars} bars "
                        f"(need {min_bars}) — skip patterns this run"
                    )
                    logged = True
                continue
            ready = True
        return ready

    def _v2_before_filter(self) -> bool:
        return (
            getattr(self, "_rule_filter", None) is not None
            and getattr(self, "_rule_filter_mode", "off") == "filter"
            and getattr(self, "_rule_filter_position", "after") == "before"
        )

    def _v2_target(self) -> str:
        return getattr(self, "_rule_filter_pattern", "all") or "all"

    def _v2_gate_before(self, symbol: str, timeframe: str) -> bool:
        """False = a rule pinned to every pattern failed; skip this symbol."""
        if not self._v2_before_filter() or self._v2_target() != "all":
            return True
        return self._rule_prefilter(symbol, timeframe)

    def _v2_apply_before(self, snapshot: MarketSnapshot, hits: list[TradeSignal]) -> list[TradeSignal]:
        """Patternsv2 detects every shape in one pass.

        A rule pinned to one pattern is judged after detection, then only that
        pattern's signals are dropped; the others were never gated by it.
        """
        if not self._v2_before_filter() or self._v2_target() == "all":
            return hits
        if self._rule_prefilter(snapshot.symbol, snapshot.timeframe):
            return hits
        target = self._v2_target()
        return [h for h in hits if getattr(h, "pattern", "") != target]

    def _analyze_local(self, snapshot: MarketSnapshot) -> tuple[int, list[TradeSignal]]:
        """Inline analyze on this loop — tests, workers=1, or pool fallback."""
        if getattr(self, "_patterns_v2", False) and not getattr(self, "_indicators_only", False):
            if not self._v2_gate_before(snapshot.symbol, snapshot.timeframe):
                return 0, []
            n_eval, hits = self._analyze_patterns_v2(snapshot)
            return n_eval, self._v2_apply_before(snapshot, hits)
        symbol, timeframe = snapshot.symbol, snapshot.timeframe
        n_bars = self._store.available(symbol, timeframe)
        n_eval = 0
        hits: list[TradeSignal] = []
        for pattern in self._patterns:
            if timeframe not in pattern.timeframes:
                continue
            min_bars = max(
                int(getattr(pattern, "MIN_BARS", 2) or 2),
                PATTERN_SCAN_HISTORY_BARS,
            )
            if n_bars < min_bars:
                continue
            n_eval += 1
            self._bind_indicator(pattern, symbol)
            try:
                signal = pattern.analyze(snapshot, self._store)
            except Exception:
                log.exception(
                    f"Scan | {pattern.name} {symbol} {timeframe}"
                )
                continue
            if hasattr(pattern, "error"):
                self.stats["tv_error"] = pattern.error or ""
            if signal:
                hits.append(signal)
        return n_eval, hits

    async def _guard_signal(self, signal: TradeSignal, runner, *args) -> None:
        """One bad fill must not abort the scan.

        The bar is marked processed before analyze. An exception here used to
        restart the data sessions, and the replay then skipped that breakout.
        """
        try:
            await runner(*args)
        except Exception as exc:
            log.exception(
                f"Signal | {signal.symbol} {signal.timeframe} "
                f"{signal.pattern} failed — continuing the scan"
            )
            self.stats["signals_rejected"] = self.stats.get("signals_rejected", 0) + 1
            try:
                self._append_signal_log(
                    signal,
                    status="rejected",
                    reason=f"Signal handling failed: {exc}",
                )
            except Exception:
                log.exception(
                    f"Signal log | could not record failed "
                    f"{signal.symbol} {signal.pattern}"
                )

    async def _emit_signals(
        self, snapshot: MarketSnapshot, signals: list[TradeSignal],
    ) -> None:
        by_name = {p.name: p for p in self._patterns}
        for signal in signals:
            self.stats["patterns_found"] += 1
            try:
                self._record_detection(signal)
            except Exception:
                log.exception(
                    f"Signal | could not record detection "
                    f"{signal.symbol} {signal.pattern}"
                )
            await self._guard_signal(
                signal,
                self._process_signal,
                signal,
                by_name.get(signal.pattern),
                snapshot.candle,
            )

    async def _run_analyzes(self, snapshots: list[MarketSnapshot]) -> None:
        """Run pattern.analyze() off-loop when a spawn pool is open."""
        if not snapshots:
            return
        if getattr(self, "_patterns_v2", False):
            await self._run_analyzes_v2(snapshots)
            return
        if self._analyze_pool is None:
            for snapshot in snapshots:
                n_eval, hits = self._analyze_local(snapshot)
                self.stats["pattern_evaluations"] += n_eval
                await self._emit_signals(snapshot, hits)
            return

        from core.pattern_jobs import analyze_batch

        jobs = [
            (snapshot, self._store.copy_candles(snapshot.symbol, snapshot.timeframe))
            for snapshot in snapshots
        ]
        n = max(1, int(self._analyze_workers) or 1)
        chunk_size = max(4, min(16, (len(jobs) + n - 1) // n))
        chunks = [jobs[i:i + chunk_size] for i in range(0, len(jobs), chunk_size)]
        loop = asyncio.get_running_loop()
        try:
            parts = await asyncio.gather(*[
                loop.run_in_executor(self._analyze_pool, analyze_batch, chunk)
                for chunk in chunks
            ])
        except Exception:
            log.exception("Scan | analyze pool failed — inline this batch")
            for snapshot in snapshots:
                n_eval, hits = self._analyze_local(snapshot)
                self.stats["pattern_evaluations"] += n_eval
                await self._emit_signals(snapshot, hits)
            return
        flat: list[tuple[int, list[TradeSignal]]] = []
        for part in parts:
            flat.extend(part)
        for snapshot, (n_eval, signals) in zip(snapshots, flat):
            self.stats["pattern_evaluations"] += n_eval
            await self._emit_signals(snapshot, signals)

    async def _run_analyzes_v2(self, snapshots: list[MarketSnapshot]) -> None:
        """Batch Patternsv2 detection across the spawn pool.

        Numeric arrays go out, raw detections come back. Signal conversion,
        rule gates, the ledger, and replay advancement stay on this loop.
        """
        from patterns_v2.signals import MIN_BARS, detect_raw

        pending: list[tuple[MarketSnapshot, tuple]] = []
        for snapshot in snapshots:
            if not self._v2_gate_before(snapshot.symbol, snapshot.timeframe):
                continue
            arrays = self._store.detect_arrays(
                snapshot.symbol, snapshot.timeframe, min_bars=MIN_BARS,
            )
            if arrays is not None:
                pending.append((snapshot, arrays))
        if not pending:
            return

        async def _emit(snapshot: MarketSnapshot, found: list[dict]) -> None:
            n_eval, hits = self._v2_signals(snapshot, found)
            self.stats["pattern_evaluations"] += n_eval
            await self._emit_signals(snapshot, self._v2_apply_before(snapshot, hits))

        if self._analyze_pool is None:
            for snapshot, arrays in pending:
                try:
                    found = detect_raw(*arrays, latest_bar_only=True)
                except Exception:
                    log.exception(
                        f"Scan | patternsv2 {snapshot.symbol} {snapshot.timeframe}"
                    )
                    self._analysis_failed(snapshot)
                    self.stats["pattern_evaluations"] += 1
                    continue
                await _emit(snapshot, found)
            return

        from core.pattern_jobs import detect_v2_batch

        jobs = [(s.symbol, s.timeframe, *arrays) for s, arrays in pending]
        n = max(1, int(self._analyze_workers) or 1)
        chunk_size = max(4, min(16, (len(jobs) + n - 1) // n))
        chunks = [jobs[i:i + chunk_size] for i in range(0, len(jobs), chunk_size)]
        loop = asyncio.get_running_loop()
        try:
            parts = await asyncio.gather(*[
                loop.run_in_executor(self._analyze_pool, detect_v2_batch, chunk)
                for chunk in chunks
            ])
        except Exception:
            log.exception("Scan | patternsv2 pool failed — inline this batch")
            for snapshot, arrays in pending:
                try:
                    found = detect_raw(*arrays, latest_bar_only=True)
                except Exception:
                    log.exception(
                        f"Scan | patternsv2 {snapshot.symbol} {snapshot.timeframe}"
                    )
                    self._analysis_failed(snapshot)
                    self.stats["pattern_evaluations"] += 1
                    continue
                await _emit(snapshot, found)
            return
        flat = [found for part in parts for found in part]
        for (snapshot, _arrays), found in zip(pending, flat):
            await _emit(snapshot, found)


    # ── Signal pipeline ────────────────────────────────────────────────────────
    def _apply_fixed_stop(self, signal: TradeSignal, candle) -> None:
        """Apply the legacy override; V2 owns its structural/ATR risk ladder."""
        if signal.provenance == "patternsv2":
            return
        pct = float(settings.stop_loss_pct or 0.0)
        if pct <= 0 or signal.action not in ("BUY", "SELL"):
            return
        reference = getattr(signal, "price", None)
        try:
            ref = float(reference)
        except (TypeError, ValueError):
            ref = 0.0
        if not math.isfinite(ref) or ref <= 0:
            try:
                ref = float(getattr(candle, "close", 0.0))
            except (TypeError, ValueError):
                ref = 0.0
        if not math.isfinite(ref) or ref <= 0:
            return
        stop = ref * (1 - pct if signal.action == "BUY" else 1 + pct)
        signal.stop_loss = round(stop, 4)
        signal.stop_loss_pct_cap = pct
        # The pattern's trail was sized from its ATR stop, which no longer
        # matches the executed stop. Re-express it in R of the fixed stop so
        # the trail actually arms (opening_candidate refines it again from the
        # effective risk after any opening gap).
        trail_r = float(settings.stop_loss_trail_r or 0.0)
        if trail_r > 0:
            signal.trailing_stop_pct = round(trail_r * pct, 6)
            signal.trailing_stop_basis = "entry"
            signal.trailing_activation_pct = round(
                float(settings.stop_loss_arm_r or 0.0) * pct, 6)
            if signal.trailing_stop_mode is None:
                signal.trailing_stop_mode = (
                    "highest_close" if signal.action == "BUY" else "lowest_close")

    async def _process_signal(
        self, signal: TradeSignal, pattern: BasePattern | None = None, candle=None,
    ) -> None:
        log.info(
            f"Signal | {signal.symbol} {signal.timeframe} | "
            f"{signal.action} | pattern={signal.pattern} | "
            f"confidence={signal.confidence:.2f}"
        )
        # Stamp the signal event before any gate can reject it, so the Logs
        # tab can correlate both accepted and rejected decisions to the
        # simulated market bar during replay.
        if candle is not None:
            signal.signal_bar_idx = (
                self._paper.bar_count(signal.symbol, signal.timeframe)
                if self._paper is not None else None
            )
            signal.signal_bar_timestamp = candle.timestamp

        # V2 already supplies a causal volatility cap and its own trail.
        self._apply_fixed_stop(signal, candle)
        if signal.action in ("BUY", "SELL") and not signal.requested_rules:
            from core.pattern_provenance import rules

            # Save the decision even if a gate rejects it. The previous export
            # lost every short's risk geometry before the short/DII gates.
            signal.requested_rules = rules(signal, entry_mode="decision-close")

        from core.entry_policy import describe_short_entry_rejection

        reason = describe_short_entry_rejection(
            signal.action, indicators_only=self._indicators_only,
        )
        if reason is not None:
            log.info(f"Signal REJECTED by short gate — {signal.symbol} {signal.pattern}")
            self.stats["signals_rejected"] += 1
            self.stats["short_rejected"] = self.stats.get("short_rejected", 0) + 1
            self._append_signal_log(signal, status="rejected", reason=reason)
            return

        if signal.action in ("BUY", "SELL"):
            from core.execution import validate_entry_geometry

            try:
                validate_entry_geometry(signal, signal.price)
            except ValueError as exc:
                self.stats["signals_rejected"] += 1
                self.stats["geometry_rejected"] = self.stats.get("geometry_rejected", 0) + 1
                self._append_signal_log(signal, status="rejected", reason=str(exc))
                return

        # Optional US price floor. Off by default: the sub-$10 cohort in the
        # 2026-11 export was +$1,691 and held both winners.
        price_floor = float(settings.min_signal_price_usd or 0.0)
        if (
            not self._indicators_only
            and self._market == "us"
            and signal.action in ("BUY", "SELL")
            and price_floor > 0
            and 0 < float(signal.price or 0) < price_floor
        ):
            reason = (
                f"Signal price {float(signal.price):.2f} is below the "
                f"{price_floor:.2f} minimum (MIN_SIGNAL_PRICE_USD)."
            )
            log.info(f"Signal REJECTED by price floor — {signal.symbol} {signal.pattern}")
            self.stats["signals_rejected"] += 1
            self.stats["price_floor_rejected"] = (
                self.stats.get("price_floor_rejected", 0) + 1)
            self._append_signal_log(signal, status="rejected", reason=reason)
            return

        # Paper records the DII vote by default so unvalidated narrow bands
        # cannot starve both sides. Explicit filter mode retains the veto.
        if not self._indicators_only and signal.action in ("BUY", "SELL"):
            from analysis.pattern_dii import MIN_BARS, describe_pattern_dii_rejection

            df = self._store.get_df(signal.symbol, signal.timeframe, min_bars=MIN_BARS)
            dii_reason = describe_pattern_dii_rejection(df, action=signal.action)
            if dii_reason is not None:
                if settings.dii_gate_shadow or (
                    self._paper is not None and settings.paper_dii_gate_mode == "shadow"
                ):
                    # A/B mode: record what the gate would have done but let
                    # the entry continue, so both books can be compared.
                    log.info(
                        f"Signal DII gate SHADOW — {signal.symbol} "
                        f"{signal.pattern} | {dii_reason}"
                    )
                    self.stats["dii_shadow"] = self.stats.get("dii_shadow", 0) + 1
                    self._append_signal_log(
                        signal, status="shadow", reason=dii_reason)
                else:
                    log.info(
                        f"Signal REJECTED by DII gate — {signal.symbol} "
                        f"{signal.pattern} | {dii_reason}"
                    )
                    self.stats["signals_rejected"] += 1
                    self.stats["dii_rejected"] = self.stats.get("dii_rejected", 0) + 1
                    self._append_signal_log(signal, status="rejected", reason=dii_reason)
                    return

        reason = signal.blocked_reason or signal.filtered_reason
        if reason:
            self.stats["signals_rejected"] += 1
            self._append_signal_log(signal, status="rejected", reason=reason)
            return

        # Post-loss cooldown — enforced regardless of Pattern-only. Pattern-only
        # isolates raw pattern edge from *signal-quality* filters below, but the
        # cooldown is a basic account-risk control, not a pattern-quality
        # opinion: skipping it allowed same-session re-entry into a name that
        # had just stopped out. It still applies when Pattern-only is checked.
        bar_idx = (
            self._paper.bar_count(signal.symbol, signal.timeframe)
            if self._paper is not None else 0
        )
        cooldown_reason = describe_cooldown_rejection(
            signal, bar_idx, self._cooldown_tracker,
        )
        if cooldown_reason is not None:
            log.info(
                f"Signal REJECTED by cooldown — {signal.symbol} "
                f"{signal.pattern} | {cooldown_reason}"
            )
            self.stats["signals_rejected"] += 1
            self._append_signal_log(signal, status="rejected", reason=cooldown_reason)
            return

        # Step 0 — Same entry gates the backtester applies before volume
        # (min_confidence + SMA200 regime). Without these, paper/live took
        # trades the "validated" backtest would have skipped. Pattern-only
        # skips confidence for all patterns, but the regime gate stays on
        # for REGIME_REQUIRED_PATTERNS (006/007) even in Pattern-only — see
        # regime_filter_required() / REGIME_REQUIRED_PATTERNS.
        if structure_filters_enabled(self._pattern_only):
            if not passes_min_confidence(signal):
                reason = describe_confidence_rejection(signal)
                log.info(
                    f"Signal REJECTED by confidence — {signal.symbol} "
                    f"{signal.pattern} | {reason}"
                )
                self.stats["signals_rejected"] += 1
                self._append_signal_log(signal, status="rejected", reason=reason)
                return

        if regime_filter_required(signal.pattern, self._pattern_only):
            regime_reason = describe_regime_rejection(
                signal, self._store, market=self._market,
            )
            if regime_reason is not None:
                log.info(
                    f"Signal REJECTED by regime filter — {signal.symbol} "
                    f"{signal.pattern} | {regime_reason}"
                )
                self.stats["signals_rejected"] += 1
                self._append_signal_log(signal, status="rejected", reason=regime_reason)
                return

        if structure_filters_enabled(self._pattern_only):
            profile = get_market(self._market)
            if profile.long_only and signal.action == "SELL":
                reason = (
                    f"Long-only {profile.label}: pattern SELL/short is disabled "
                    f"(PSE retail shorts need SBL)."
                )
                log.info(
                    f"Signal REJECTED by long-only — {signal.symbol} "
                    f"{signal.pattern} | {reason}"
                )
                self.stats["signals_rejected"] += 1
                self._append_signal_log(signal, status="rejected", reason=reason)
                return

        risk_reason = describe_risk_gate_rejection(
            signal, self._store, signal.symbol, signal.timeframe,
            **risk_gate_kwargs(),
        )
        if risk_reason is not None:
            log.info(
                f"Signal REJECTED by risk gates — {signal.symbol} "
                f"{signal.pattern} | {risk_reason}"
            )
            self.stats["signals_rejected"] += 1
            self._append_signal_log(signal, status="rejected", reason=risk_reason)
            return

        # Collect-first: don't finish yet. Defer volume/vision until the
        # full scan has drained, then rank the pool by R:R and finish only the
        # top-N.
        if self._collect_first:
            self._collect_pool.append((signal, pattern, candle))
            return

        await self._finish_signal(signal, pattern, candle)


    async def _flush_collect_first(self) -> None:
        """Rank the collected chart-pattern signals by R:R; finish the top-N."""
        if not self._collect_first or not self._collect_pool:
            return
        pool = self._collect_pool
        self._collect_pool = []

        from core.execution import rank_candidates
        selected, rejected = rank_candidates(pool, self._collect_first_top_n)
        ranked = selected + rejected

        self.stats["collect_first_ranked"] = self.stats.get("collect_first_ranked", 0) + len(ranked)
        self.stats["collect_first_selected"] = self.stats.get("collect_first_selected", 0) + len(selected)

        log.info(
            f"Collect-first | {len(ranked)} ranked by R:R, "
            f"selected top {len(selected)} of {self._collect_first_top_n}"
        )

        for signal, _pattern, _candle in rejected:
            self.stats["signals_rejected"] += 1
            self._append_signal_log(
                signal,
                status="rejected",
                reason=(
                    f"Collect-first: R:R ranked below top "
                    f"{self._collect_first_top_n} this scan."
                ),
            )

        for signal, pattern, candle in selected:
            rr = signal_reward_risk(signal)
            rr_txt = f"{rr:.2f}" if rr is not None else "—"
            log.info(
                f"Collect-first | selected {signal.symbol} {signal.pattern} "
                f"R:R={rr_txt}"
            )
            await self._guard_signal(
                signal, self._finish_signal, signal, pattern, candle,
            )


    async def _finish_signal(
        self, signal: TradeSignal, pattern: BasePattern | None = None, candle=None,
    ) -> None:
        if self._volume_gate:
            vgate = volume_confirm_gate(signal, self._store,
                                       rvol_min=self._volume_gate_rvol_min, obv_bars=self._volume_gate_obv_bars)
            if not vgate.passed:
                reason = (
                    f"Volume confirm gate vetoed this {signal.action}: {vgate.reason}. "
                    f"Needs relative volume ≥ VOLUME_GATE_RVOL_MIN and OBV slope "
                    f"agreeing with the trade direction."
                )
                log.info(
                    f"Volume gate REJECT | {signal.symbol} {signal.pattern} | "
                    f"{vgate.reason}"
                )
                self.stats["signals_rejected"] += 1
                self.stats["volume_gate_rejected"] += 1
                self._append_signal_log(signal, status="rejected", reason=reason)
                return
            log.info(
                f"Volume gate PASS | {signal.symbol} {signal.pattern} | "
                f"{vgate.reason}"
            )

        # Visual rule filter (Builder tab): supplies the context-before-the-
        # pattern gate that the deleted regime layer used to provide. Shadow
        # only records evidence; filter vetoes entries.
        if (self._rule_filter is not None
                and self._rule_filter_position == "after"
                and signal.provenance != "visual_rule"
                and self._rule_filter_matches_pattern(signal.pattern)
                and signal.timeframe == self._rule_filter.timeframe):
            evidence = self._rule_filter_evidence(signal.symbol, signal.timeframe)
            if self._rule_filter_mode == "shadow":
                signal.rule_evidence = evidence
                self.stats["rule_filter_shadow"] = (
                    self.stats.get("rule_filter_shadow", 0) + 1)
            elif not evidence.get("matched"):
                failed = [
                    f"{item['label']} (got {item.get('left')})"
                    for item in evidence.get("conditions", [])
                    if not item.get("pass")
                ] or [evidence.get("reason") or "rule not satisfied"]
                reason = "Rule filter: " + "; ".join(failed)
                log.info(
                    f"Rule filter REJECT | {signal.symbol} {signal.pattern} | {reason}"
                )
                self.stats["signals_rejected"] += 1
                self.stats["rule_filter_rejected"] = (
                    self.stats.get("rule_filter_rejected", 0) + 1)
                self._append_signal_log(signal, status="rejected", reason=reason)
                return

        # Step 1 — Vision confirmation, only when enabled. The pattern's own
        # min_confidence already gated this signal before analyze() returned
        # it — vision_min_indicator_confidence exists only to decide whether
        # a signal is worth spending a vision check on, not as a second
        # trade-approval gate when vision is off.
        if settings.vision_confirmation_enabled:
            if signal.confidence < settings.vision_min_indicator_confidence:
                reason = (
                    f"Vision gate skipped the trade: confidence {signal.confidence:.2f} "
                    f"is below VISION_MIN_INDICATOR_CONFIDENCE "
                    f"({settings.vision_min_indicator_confidence:.2f}), so no Claude "
                    f"check was run and the signal was dropped."
                )
                log.info(
                    f"Signal confidence {signal.confidence:.2f} below threshold "
                    f"{settings.vision_min_indicator_confidence} — skipping vision, skipping trade"
                )
                self.stats["signals_rejected"] += 1
                self._append_signal_log(signal, status="rejected", reason=reason)
                return
            if pattern is None:
                reason = "Vision confirmation requires a pattern instance — none provided."
                self.stats["signals_rejected"] += 1
                self._append_signal_log(signal, status="rejected", reason=reason)
                return
            verdict = await self._run_vision_check(signal, pattern)
            if verdict != VisionVerdict.CONFIRM:
                reason = (
                    f"Vision confirmation failed with verdict {verdict} — Claude did "
                    f"not CONFIRM the {signal.pattern} chart setup on {signal.symbol}."
                )
                log.info(
                    f"Signal REJECTED by vision check "
                    f"({verdict}) — {signal.symbol} {signal.pattern}"
                )
                self.stats["signals_rejected"] += 1
                self._append_signal_log(signal, status="rejected", reason=reason)
                return

        # Laya paper check (docs/laya.md section 10). Runs after the existing
        # volume/vision requirements and before the paper entry call, scoped
        # explicitly to paper execution. Shadow snapshots the immutable
        # candidate context immediately before the entry attempt and enqueues
        # without awaiting inference (no latency/fill change). Filter rechecks
        # book liveness, eligibility, and deadline before entry. Mandatory
        # position management and CLOSE/exit handling continue on model failure.
        if self._laya_mode in ("shadow", "filter") and self._paper is not None and candle is not None:
            verdict = self._laya_check(signal, candle,
                                       closes=_tail_column(self._store, signal, "close"),
                                       volumes=_tail_column(self._store, signal, "volume"))
            # Stamp the verdict on the signal: _open_trade copies it onto the
            # trade, so fills trace back to their assessment in Paper/export.
            signal.laya_assessment = {
                "assessment_id": verdict.get("assessment_id"),
                "policy_id": self._laya_policy_id or "laya-event-v1-default",
                "mode": self._laya_mode,
                "outcome": verdict.get("outcome", "abstain"),
            }
            if self._laya_mode == "shadow":
                # Shadow: record only, including on errors. Never affects fills.
                self._laya_shadow.append(verdict)
                self.stats["laya_shadow"] = self.stats.get("laya_shadow", 0) + 1
            else:
                outcome = verdict.get("outcome", "abstain")
                if outcome != "allow":
                    reason = (
                        f"Laya filter {outcome}: "
                        + "; ".join(verdict.get("reasons") or ["no reason"])
                        + f" (assessment {verdict.get('assessment_id') or 'missing'})"
                    )
                    log.info(f"Laya FILTER | {signal.symbol} {signal.pattern} | {reason}")
                    self.stats["signals_rejected"] += 1
                    self.stats["laya_filtered"] = self.stats.get("laya_filtered", 0) + 1
                    self._append_signal_log(signal, status="rejected", reason=reason)
                    return

        # Kronos 1-day confirmation (docs/kronos-3-preds.md): every surviving
        # entry waits for the end-of-scan batch so one worker call covers all
        # pending symbols. Shadow stamps evidence and queues anyway; confirm
        # queues only when the vote agrees.
        if (
            self._kronos_gate is not None
            and self._kronos_mode != "off"
            and signal.action in ("BUY", "SELL")
        ):
            df = self._store.get_df(signal.symbol, signal.timeframe, min_bars=2)
            self._kronos_gate.queue(signal, candle, df)
            return

        await self._queue_entry(signal, candle)

    async def _queue_entry(self, signal: TradeSignal, candle) -> None:
        # Completed-bar decisions queue for retrospective next-open simulation.
        if self._paper is not None and candle is not None:
            if not self._paper.assume_session_open:
                self._append_signal_log(signal, status="rejected", reason=(
                    "Closed-bar feed has no executable live quote; enable historical replay "
                    "for retrospective next-open simulation."))
                self.stats["signals_rejected"] += 1
                return
            opened, fill_reason = self._paper.queue_position(
                signal, candle, self._store,
            )
            if opened:
                self.stats["orders_queued"] = self.stats.get("orders_queued", 0) + 1
                self._append_signal_log(
                    signal, status="queued", reason=fill_reason,
                )
            else:
                self.stats["signals_rejected"] += 1
                self._append_signal_log(
                    signal, status="rejected", reason=fill_reason,
                )
        else:
            log.info(
                f"Signal APPROVED (IBKR disabled) — would {signal.action} "
                f"{signal.qty} {signal.symbol} @ ~{signal.price:.2f}"
            )
            self._append_signal_log(
                signal,
                status="accepted",
                reason=(
                    f"Cleared entry gates; IBKR execution disabled so this would "
                    f"{signal.action} {signal.qty:g} {signal.symbol} @ ~{signal.price:.2f} "
                    f"but no order was sent."
                ),
            )
        # if signal.action == "BUY":
        #     self._orders.place_market_order(
        #         signal.symbol, "BUY", signal.qty, signal.pattern
        #     )
        # elif signal.action == "SELL":
        #     self._orders.place_market_order(
        #         signal.symbol, "SELL", signal.qty, signal.pattern
        #     )
        # elif signal.action == "CLOSE":
        #     self._orders.close_position(signal.symbol, signal.qty, signal.pattern)

    def _check_kronos_entry(self, signal: TradeSignal) -> None:
        """Honor this run's confirm mode at fill, using the saved decision only.

        Forecasting from the fill session's completed candle would look ahead.
        A queue left by an off/shadow run cannot silently bypass confirmation.
        """
        if self._kronos_mode != "confirm":
            return
        from core.kronos_confirm import _anchor_date, evaluate, summarize
        evidence = signal.kronos_evidence or {}
        gate = self._kronos_gate
        if not (
            gate is not None
            and evidence.get("schema_version") == 1
            and evidence.get("decision") == "confirm" and evidence.get("mode") == "confirm"
            and evidence.get("symbol") == signal.symbol
            and evidence.get("action") == signal.action
            and evidence.get("timeframe") == signal.timeframe
            and evidence.get("market") == self._market
            and evidence.get("anchor_date") == _anchor_date(None, signal, self._market)
            and evidence.get("model") == gate.model and evidence.get("samples") == gate.samples
            and evidence.get("forecast_horizon") == "1d"
        ):
            raise ValueError("Kronos confirmation missing or mismatched for this queued entry")
        try:
            returns = evidence["returns"]
            valid = (isinstance(returns, (list, tuple)) and len(returns) <= gate.samples
                     and all(math.isfinite(float(v)) for v in returns)
                     and math.isfinite(float(evidence["anchor_close"])) and evidence["anchor_close"] > 0
                     and math.isfinite(float(evidence["band_pct"])) and evidence["band_pct"] > 0)
            verdict = evaluate(summarize(evidence["anchor_close"], evidence["band_pct"], returns),
                               signal.action, min_agree=gate.min_agree)
        except (KeyError, TypeError, ValueError):
            valid, verdict = False, None
        if not valid or verdict.decision != "confirm":
            raise ValueError("Kronos saved forecasts do not confirm this queued entry")
        from core.entry_economics import net_direction_return
        price = float(signal.price)
        book = self._paper
        fee = book.txn_cost_pct if book is not None else get_market(self._market).txn_cost_pct
        slip = book.slippage_pct if book is not None else 0.0
        rebased = [net_direction_return(
            price, evidence['anchor_close'] * (1 + float(value)), signal.action, fee, slip,
        ) for value in returns]
        opening_vote = evaluate(summarize(price, evidence['band_pct'], rebased),
                                signal.action, min_agree=gate.min_agree)
        if opening_vote.decision != 'confirm':
            raise ValueError('Kronos saved forecasts do not confirm net return from the execution price')
        # Keep the decision evidence immutable and add the executable vote.
        signal.kronos_evidence = {**evidence, 'opening_check': {
            'entry_price': price, 'fee_per_side': fee, 'slippage_pct': slip,
            'net_direction_returns': rebased, 'decision': opening_vote.decision,
        }}

    async def _flush_kronos_confirm(self) -> None:
        """Resolve pending entry and open-position votes in one worker call."""
        gate = self._kronos_gate
        if gate is None:
            return
        entry_rows, exit_rows = await asyncio.to_thread(gate.flush_with_exits)
        for signal, candle, verdict, evidence in entry_rows:
            signal.kronos_evidence = evidence
            if self._defer_replay_kronos(signal,evidence):
                self._append_signal_log(signal,status='deferred',reason=evidence['reason'])
                continue
            if gate.mode == "shadow" or verdict.decision == "confirm":
                await self._guard_signal(
                    signal, self._queue_entry, signal, candle,
                )
                continue
            reason = f"Kronos {verdict.decision}: {verdict.reason}"
            log.info(
                f"Kronos REJECT | {signal.symbol} {signal.pattern} | {reason}"
            )
            self.stats["signals_rejected"] += 1
            self.stats["kronos_rejected"] = self.stats.get("kronos_rejected", 0) + 1
            self._append_signal_log(signal, status="rejected", reason=reason)
        for position, verdict, evidence in exit_rows:
            if self._defer_replay_kronos(position,evidence):
                position.kronos_exit_evidence=evidence
                continue
            self._handle_kronos_exit(position, verdict, evidence)

    def _defer_replay_kronos(self, value, evidence: dict) -> bool:
        kind=evidence.get('failure_kind')
        if (self._paper is None or not getattr(self._paper,'assume_session_open',False)
                or kind not in ('capacity','infrastructure_or_context')):
            return False
        if ((evidence.get('scope')=='entry' and self._kronos_mode!='confirm')
                or (evidence.get('scope')=='position' and self._kronos_exit_mode!='exit')):
            return False
        self._failed_decisions.add(f'{value.symbol}|{value.timeframe}')
        self.stats['snapshot_errors']+=1
        self.stats['kronos_deferred']=self.stats.get('kronos_deferred',0)+1
        evidence['replay_action']='retry_same_session'
        if kind!='capacity':
            key=(value.symbol,value.timeframe,evidence.get('anchor_date'))
            count=self._kronos_retry_tries.get(key,0)
            if key not in self._kronos_failure_seen:
                count+=1
                self._kronos_failure_seen.add(key)
            self._kronos_retry_tries[key]=count
            if count>=3:
                self._running=False
                self.stats['termination_reason']='kronos_unavailable'
                evidence['replay_action']='stopped_incomplete'
        return True

    def _handle_kronos_exit(self, position, verdict, evidence: dict) -> None:
        """Apply one open-position vote: record it, and exit on `oppose`."""
        gate = self._kronos_gate
        if gate is None:
            return
        position.kronos_exit_evidence = evidence
        if gate.exit_mode == "shadow":
            self.stats["kronos_exit_shadow"] = (
                self.stats.get("kronos_exit_shadow", 0) + 1
            )
            log.info(
                f"Kronos EXIT-SHADOW | {position.symbol} {position.pattern} | "
                f"{verdict.decision}: {verdict.reason}"
            )
            return
        if gate.exit_mode != "exit" or verdict.decision != "oppose":
            return
        if getattr(position, "pending_exit_reason", ""):
            evidence["exit_action"] = "exit_already_pending"
            return
        # A one-day forecast is noisy relative to a swing trade's daily range.
        # Defer inside a noise band around entry on either side. Measuring
        # only losses used to defer every profitable position, even one far
        # outside the band, until its gain had become a loss.
        min_atr = float(settings.kronos_exit_min_adverse_atr or 0.0)
        anchor = float((evidence or {}).get("anchor_close") or 0.0)
        atr_pct = float((evidence or {}).get("atr_pct") or 0.0)
        entry = float(getattr(position, "entry_price", 0.0) or 0.0)
        if min_atr > 0 and anchor > 0 and atr_pct > 0 and entry > 0:
            adverse = (
                (anchor - entry) if position.action == "SELL" else (entry - anchor)
            ) / entry
            # ATR% is measured at the forecast anchor; express that dollar
            # range as a fraction of ENTRY before comparing displacement.
            floor = min_atr * (atr_pct / 100.0) * anchor / entry
            displacement = abs(adverse)
            evidence["entry_displacement_pct"] = displacement * 100
            evidence["noise_band_pct"] = floor * 100
            if displacement < floor:
                evidence["exit_action"] = "deferred_noise_band"
                self.stats["kronos_exit_deferred"] = (
                    self.stats.get("kronos_exit_deferred", 0) + 1
                )
                log.info(
                    f"Kronos EXIT deferred | {position.symbol} {position.pattern} | "
                    f"{displacement:.2%} move < {floor:.2%} ({min_atr:g} ATR) threshold"
                )
                return
        if self._paper is None or not self._paper.request_exit(position, "kronos_exit"):
            evidence["exit_action"] = "position_not_open"
            log.warning(
                f"Kronos EXIT skipped | {position.symbol} {position.pattern} "
                f"(position no longer open)"
            )
            return
        evidence["exit_action"] = "queued_next_open"
        self.stats["kronos_exits"] = self.stats.get("kronos_exits", 0) + 1
        log.info(
            f"Kronos EXIT | {position.symbol} {position.pattern} | "
            f"{verdict.reason} — queued for the next open"
        )


    def ohlcv_frame(self, symbol: str, timeframe: str, min_bars: int = 2):
        """OHLCV for on-demand chart rendering (open/closed row click)."""
        return self._store.get_df(symbol, timeframe, min_bars=min_bars)

    def _laya_check(self, signal: TradeSignal, candle,
                    *, closes=None, volumes=None) -> dict:
        """Synchronous deterministic policy gate for paper entries.

        Captures the immutable candidate context after deterministic
        eligibility checks. Without a configured assessor, shadow records an
        operational note (drops the assessment, fills unchanged) while filter
        must not execute an unaudited entry (abstain → skip). Inference
        timeout/malformed/stale/missing are abstentions, not positive votes.
        """
        ts = getattr(candle, "timestamp", None)
        snapshot = {
            "symbol": signal.symbol,
            "timeframe": signal.timeframe,
            "market": self._market,
            "pattern": signal.pattern,
            "pattern_version_id": getattr(signal, "pattern_version_id", None),
            "action": signal.action,
            "price": signal.price,
            "stop": signal.stop_loss,
            "target": signal.take_profit,
            "confidence": signal.confidence,
            "bar_close": getattr(candle, "close", None),
            "bar_ts": ts.isoformat() if ts is not None else None,
            "closes": list(closes) if closes else None,
            "volumes": list(volumes) if volumes else None,
            "mode": self._laya_mode,
            "policy_id": self._laya_policy_id or "laya-candidate-v1-default",
        }
        if self._laya_assess is None:
            if self._laya_mode == "shadow":
                return {"outcome": "allow", "reasons": ["shadow: no assessor; baseline fill"],
                        "assessment_id": None, "snapshot": snapshot}
            return {"outcome": "abstain", "reasons": ["filter: missing Laya assessment"],
                    "assessment_id": None, "snapshot": snapshot}
        try:
            verdict = self._laya_assess(deepcopy(snapshot))
        except Exception as exc:  # noqa: BLE001 - model failure never blocks exits
            log.warning(f"Laya | assessor failed for {signal.symbol}: {exc}")
            if self._laya_mode == "shadow":
                return {"outcome": "allow", "reasons": [f"shadow assessor error: {exc}"],
                        "assessment_id": None, "snapshot": snapshot}
            return {"outcome": "abstain", "reasons": [f"assessor error: {exc}"],
                    "assessment_id": None, "snapshot": snapshot}
        if not isinstance(verdict, dict) or verdict.get("outcome") not in ("allow", "veto", "abstain"):
            if self._laya_mode == "shadow":
                return {"outcome": "allow", "reasons": ["shadow: malformed verdict; baseline fill"],
                        "assessment_id": None, "snapshot": snapshot}
            return {"outcome": "abstain", "reasons": ["malformed Laya verdict"],
                    "assessment_id": None, "snapshot": snapshot}
        verdict.setdefault("snapshot", snapshot)
        return verdict

    async def _run_vision_check(
        self, signal: TradeSignal, pattern: BasePattern
    ) -> VisionVerdict:
        df = self._store.get_df(signal.symbol, signal.timeframe, min_bars=2)
        if df is None:
            log.warning("Vision | No OHLCV data in store — skipping visual check")
            return VisionVerdict.UNCERTAIN

        chart_png = self._renderer.render(
            signal.symbol, signal.timeframe, df,
            annotations=signal.chart_annotations or None,
        )
        return self._vision.check(
            chart_png=chart_png,
            pattern_name=pattern.name,
            pattern_description=pattern.chart_description,
            symbol=signal.symbol,
            action=signal.action,
        )

    # ── Pattern discovery ──────────────────────────────────────────────────────
    _DETECTED_TABLE_HEADER = (
        "| timestamp | pattern | file | symbol | timeframe | action | confidence |\n"
        "|---|---|---|---|---|---|---|\n"
    )

    def _init_patterns_detected_file(self) -> None:
        PATTERNS_DETECTED_FILE.write_text(
            "# Pattern detections\n\n" + self._DETECTED_TABLE_HEADER,
            encoding="utf-8",
        )

    def _record_detection(self, signal: TradeSignal) -> None:
        if signal.pattern in EXCLUDED_PATTERNS:
            return
        filename = self._pattern_files.get(signal.pattern)
        if not filename:
            filename = "patternsv2" if getattr(self, "_patterns_v2", False) else "?"
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        row = (
            f"| {ts} | `{signal.pattern}` | {filename} | "
            f"**{signal.symbol}** | {signal.timeframe} | "
            f"{signal.action} | {signal.confidence:.2f} |\n"
        )
        with PATTERNS_DETECTED_FILE.open("a", encoding="utf-8") as f:
            f.write(row)

    def _analyze_patterns_v2(self, snapshot: MarketSnapshot) -> tuple[int, list[TradeSignal]]:
        """Breakouts from technical-pattern-detector on the current bar only."""
        from patterns_v2.signals import MIN_BARS, detect_raw

        symbol, timeframe = snapshot.symbol, snapshot.timeframe
        arrays = self._store.detect_arrays(symbol, timeframe, min_bars=MIN_BARS)
        if arrays is None:
            return 0, []
        try:
            found = detect_raw(*arrays, latest_bar_only=True)
        except Exception:
            log.exception(f"Scan | patternsv2 {symbol} {timeframe}")
            return 1, []
        return self._v2_signals(snapshot, found)

    def _v2_signals(
        self, snapshot: MarketSnapshot, found: list[dict],
    ) -> tuple[int, list[TradeSignal]]:
        """Turn raw detections into signals; build the frame only for a hit."""
        if not found:
            return 1, []
        from patterns_v2.signals import MIN_BARS, signals_from_raw

        symbol, timeframe = snapshot.symbol, snapshot.timeframe
        df = self._store.get_df(symbol, timeframe, min_bars=MIN_BARS)
        if df is None:
            return 1, []
        return 1, signals_from_raw(
            df, found, symbol=symbol, timeframe=timeframe, latest_bar_only=True,
        )

    def _bind_indicator(self, indicator, symbol: str) -> None:
        bind = getattr(indicator, "bind", None)
        if bind is None:
            return
        exchange = "NASDAQ"
        finder = getattr(self._tv, "_exchange_for", None)
        if finder is not None:
            exchange = finder(symbol)
        bind(symbol, getattr(indicator, "timeframes", ["1d"])[0], exchange)

    def _discover_patterns(self) -> None:
        if getattr(self, "_rule_id", ""):
            from analysis.rule_detector import VisualRuleIndicator
            from core.rules_store import open_rules_store

            self._version_set = {}
            self._indicators_only = True
            if self._paper is not None:
                self._paper.indicators_only = True
            row = open_rules_store().get_rule(self._rule_id)
            if row is None:
                self._patterns = []
                self._pattern_files = {}
                self.stats["rule_error"] = "Unknown indicator rule."
                return
            self._patterns = [VisualRuleIndicator(row)]
            self._pattern_files = {row["name"]: "visual_rule"}
            return
        if getattr(self, "_tv_stack_id", ""):
            from data.tv_stacks import TvStackIndicator, get_stack

            self._version_set = {}
            stack = get_stack(self._tv_stack_id)
            self._indicators_only = True
            if self._paper is not None:
                self._paper.indicators_only = True
            if stack is None:
                self._patterns = []
                self._pattern_files = {}
                self.stats["tv_error"] = "Unknown TradingView stack."
                return
            self._patterns = [TvStackIndicator(stack)]
            self._pattern_files = {stack["name"]: "tv_indicator"}
            return
        if self._indicators_only:
            from indicators import discover as discover_indicators
            self._version_set = {}
            self._patterns = discover_indicators()
            self._pattern_files = {p.name: "indicator" for p in self._patterns}
            return
        if getattr(self, "_patterns_v2", False):
            from patterns_v2.signals import Patternsv2Scan
            self._version_set = {}
            self._patterns = [Patternsv2Scan()]
            self._pattern_files = {}
            return
        from core.pattern_loader import discover
        from core.pattern_versions import PatternVersions
        self._version_set = PatternVersions().resolve(disabled=self._disabled_patterns)
        # Preload each pinned version's own source once, here. Without it an
        # applied edit re-enters the candidate sandbox for every (symbol,
        # pattern) analysis, which is one fresh interpreter per bar.
        self._patterns = discover(
            self._disabled_patterns, self._version_set, in_process=True,
        )
        self._pattern_files = {p.name: "version:" + p.pattern_version_id for p in self._patterns}
