"""
core/kronos_gate.py — Kronos 1-week forecast filter for chart-pattern signals.

Does NOT generate entries. After a real pattern emits BUY/SELL, this gate
asks Kronos-base for a +1 week close forecast and only lets the signal
through when:
  - predicted move aligns with the signal action, AND
  - |pred_1w| >= settings.kronos_min_move_pct

Used as a veto/confirm layer on top of Toby patterns, not as a standalone
entry (unlike the Kronos repo's finetune top-K demo).

If weights are missing or predict fails, the gate fails open (passes the
signal) so a broken Kronos install cannot freeze the scanner.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from patterns.base_pattern import TradeSignal
from data.ohlcv_store import OHLCVStore, DEFAULT_WINDOW
from core.kronos_eval import (
    LOOKBACK,
    MAX_CONTEXT,
    MODEL_PATH,
    _load_predictor,
    predict_1w_return,
)
from config import settings
from utils.logger import log


def _context_lookback() -> int:
    """Bars fed to KronosPredictor — official demos use LOOKBACK=400.

    Capped by store window, TV history pull, and model max_context (512).
    """
    available = min(DEFAULT_WINDOW, settings.tv_history_days)
    return max(60, min(LOOKBACK, available, MAX_CONTEXT))


@dataclass(frozen=True)
class KronosGateResult:
    passed: bool
    pred_1w: float | None = None
    reason: str = ""


class KronosGate:
    """Lazy-loaded singleton-friendly gate. One instance per process is enough."""

    def __init__(self) -> None:
        self._predictor = None
        self._load_failed = False
        self._warned_missing = False

    @property
    def available(self) -> bool:
        return MODEL_PATH.exists() and not self._load_failed

    def _ensure_loaded(self) -> bool:
        if self._predictor is not None:
            return True
        if self._load_failed:
            return False
        if not MODEL_PATH.exists():
            if not self._warned_missing:
                log.warning(
                    "KronosGate | weights missing at "
                    f"{MODEL_PATH} — gate disabled (fail-open). "
                    "See README Kronos setup."
                )
                self._warned_missing = True
            return False
        try:
            import torch

            device = "cuda" if torch.cuda.is_available() else "cpu"
            self._predictor = _load_predictor(
                device=device,
                use_finetuned=settings.kronos_use_finetuned,
            )
        except Exception:
            log.exception("KronosGate | failed to load Kronos — fail-open for this run")
            self._load_failed = True
            return False
        return True

    def check(
        self,
        signal: TradeSignal,
        store: OHLCVStore,
        *,
        adjust_exits: bool | None = None,
    ) -> KronosGateResult:
        """Return whether `signal` clears the 1w Kronos filter.

        When passed and adjust_exits is True, mutates signal.take_profit /
        stop_loss from the forecast path (in place).
        """
        if adjust_exits is None:
            adjust_exits = settings.kronos_gate_adjust_exits

        if signal.action == "CLOSE":
            return KronosGateResult(passed=True, reason="skipped")

        if signal.timeframe != "1d":
            return KronosGateResult(passed=True, reason="non-daily skip")

        if not self._ensure_loaded():
            if settings.kronos_gate_fail_open:
                return KronosGateResult(
                    passed=True, reason="model unavailable (fail-open)"
                )
            return KronosGateResult(
                passed=False, reason="model unavailable (fail-closed)"
            )

        lookback = _context_lookback()
        df = store.get_df(signal.symbol, signal.timeframe, min_bars=lookback)
        if df is None:
            if settings.kronos_gate_fail_open:
                return KronosGateResult(
                    passed=True, reason="insufficient bars (fail-open)"
                )
            return KronosGateResult(
                passed=False, reason="insufficient bars (fail-closed)"
            )

        out = predict_1w_return(
            self._predictor,
            df,
            sample_count=settings.kronos_sample_count,
            lookback=lookback,
        )
        if out is None:
            if settings.kronos_gate_fail_open:
                return KronosGateResult(
                    passed=True, reason="predict error (fail-open)"
                )
            return KronosGateResult(
                passed=False, reason="predict error (fail-closed)"
            )

        pred_1w, last_close = out
        min_move = settings.kronos_min_move_pct

        if abs(pred_1w) < min_move:
            return KronosGateResult(
                passed=False,
                pred_1w=pred_1w,
                reason=f"|pred_1w|={abs(pred_1w):.2%} < min {min_move:.2%}",
            )

        aligned = (signal.action == "BUY" and pred_1w > 0) or (
            signal.action == "SELL" and pred_1w < 0
        )
        if not aligned:
            return KronosGateResult(
                passed=False,
                pred_1w=pred_1w,
                reason=f"pred_1w={pred_1w:+.2%} conflicts with {signal.action}",
            )

        note = f"KronosGate 1w {pred_1w:+.2%}"
        signal.notes = f"{signal.notes} | {note}".strip(" |") if signal.notes else note

        if adjust_exits:
            signal.take_profit = last_close * (1 + pred_1w)
            stop_pct = abs(pred_1w) * 0.5
            if signal.action == "BUY":
                signal.stop_loss = last_close * (1 - stop_pct)
            else:
                signal.stop_loss = last_close * (1 + stop_pct)

        return KronosGateResult(passed=True, pred_1w=pred_1w, reason="aligned")


# Process-local shared instance (scanner + each backtest worker).
_gate: KronosGate | None = None


def get_kronos_gate() -> KronosGate:
    global _gate
    if _gate is None:
        _gate = KronosGate()
    return _gate


def kronos_gate_check(
    signal: TradeSignal,
    store: OHLCVStore,
    *,
    adjust_exits: bool | None = None,
) -> KronosGateResult:
    """Convenience wrapper around the process-local KronosGate."""
    return get_kronos_gate().check(signal, store, adjust_exits=adjust_exits)
