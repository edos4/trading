"""
core/kronos_gate.py — Kronos 3-trading-day forecast filter for chart-pattern signals.

Does NOT generate entries. After a real pattern emits BUY/SELL, this gate
asks Kronos-base for a +3 trading day close forecast and only lets the signal
through when:
  - predicted move aligns with the signal action, AND
  - |pred_3d| >= 3% in those 3 days (settings.kronos_min_move_pct, default 0.03)

Used as a veto/confirm layer on top of Toby patterns, not as a standalone
entry (unlike the Kronos repo's finetune top-K demo).

If weights are missing or predict fails, the gate is fail-closed by default
(`settings.kronos_gate_fail_open=False`): the signal is rejected so a broken
Kronos install cannot silently let un-vetted signals through. Set
`kronos_gate_fail_open=True` only for research runs that should tolerate a
missing model.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from patterns.base_pattern import TradeSignal
from data.ohlcv_store import OHLCVStore, DEFAULT_WINDOW
from core.kronos_eval import (
    GATE_HORIZON_BARS,
    LOOKBACK,
    MAX_CONTEXT,
    MODEL_PATH,
    _load_predictor,
    predict_1w_return,
    predict_1w_return_batch,
)
from config import settings
from utils.logger import log

_INFER_LOCK = threading.Lock()
_facade_df_cache: dict[str, object] = {}
_facade_cache_lock = threading.Lock()


def kronos_infer_lock() -> threading.Lock:
    """Process-wide GPU/CPU infer lock. US + PH threads must not overlap."""
    return _INFER_LOCK


def _context_lookback() -> int:
    """Bars fed to KronosPredictor — official demos use LOOKBACK=400.

    Capped by store window, TV history pull, and model max_context (512).
    """
    available = min(DEFAULT_WINDOW, settings.tv_history_days)
    return max(60, min(LOOKBACK, available, MAX_CONTEXT))


def _facade_daily_df(symbol: str):
    """Cached API/Postgres daily frame (no TV). None if short or unavailable."""
    key = (symbol or "").upper()
    with _facade_cache_lock:
        if key in _facade_df_cache:
            return _facade_df_cache[key]
    try:
        from data.history import load_daily_ohlcv_df

        df = load_daily_ohlcv_df(key, tv_fallback=False, limit=MAX_CONTEXT)
    except Exception:
        df = None
    if df is not None and len(df) < 60:
        df = None
    with _facade_cache_lock:
        _facade_df_cache[key] = df
    return df


def _load_gate_df(symbol: str, timeframe: str, store: OHLCVStore, lookback: int):
    df = _facade_daily_df(symbol)
    if df is None or len(df) < lookback:
        stored = store.get_df(symbol, timeframe, min_bars=lookback)
        if stored is not None:
            df = stored
    return df


def _insufficient_bars(df, lookback: int) -> bool:
    return df is None or len(df) < min(60, lookback)


def _bars_fail_result() -> KronosGateResult:
    if settings.kronos_gate_fail_open:
        return KronosGateResult(passed=True, reason="insufficient bars (fail-open)")
    return KronosGateResult(passed=False, reason="insufficient bars (fail-closed)")


def _model_fail_result() -> KronosGateResult:
    if settings.kronos_gate_fail_open:
        return KronosGateResult(passed=True, reason="model unavailable (fail-open)")
    return KronosGateResult(passed=False, reason="model unavailable (fail-closed)")


def _predict_fail_result() -> KronosGateResult:
    if settings.kronos_gate_fail_open:
        return KronosGateResult(passed=True, reason="predict error (fail-open)")
    return KronosGateResult(passed=False, reason="predict error (fail-closed)")


def _apply_pred(
    signal: TradeSignal,
    pred_1w: float,
    last_close: float,
    *,
    adjust_exits: bool,
) -> KronosGateResult:
    min_move = settings.kronos_min_move_pct
    horizon = GATE_HORIZON_BARS

    if abs(pred_1w) < min_move:
        return KronosGateResult(
            passed=False,
            pred_1w=pred_1w,
            reason=(
                f"|pred_3d|={abs(pred_1w):.2%} < min {min_move:.2%} "
                f"in {horizon}d"
            ),
        )

    aligned = (signal.action == "BUY" and pred_1w > 0) or (
        signal.action == "SELL" and pred_1w < 0
    )
    if not aligned:
        return KronosGateResult(
            passed=False,
            pred_1w=pred_1w,
            reason=f"pred_3d={pred_1w:+.2%} conflicts with {signal.action}",
        )

    note = f"KronosGate 3d {pred_1w:+.2%} in {horizon}d"
    signal.notes = f"{signal.notes} | {note}".strip(" |") if signal.notes else note

    if adjust_exits:
        signal.take_profit = last_close * (1 + pred_1w)
        stop_pct = abs(pred_1w) * 0.5
        if signal.action == "BUY":
            signal.stop_loss = last_close * (1 - stop_pct)
        else:
            signal.stop_loss = last_close * (1 + stop_pct)

    return KronosGateResult(passed=True, pred_1w=pred_1w, reason="aligned")


@dataclass(frozen=True)
class KronosGateResult:
    passed: bool
    # Close-to-close % over GATE_HORIZON_BARS (3 trading days). Name is historical.
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
                    f"{MODEL_PATH} — gate disabled (fail-closed by default). "
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
            log.exception(
                "KronosGate | failed to load Kronos — gate unavailable "
                "(fail-closed by default) for this run"
            )
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
        """Return whether `signal` clears the 3% in 3 trading days Kronos filter.

        When passed and adjust_exits is True, mutates signal.take_profit /
        stop_loss from the forecast path (in place).
        """
        if adjust_exits is None:
            adjust_exits = settings.kronos_gate_adjust_exits

        if signal.action == "CLOSE":
            return KronosGateResult(passed=True, reason="skipped")

        if signal.timeframe != "1d":
            return KronosGateResult(passed=True, reason="non-daily skip")

        lookback = _context_lookback()
        df = _load_gate_df(signal.symbol, signal.timeframe, store, lookback)
        if _insufficient_bars(df, lookback):
            return _bars_fail_result()

        with _INFER_LOCK:
            if not self._ensure_loaded():
                return _model_fail_result()
            out = predict_1w_return(
                self._predictor,
                df,
                sample_count=settings.kronos_sample_count,
                lookback=lookback,
            )
        if out is None:
            return _predict_fail_result()
        pred_1w, last_close = out
        return _apply_pred(
            signal, pred_1w, last_close, adjust_exits=adjust_exits,
        )

    def check_many(
        self,
        signals: list[TradeSignal],
        store: OHLCVStore,
        *,
        adjust_exits: bool | None = None,
    ) -> list[KronosGateResult]:
        """Batch Kronos gate. One result per signal, same 3% / 3d rule as ``check``.

        Dedupes GPU work by symbol. Only used when the caller opted into
        collect-then-batch (scanner/UI "Batch Kronos"). ``check()`` stays
        sequential. Horizon is ``GATE_HORIZON_BARS`` (3 trading days), not 1w.
        """
        if adjust_exits is None:
            adjust_exits = settings.kronos_gate_adjust_exits
        if not signals:
            return []

        lookback = _context_lookback()
        results: list[KronosGateResult | None] = [None] * len(signals)
        need: list[int] = []
        frames_by_symbol: dict[str, object] = {}

        for i, signal in enumerate(signals):
            if signal.action == "CLOSE":
                results[i] = KronosGateResult(passed=True, reason="skipped")
                continue
            if signal.timeframe != "1d":
                results[i] = KronosGateResult(passed=True, reason="non-daily skip")
                continue
            df = _load_gate_df(signal.symbol, signal.timeframe, store, lookback)
            if _insufficient_bars(df, lookback):
                results[i] = _bars_fail_result()
                continue
            need.append(i)
            key = (signal.symbol or "").upper()
            if key not in frames_by_symbol:
                frames_by_symbol[key] = df

        if not need:
            return [r if r is not None else _predict_fail_result() for r in results]

        unique_symbols = list(dict.fromkeys(
            (signals[i].symbol or "").upper() for i in need
        ))
        unique_frames = [frames_by_symbol[s] for s in unique_symbols]

        with _INFER_LOCK:
            if not self._ensure_loaded():
                fail = _model_fail_result()
                for i in need:
                    results[i] = fail
                return [r if r is not None else fail for r in results]
            outs = predict_1w_return_batch(
                self._predictor,
                unique_frames,
                sample_count=settings.kronos_sample_count,
                lookback=lookback,
                batch_size=settings.kronos_batch_size,
            )

        by_sym = dict(zip(unique_symbols, outs))
        for i in need:
            key = (signals[i].symbol or "").upper()
            out = by_sym.get(key)
            if out is None:
                results[i] = _predict_fail_result()
                continue
            pred_1w, last_close = out
            results[i] = _apply_pred(
                signals[i], pred_1w, last_close, adjust_exits=adjust_exits,
            )
        return [
            r if r is not None else _predict_fail_result() for r in results
        ]


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


def kronos_gate_check_many(
    signals: list[TradeSignal],
    store: OHLCVStore,
    *,
    adjust_exits: bool | None = None,
) -> list[KronosGateResult]:
    """Batch convenience wrapper. Sequential ``check()`` is unchanged."""
    return get_kronos_gate().check_many(
        signals, store, adjust_exits=adjust_exits,
    )
