"""core/kronos_confirm.py — 1-day Kronos entry confirmation (docs/kronos-3-preds.md).

Every surviving entry signal is assessed by N stochastic one-day draws from
one checkpoint; agreement is not a calibrated probability or model ensemble.
Each simulated close is classified up/down/flat against an ATR-scaled band,
and the vote is compared with the signal's direction. Pure logic stays
torch-free and injectable; the model is reached through a batched subprocess
in ``.venv-kronos``.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Literal, Sequence

import pandas as pd

from core.kronos_process import (
    KronosBusy,
    KronosError,
    KronosUnavailable,
    run_worker,
)
from indicators._math import wilder_atr
from utils.logger import log

WORKER_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "kronos_confirm_worker.py"

ATR_PERIOD = 14
MIN_BARS = 60

# A one-day forecast further than this from its own anchor is not a forecast.
# Kronos occasionally collapses on a context it cannot represent: on a 7.8%-ATR
# name it returned closes of -85% and -66% (its sibling sample was nonpositive).
# Usable paper votes span about -31%..+31% at this horizon, so the bound
# separates the two without touching any observed vote. An implausible
# simulation is dropped exactly like a non-finite one: it never votes, and it
# never lowers the majority that the surviving samples must reach.
MAX_CREDIBLE_RETURN = 0.50

Direction = Literal["up", "down", "flat"]
Decision = Literal["confirm", "oppose", "no_opinion"]


@dataclass(frozen=True)
class ConfirmSummary:
    anchor_close: float
    band_pct: float
    returns: tuple[float, ...]
    up: int
    down: int
    flat: int
    mean_return: float


@dataclass(frozen=True)
class ConfirmVerdict:
    decision: Decision
    reason: str
    direction: Direction | None = None


@dataclass(frozen=True)
class Forecast:
    returns: tuple[float, ...]
    anchor_date: str
    anchor_close: float
    atr_pct: float
    error: str | None = None
    provenance: dict | None = None
    context_sha256: str | None = None
    model_context: dict | None = None


@dataclass(frozen=True)
class PendingConfirm:
    signal: object
    candle: object
    anchor_date: str
    anchor_close: float
    atr_pct: float
    band_pct: float
    model_provenance: dict | None = None
    context: dict | None = None
    model_context: dict | None = None

    @property
    def symbol(self) -> str:
        return self.signal.symbol


@dataclass(frozen=True)
class PendingExit:
    symbol: str
    timeframe: str
    positions: tuple
    candle: object
    anchor_date: str
    anchor_close: float
    atr_pct: float
    band_pct: float
    model_provenance: dict | None = None
    context: dict | None = None
    model_context: dict | None = None


def _finite(value) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def sideways_band_pct(
    atr_pct: float,
    *,
    k: float = 0.35,
    min_pct: float = 0.25,
    max_pct: float = 3.0,
) -> float:
    """ATR-scaled dead zone, clamped at both pathological ends."""
    base = float(atr_pct) if _finite(atr_pct) and float(atr_pct) > 0 else 0.0
    return max(float(min_pct), min(float(k) * base, float(max_pct)))


def atr_pct_from_frame(df: pd.DataFrame | None, anchor_close: float) -> float:
    """ATR(14) as a percentage of the anchor close, or 0 when unavailable."""
    if df is None or len(df) < ATR_PERIOD + 1 or not _finite(anchor_close) or anchor_close <= 0:
        return 0.0
    try:
        high = pd.to_numeric(df["high"], errors="coerce").to_numpy(dtype=float)
        low = pd.to_numeric(df["low"], errors="coerce").to_numpy(dtype=float)
        close = pd.to_numeric(df["close"], errors="coerce").to_numpy(dtype=float)
    except (KeyError, TypeError, ValueError):
        return 0.0
    atr = wilder_atr(high, low, close, ATR_PERIOD)
    value = float(atr[-1])
    if not math.isfinite(value) or value <= 0:
        return 0.0
    return value / float(anchor_close) * 100.0


def classify(ret: float, band_pct: float) -> Direction:
    """Classify a close-to-close return against the sideways band."""
    band = float(band_pct) / 100.0
    # The stored band is a percentage; a one-ulp tolerance keeps an exact
    # boundary return on the directional side after the division.
    eps = 1e-12
    if ret >= band - eps:
        return "up"
    if ret <= -band + eps:
        return "down"
    return "flat"


def summarize(
    anchor_close: float,
    band_pct: float,
    returns: Sequence[float],
) -> ConfirmSummary:
    """Count the simulations that may vote.

    A return that is non-finite or beyond ±``MAX_CREDIBLE_RETURN`` is not
    evidence about direction, it is a model artifact, so it is dropped — the
    same way a missing simulation is. Survivors still have to reach the
    configured majority in ``evaluate``, so dropping one never makes a
    confirmation easier.
    """
    usable = tuple(
        value
        for value in (float(r) for r in returns if _finite(r))
        if abs(value) <= MAX_CREDIBLE_RETURN
    )
    counts = {"up": 0, "down": 0, "flat": 0}
    for ret in usable:
        counts[classify(ret, band_pct)] += 1
    mean_return = sum(usable) / len(usable) if usable else float("nan")
    return ConfirmSummary(
        anchor_close=float(anchor_close),
        band_pct=float(band_pct),
        returns=usable,
        up=counts["up"],
        down=counts["down"],
        flat=counts["flat"],
        mean_return=mean_return,
    )


def evaluate(
    summary: ConfirmSummary,
    action: str,
    *,
    min_agree: int = 2,
) -> ConfirmVerdict:
    """Use only the majority direction; unresolved votes have no direction."""
    usable = len(summary.returns)
    need = max(1, int(min_agree), usable // 2 + 1)
    if usable < need:
        return ConfirmVerdict(
            "no_opinion",
            f"only {usable} usable forecast(s); need {need} to vote",
        )

    if action == "BUY":
        direction, opposite, against = "up", "down", summary.down
    elif action == "SELL":
        direction, opposite, against = "down", "up", summary.up
    else:
        return ConfirmVerdict("no_opinion", f"{action} is not an entry signal")

    votes_for = summary.up if direction == "up" else summary.down
    if votes_for >= need:
        return ConfirmVerdict(
            "confirm",
            f"{votes_for}/{usable} forecasts {direction} (need {need})",
            direction,
        )
    if against >= need:
        return ConfirmVerdict(
            "oppose",
            f"{against}/{usable} forecasts against the {action} (need {need})",
            opposite,
        )
    if summary.flat >= need:
        return ConfirmVerdict(
            "no_opinion",
            f"{summary.flat}/{usable} forecasts flat (need {need})",
            "flat",
        )
    return ConfirmVerdict(
        "no_opinion",
        f"no majority of {need} in either direction",
    )


def _normalize_samples(value: int) -> int:
    samples = max(1, min(int(value), 9))
    return samples if samples % 2 else max(1, samples - 1)


class KronosConfirmGate:
    """Pending batch, TTL cache, worker call, and evidence for the scanner."""

    def __init__(
        self,
        *,
        mode: str = "off",
        market: str = "us",
        samples: int = 3,
        model: str = "finetuned",
        sideways_atr: float = 0.35,
        sideways_min_pct: float = 0.25,
        sideways_max_pct: float = 3.0,
        min_agree: int = 2,
        exit_mode: str = "off",
        exit_min_agree: int | None = None,
        cache_minutes: int = 30,
        timeout_s: float = 180.0,
        max_checks_per_scan: int = 100,
        worker_script: Path | None = None,
        fetch_fn: Callable[[list[str]], dict[str, Sequence[float]]] | None = None,
        now_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        mode = (mode or "off").strip().lower()
        if mode not in ("off", "shadow", "confirm"):
            raise ValueError(f"Unsupported Kronos gate mode: {mode}")
        exit_mode = (exit_mode or "off").strip().lower()
        if exit_mode not in ("off", "shadow", "exit"):
            raise ValueError(f"Unsupported Kronos exit mode: {exit_mode}")
        self.mode = mode
        from core.market import get_market
        self.market = get_market(market).id
        self.exit_mode = exit_mode
        self.samples = _normalize_samples(samples)
        self.model = (model or "finetuned").strip().lower()
        if self.model not in ("base", "finetuned"):
            raise ValueError(f"Unsupported Kronos gate model: {model}")
        self.sideways_atr = float(sideways_atr)
        self.sideways_min_pct = float(sideways_min_pct)
        self.sideways_max_pct = float(sideways_max_pct)
        self.min_agree = max(self.samples // 2 + 1, min(self.samples, max(1, int(min_agree))))
        self.exit_min_agree = max(
            self.samples // 2 + 1,
            min(self.samples, max(1, int(exit_min_agree if exit_min_agree is not None else min_agree))),
        )
        self.cache_minutes = max(0, int(cache_minutes))
        self.timeout_s = float(timeout_s)
        self.max_checks_per_scan = max(1, int(max_checks_per_scan))
        self.worker_script = Path(worker_script or WORKER_SCRIPT)
        self._fetch_fn = fetch_fn
        self._now = now_fn
        self._cache: dict[tuple, tuple[float, Forecast]] = {}
        self.pending: list[PendingConfirm] = []
        self.pending_exits: list[PendingExit] = []
        # Device the last worker call actually used (cuda or cpu fallback).
        self.last_device: str | None = None
        self._model_identity: str | None = None
        self.live_decision = False

    # ── pending batch ────────────────────────────────────────────────────
    def prepare(self, signal, candle, df: pd.DataFrame | None) -> PendingConfirm | None:
        """Build one pending confirmation; None when mode is off."""
        if self.mode == "off":
            return None
        anchor_close = float(getattr(candle, "close", 0.0) or 0.0)
        if not _finite(anchor_close) or anchor_close <= 0:
            if df is not None and len(df):
                anchor_close = float(df["close"].iloc[-1])
        anchor_date = _anchor_date(candle, signal, self.market)
        atr_pct = atr_pct_from_frame(df, anchor_close)
        band = sideways_band_pct(
            atr_pct,
            k=self.sideways_atr,
            min_pct=self.sideways_min_pct,
            max_pct=self.sideways_max_pct,
        )
        pending = PendingConfirm(
            signal=signal,
            candle=candle,
            anchor_date=anchor_date,
            anchor_close=anchor_close,
            atr_pct=atr_pct,
            band_pct=band,
            context=__import__('core.kronos_context',fromlist=['frame_context']).frame_context(df,anchor_date,signal.timeframe),
        )
        self.pending.append(pending)
        return pending

    def queue(self, signal, candle, df: pd.DataFrame | None) -> bool:
        """Append a pending entry; True when the caller must defer the entry."""
        return self.prepare(signal, candle, df) is not None

    def prepare_exit(self, positions, candle, df: pd.DataFrame | None) -> PendingExit | None:
        """Queue one vote that can close a symbol's open positions."""
        if self.exit_mode == "off":
            return None
        positions = tuple(
            p for p in positions if getattr(p, "action", None) in ("BUY", "SELL")
        )
        if not positions:
            return None
        symbol = positions[0].symbol
        timeframe = getattr(positions[0], "timeframe", "") or ""
        anchor_close = float(getattr(candle, "close", 0.0) or 0.0)
        if not _finite(anchor_close) or anchor_close <= 0:
            if df is not None and len(df):
                anchor_close = float(df["close"].iloc[-1])
        anchor_date = _anchor_date(candle, None, self.market)
        key = (symbol, timeframe, anchor_date)
        for item in self.pending_exits:
            if (item.symbol, item.timeframe, item.anchor_date) == key:
                return item
        atr_pct = atr_pct_from_frame(df, anchor_close)
        pending = PendingExit(
            symbol=symbol,
            timeframe=timeframe,
            positions=positions,
            candle=candle,
            anchor_date=anchor_date,
            anchor_close=anchor_close,
            atr_pct=atr_pct,
            band_pct=sideways_band_pct(
                atr_pct,
                k=self.sideways_atr,
                min_pct=self.sideways_min_pct,
                max_pct=self.sideways_max_pct,
            ),
            context=__import__('core.kronos_context',fromlist=['frame_context']).frame_context(df,anchor_date,timeframe),
        )
        self.pending_exits.append(pending)
        return pending

    def clear_pending(self) -> None:
        self.pending.clear()
        self.pending_exits.clear()

    # ── cache ────────────────────────────────────────────────────────────
    def _cache_key(self, pending: PendingConfirm | PendingExit) -> tuple:
        from core.kronos_input import POLICY_VERSION
        return (self.market, pending.symbol, pending.anchor_date, pending.anchor_close,
                self.model, self.samples, (pending.context or {}).get('sha256'), self._model_identity, POLICY_VERSION)

    def _cache_get(self, key: tuple) -> Forecast | None:
        if self.cache_minutes <= 0:
            return None
        hit = self._cache.get(key)
        if hit is None:
            return None
        expires_at, returns = hit
        if self._now() >= expires_at:
            self._cache.pop(key, None)
            return None
        return returns

    def _cache_put(self, key: tuple, returns: Forecast) -> None:
        if self.cache_minutes <= 0:
            return
        self._cache[key] = (self._now() + self.cache_minutes * 60.0, returns)

    # ── worker ───────────────────────────────────────────────────────────
    def _fetch(
        self, symbols: list[str], as_of: str | None = None, contexts: dict | None = None,
    ) -> dict[str, Forecast | Sequence[float]]:
        if self._fetch_fn is not None:
            return self._fetch_fn(symbols)
        args = [
            "--symbols", *symbols,
            "--samples", str(self.samples),
            "--model", self.model,
            "--market", self.market,
        ]
        if self.live_decision:
            from datetime import datetime, timezone
            args += ["--decision-at", datetime.now(timezone.utc).isoformat()]
        if as_of:
            # A replay date is a hard information boundary. Without it the
            # worker loads the live series and the vote sees future bars.
            args += ["--as-of", as_of]
        from config import settings
        if settings.kronos_require_provenance:
            args += ['--require-provenance']
        import json
        import tempfile
        context_path=None
        try:
            if contexts:
                with tempfile.NamedTemporaryFile(mode='w',suffix='.json',prefix='kronos-input-',delete=False) as f:
                    json.dump(contexts,f,allow_nan=False)
                    context_path=Path(f.name)
                args += ['--contexts',str(context_path)]
            payload = run_worker(self.worker_script,args,timeout_s=self.timeout_s,label='confirm')
        finally:
            if context_path is not None:
                context_path.unlink(missing_ok=True)
        received_identity = payload.get("model_identity")
        if received_identity is not None and received_identity != self._model_identity:
            raise KronosError("Kronos deployed model identity changed during the batch; retry")
        device = payload.get("device")
        if isinstance(device, str) and device:
            self.last_device = device
        rows = payload.get("results")
        if not isinstance(rows, list):
            raise KronosError("confirm worker returned no results")
        if (payload.get("model") != self.model or payload.get("samples") != self.samples
                or payload.get("market") != self.market):
            raise KronosError("confirm worker market/model/sample settings differ from the request")
        out: dict[str, Forecast] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            symbol = str(row.get("symbol") or "").upper()
            if not symbol:
                continue
            if row.get("error"):
                out[symbol] = Forecast((), "", 0, 0, error=str(row["error"]))
                continue
            if contexts and symbol in contexts and row.get('context_sha256')!=contexts[symbol]['sha256']:
                out[symbol]=Forecast((),'',0,0,error='Kronos context identity differs from scanner snapshot')
                continue
            raw = row.get("returns") or []
            if not isinstance(raw, (tuple, list)) or any(not _finite(value) for value in raw):
                continue
            out[symbol] = Forecast(
                tuple(float(value) for value in raw), str(row.get("anchor_date") or ""),
                float(row["anchor_close"]) if _finite(row.get("anchor_close")) else 0,
                float(row["atr_pct"]) if _finite(row.get("atr_pct")) else float("nan"),
                provenance=payload.get('model_provenance'),
                context_sha256=row.get('context_sha256'), model_context=row.get('model_context'),
            )
        return out

    # ── flush ────────────────────────────────────────────────────────────
    def flush(self) -> list[tuple[object, object, ConfirmVerdict, dict]]:
        """Resolve pending entries (the original entry-gate call shape)."""
        entries, _exits = self.flush_with_exits()
        return entries

    def flush_with_exits(
        self,
    ) -> tuple[
        list[tuple[object, object, ConfirmVerdict, dict]],
        list[tuple[object, ConfirmVerdict, dict]],
    ]:
        """Resolve entry votes and open-position votes in one worker call."""
        from core.kronos_provenance import active_model_identity
        identity = active_model_identity() if self.model == "finetuned" else "base"
        if identity != self._model_identity:
            self._cache.clear()
            self._model_identity = identity
        entries = list(self.pending)
        exits = list(self.pending_exits)
        self.pending.clear()
        self.pending_exits.clear()
        if not entries and not exits:
            return [], []

        # Existing positions get the limited inference budget before new risk.
        order=lambda item:(item.anchor_date,item.symbol,getattr(item,'timeframe',getattr(getattr(item,'signal',None),'timeframe','')),item.anchor_close)
        all_items: list[PendingConfirm | PendingExit] = [*sorted(exits,key=order), *sorted(entries,key=order)]
        resolved: dict[tuple, tuple[Forecast, bool]] = {}
        missing: dict[tuple, PendingConfirm | PendingExit] = {}
        failures: dict[tuple, str] = {}
        for item in all_items:
            key = self._cache_key(item)
            if not item.anchor_date or not _finite(item.anchor_close) or item.anchor_close <= 0:
                failures[key] = "Kronos requires a dated completed bar and positive anchor close"
                continue
            cached = self._cache_get(key)
            if cached is not None:
                resolved[key] = (cached, True)
            else:
                missing.setdefault(key, item)

        keys = list(missing)
        checked = keys[: self.max_checks_per_scan]
        over_cap = set(keys[self.max_checks_per_scan:])
        fetched: dict[tuple, Forecast] = {}
        if checked:
            # One call per replay date: the batch may mix entry and exit votes,
            # but every symbol must be cut at its own anchor bar.
            groups: dict[str, list[tuple]] = {}
            for key in checked:
                groups.setdefault(missing[key].anchor_date, []).append(key)
            for as_of, group in groups.items():
                symbols = list(dict.fromkeys(missing[key].symbol for key in group))
                try:
                    contexts={missing[key].symbol:missing[key].context for key in group if missing[key].context is not None}
                    result = self._fetch(symbols, as_of=as_of, contexts=contexts)
                    if not isinstance(result, dict):
                        for key in group:
                            failures[key] = "Kronos worker returned no payload"
                    else:
                        for key in group:
                            item = missing[key]
                            value = result.get(item.symbol)
                            if value is None:
                                continue
                            # A sequence-only provider is an injected test adapter.
                            forecast = value if isinstance(value, Forecast) else Forecast(
                                tuple(float(v) for v in value if _finite(v)), item.anchor_date,
                                item.anchor_close, item.atr_pct,
                            )
                            if forecast.error:
                                failures[key] = f"Kronos worker error for {item.symbol}: {forecast.error}"
                            elif forecast.anchor_date != item.anchor_date:
                                failures[key] = (
                                    "Kronos forecast date differs from the decision bar: "
                                    f"expected {item.anchor_date}, received {forecast.anchor_date or 'missing'}"
                                )
                            elif not math.isclose(forecast.anchor_close, item.anchor_close, rel_tol=0, abs_tol=.000051):
                                failures[key] = "Kronos forecast price differs from the decision bar"
                            elif len(forecast.returns) > self.samples:
                                failures[key] = "Kronos returned more simulations than requested"
                            elif not _finite(forecast.atr_pct) or forecast.atr_pct < 0:
                                failures[key] = "Kronos forecast volatility is missing or invalid"
                            else:
                                fetched[key] = forecast
                except KronosUnavailable as exc:
                    for key in group:
                        failures[key] = f"Kronos unavailable: {exc}"
                except KronosBusy as exc:
                    for key in group:
                        failures[key] = f"Kronos busy: {exc}"
                except KronosError as exc:
                    for key in group:
                        failures[key] = f"Kronos worker error: {exc}"
                except Exception as exc:
                    log.exception("Kronos confirm | worker call failed")
                    for key in group:
                        failures[key] = f"Kronos worker error: {exc}"
            for key, forecast in fetched.items():
                if forecast.returns:
                    self._cache_put(key, forecast)

        if self.model == "finetuned" and active_model_identity() != self._model_identity:
            resolved.clear()
            fetched.clear()
            self._cache.clear()
            for item in all_items:
                failures[self._cache_key(item)] = "Kronos model identity changed during the batch; retry"

        def _miss_reason(item: PendingConfirm | PendingExit) -> str:
            key = self._cache_key(item)
            if key in over_cap:
                return "Kronos check cap reached"
            if key in failures:
                return failures[key]
            return f"no forecast for {item.symbol}"

        entry_rows: list[tuple[object, object, ConfirmVerdict, dict]] = []
        for item in entries:
            hit = self._hit(item, resolved, fetched, over_cap)
            if hit is None:
                verdict = ConfirmVerdict("no_opinion", _miss_reason(item))
                summary = summarize(item.anchor_close, item.band_pct, ())
                evidence = _evidence(self, item, summary, verdict, cached=False)
            else:
                forecast, cached = hit
                item = self._forecast_context(item, forecast)
                summary = summarize(item.anchor_close, item.band_pct, forecast.returns)
                verdict = evaluate(summary, item.signal.action, min_agree=self.min_agree)
                evidence = _evidence(self, item, summary, verdict, cached=cached)
            entry_rows.append((item.signal, item.candle, verdict, evidence))

        exit_rows: list[tuple[object, ConfirmVerdict, dict]] = []
        for item in exits:
            hit = self._hit(item, resolved, fetched, over_cap)
            if hit is None:
                summary = summarize(item.anchor_close, item.band_pct, ())
                for position in item.positions:
                    verdict = ConfirmVerdict("no_opinion", _miss_reason(item))
                    exit_rows.append(
                        (position, verdict, _evidence(
                            self, item, summary, verdict, cached=False, scope="position", action=position.action,
                        ))
                    )
                continue
            forecast, cached = hit
            item = self._forecast_context(item, forecast)
            summary = summarize(item.anchor_close, item.band_pct, forecast.returns)
            for position in item.positions:
                verdict = evaluate(
                    summary, position.action, min_agree=self.exit_min_agree,
                )
                exit_rows.append(
                    (position, verdict, _evidence(
                        self, item, summary, verdict, cached=cached, scope="position", action=position.action,
                    ))
                )
        return entry_rows, exit_rows

    def _hit(self, item, resolved, fetched, over_cap):
        if self._cache_key(item) in resolved:
            return resolved[self._cache_key(item)]
        if self._cache_key(item) in over_cap:
            return None
        returns = fetched.get(self._cache_key(item))
        if returns is None:
            return None
        return returns, False

    def _forecast_context(self, item, forecast: Forecast):
        return replace(item, model_provenance=forecast.provenance, model_context=forecast.model_context, atr_pct=forecast.atr_pct, band_pct=sideways_band_pct(
            forecast.atr_pct, k=self.sideways_atr,
            min_pct=self.sideways_min_pct, max_pct=self.sideways_max_pct,
        ))


def _anchor_date(candle, signal, market: str = "us") -> str:
    ts = getattr(candle, "timestamp", None) or getattr(signal, "signal_bar_timestamp", None)
    if ts is None:
        return ""
    try:
        from core.market import get_market
        from data.chart_dates import chart_session_day
        day = chart_session_day(ts, get_market(market).session_tz)
        return day.date().isoformat() if day is not None else ""
    except (TypeError, ValueError):
        return ""


def _evidence(
    gate: KronosConfirmGate,
    item: PendingConfirm | PendingExit,
    summary: ConfirmSummary,
    verdict: ConfirmVerdict,
    *,
    cached: bool,
    scope: str = "entry",
    action: str | None = None,
) -> dict:
    return {
        "schema_version": 1,
        "symbol": item.symbol,
        "action": item.signal.action if scope == "entry" else action,
        "timeframe": item.signal.timeframe if scope == "entry" else item.timeframe,
        "market": gate.market,
        "anchor_date": item.anchor_date,
        "forecast_horizon": "1d",
        "anchor_close": item.anchor_close,
        "atr_pct": item.atr_pct,
        "band_pct": item.band_pct,
        "returns": list(summary.returns),
        "votes": {"up": summary.up, "down": summary.down, "flat": summary.flat},
        "direction": verdict.direction,
        "decision_basis": "direction_only",
        "decision": verdict.decision,
        "reason": verdict.reason,
        "mode": gate.mode if scope == "entry" else gate.exit_mode,
        "scope": scope,
        "model": gate.model,
        "model_identity": gate._model_identity,
        "model_context": item.model_context,
        "model_provenance": item.model_provenance or {'identity_verified':False,'out_of_sample_eligible':False},
        "context_sha256": (item.context or {}).get('sha256'),
        "context_source": 'scanner_snapshot' if item.context else 'provider_history',
        "failure_kind": ('capacity' if verdict.reason=='Kronos check cap reached' else
                         'infrastructure_or_context' if any(x in verdict.reason.lower() for x in ('worker','unavailable','busy','differs','no forecast')) else
                         'model_vote'),
        "device": gate.last_device,
        "samples": gate.samples,
        "cached": bool(cached),
    }
