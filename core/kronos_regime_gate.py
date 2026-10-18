"""core/kronos_regime_gate.py — regime-gated Kronos short-confirm rule.

Validated in docs/kronos-finetune.md §13 on 1,000 walk-forward windows
(50 symbols, 22 asofs, Exp A weights): magnitude thresholds are monotone —
bigger predicted declines → higher hit rate AND bigger realized edge:

    STRONG past-5d tape + pred<=-0%: n=689 dir=59.1% shortPnL=+0.79%
    STRONG past-5d tape + pred<=-3%: n=372 dir=62.1% shortPnL=+1.26%
    STRONG past-5d tape + pred<=-5%: n=210 dir=66.7% shortPnL=+1.89%

while weak-tape shorts hit 34% (tape bounces crush them) and longs are a
coin flip everywhere (~52%). So this gate ONLY ever confirms shorts, and
only by fading a strong tape. Longs and weak-tape days get NO_OPINION
(absence of edge is not evidence — never veto on it).

Pure logic, no weights, no GPU: unit-testable in the app venv. The caller
(scanner) supplies the model's pred_3d and the universe trend computed from
asof-known bars only.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class GateDecision(str, Enum):
    CONFIRM_SHORT = "confirm_short"
    NO_OPINION = "no_opinion"


@dataclass(frozen=True)
class GateResult:
    decision: GateDecision
    reason: str


class GateUnavailable(Exception):
    """Predictor cannot run (no torch/weights) — enabled gate fails closed."""


class KronosRegimeGate:
    """Wiring between the scanner and `evaluate`.

    `predict_fn(df) -> float | None` maps a signal's OHLCV frame to a 3-day
    close-to-close forecast (fraction, e.g. -0.04). Injected in tests; default
    runs the fine-tuned Kronos predictor (lazy torch import — this module
    stays import-safe in the torch-free app venv).
    """

    def __init__(self, predict_fn=None, *, min_move: float = 0.03,
                 regime_days: int = 5, sample_count: int = 3):
        self._predict_fn = predict_fn
        self._min_move = float(min_move)
        self._regime_days = max(2, int(regime_days))
        self._sample_count = max(1, int(sample_count))
        self._predictor = None

    # ── forecast ─────────────────────────────────────────────────────────
    def predict_3d(self, df) -> float | None:
        """3-day forecast or None (unavailable/failed → caller fails closed)."""
        if df is None or len(df) < 60:
            return None
        if self._predict_fn is not None:
            try:
                out = self._predict_fn(df)
            except GateUnavailable:
                raise
            except Exception:
                return None
            return None if out is None else float(out)
        return self._predict_default(df)

    def _predict_default(self, df) -> float | None:
        try:
            import torch  # noqa: F401
        except ImportError as exc:
            raise GateUnavailable(f"torch not installed: {exc}")
        try:
            from core.kronos_eval import _load_predictor, predict_1w_return
        except ImportError as exc:
            raise GateUnavailable(f"kronos eval path missing: {exc}")
        if self._predictor is None:
            try:
                import torch as _t

                device = "cuda" if _t.cuda.is_available() else "cpu"
                self._predictor = _load_predictor(device=device, use_finetuned=True)
            except Exception as exc:
                raise GateUnavailable(f"predictor load failed: {exc}")
        try:
            out = predict_1w_return(
                self._predictor, df, sample_count=self._sample_count)
        except Exception:
            return None
        return None if out is None else float(out[0])

    # ── regime ───────────────────────────────────────────────────────────
    def universe_trend(self, symbols, get_df, timeframe: str = "1d") -> float:
        """Mean N-day return across symbols with enough bars (asof-known).

        Symbols without data are skipped; an empty universe yields NaN so
        `evaluate` withholds opinion instead of gating on nothing.
        """
        rets: list[float] = []
        for symbol in symbols or []:
            try:
                df = get_df(symbol, timeframe)
            except Exception:
                continue
            if df is None or len(df) < self._regime_days + 1:
                continue
            try:
                closes = df["close"].iloc[-(self._regime_days + 1):]
                base = float(closes.iloc[0])
                if base > 0:
                    rets.append(float(closes.iloc[-1]) / base - 1.0)
            except (KeyError, IndexError, ValueError):
                continue
        return universe_trend(rets)

    # ── decision ─────────────────────────────────────────────────────────
    def check_short(self, df, trend_5d: float) -> GateResult:
        """Full SELL-side decision: forecast, then `evaluate` (fail-closed)."""
        try:
            pred = self.predict_3d(df)
        except GateUnavailable as exc:
            return GateResult(GateDecision.NO_OPINION, f"predictor unavailable: {exc}")
        if pred is None:
            return GateResult(GateDecision.NO_OPINION, "no forecast for signal frame")
        return evaluate(pred, trend_5d, min_move=self._min_move)


def universe_trend(past_returns_5d: list[float]) -> float:
    """Mean 5-day return across the universe at asof. All inputs asof-known.

    Positive = strong tape (fade candidate); negative = weak tape (stand down).
    Empty input → 0.0 (neutral, which does NOT satisfy the >= 0 strong rule
    only if we require strictly... see `evaluate`: threshold is >= min with
    min defaulting 0.0, and empty input is treated as no-evidence upstream —
    callers should not gate on an empty universe).
    """
    if not past_returns_5d:
        return float("nan")
    vals = [float(x) for x in past_returns_5d]
    return sum(vals) / len(vals)


def evaluate(
    pred_3d: float,
    trend_5d: float,
    *,
    min_move: float = 0.03,
    require_strong_tape: bool = True,
) -> GateResult:
    """Decide on a pattern SHORT signal given Kronos' 3-day forecast.

    CONFIRM_SHORT iff the model predicts a decline clearing `min_move`
    AND (by default) the tape ran strong over the prior 5 days — the only
    slice with validated edge (§13). Everything else is NO_OPINION with
    the reason recorded for logs.
    """
    import math

    if isinstance(pred_3d, float) and math.isnan(pred_3d):
        return GateResult(GateDecision.NO_OPINION, "pred_3d is NaN")
    if isinstance(trend_5d, float) and math.isnan(trend_5d):
        return GateResult(GateDecision.NO_OPINION, "empty universe trend")
    if pred_3d > -min_move:
        return GateResult(
            GateDecision.NO_OPINION,
            f"|pred| {abs(pred_3d):.2%} below floor {min_move:.0%}",
        )
    if require_strong_tape and trend_5d < 0:
        return GateResult(
            GateDecision.NO_OPINION,
            f"weak tape ({trend_5d:+.2%} 5d) — shorts lose there (34% hit)",
        )
    return GateResult(
        GateDecision.CONFIRM_SHORT,
        f"fade strength: pred {pred_3d:+.2%} 3d on {trend_5d:+.2%} 5d tape",
    )
