"""core/kronos_confirm.py — 1-day Kronos entry confirmation (docs/kronos-3-preds.md).

Every surviving entry signal is confirmed by N independent one-day forecasts.
Each simulated close is classified up/down/flat against an ATR-scaled band,
and the vote is compared with the signal's direction. Pure logic stays
torch-free and injectable; the model is reached through a batched subprocess
in ``.venv-kronos``.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
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


@dataclass(frozen=True)
class PendingConfirm:
    signal: object
    candle: object
    anchor_date: str
    anchor_close: float
    atr_pct: float
    band_pct: float

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
    usable = tuple(float(r) for r in returns if _finite(r))
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
    """Confirm/oppose only on a majority; everything else is no_opinion."""
    need = max(1, int(min_agree))
    usable = len(summary.returns)
    if usable < need:
        return ConfirmVerdict(
            "no_opinion",
            f"only {usable} usable forecast(s); need {need} to vote",
        )

    if action == "BUY":
        direction, against = "up", summary.down
    elif action == "SELL":
        direction, against = "down", summary.up
    else:
        return ConfirmVerdict("no_opinion", f"{action} is not an entry signal")

    votes_for = summary.up if direction == "up" else summary.down
    if votes_for >= need:
        return ConfirmVerdict(
            "confirm",
            f"{votes_for}/{usable} forecasts {direction} (need {need})",
        )
    if against >= need:
        return ConfirmVerdict(
            "oppose",
            f"{against}/{usable} forecasts against the {action} (need {need})",
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
        self.exit_mode = exit_mode
        self.samples = _normalize_samples(samples)
        self.model = (model or "finetuned").strip().lower()
        if self.model not in ("base", "finetuned"):
            raise ValueError(f"Unsupported Kronos gate model: {model}")
        self.sideways_atr = float(sideways_atr)
        self.sideways_min_pct = float(sideways_min_pct)
        self.sideways_max_pct = float(sideways_max_pct)
        self.min_agree = max(1, int(min_agree))
        self.exit_min_agree = max(
            1, int(exit_min_agree if exit_min_agree is not None else min_agree)
        )
        self.cache_minutes = max(0, int(cache_minutes))
        self.timeout_s = float(timeout_s)
        self.max_checks_per_scan = max(1, int(max_checks_per_scan))
        self.worker_script = Path(worker_script or WORKER_SCRIPT)
        self._fetch_fn = fetch_fn
        self._now = now_fn
        self._cache: dict[tuple, tuple[float, tuple[float, ...]]] = {}
        self.pending: list[PendingConfirm] = []
        self.pending_exits: list[PendingExit] = []
        # Device the last worker call actually used (cuda or cpu fallback).
        self.last_device: str | None = None

    # ── pending batch ────────────────────────────────────────────────────
    def prepare(self, signal, candle, df: pd.DataFrame | None) -> PendingConfirm | None:
        """Build one pending confirmation; None when mode is off."""
        if self.mode == "off":
            return None
        anchor_close = float(getattr(candle, "close", 0.0) or 0.0)
        if not _finite(anchor_close) or anchor_close <= 0:
            if df is not None and len(df):
                anchor_close = float(df["close"].iloc[-1])
        anchor_date = _anchor_date(candle, signal)
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
        anchor_date = _anchor_date(candle, None)
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
        )
        self.pending_exits.append(pending)
        return pending

    def clear_pending(self) -> None:
        self.pending.clear()
        self.pending_exits.clear()

    # ── cache ────────────────────────────────────────────────────────────
    def _cache_key(self, pending: PendingConfirm | PendingExit) -> tuple:
        return (pending.symbol, pending.anchor_date, self.model, self.samples)

    def _cache_get(self, key: tuple) -> tuple[float, ...] | None:
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

    def _cache_put(self, key: tuple, returns: tuple[float, ...]) -> None:
        if self.cache_minutes <= 0:
            return
        self._cache[key] = (self._now() + self.cache_minutes * 60.0, returns)

    # ── worker ───────────────────────────────────────────────────────────
    def _fetch(
        self, symbols: list[str], as_of: str | None = None,
    ) -> dict[str, tuple[float, ...]]:
        if self._fetch_fn is not None:
            return self._fetch_fn(symbols)
        args = [
            "--symbols", *symbols,
            "--samples", str(self.samples),
            "--model", self.model,
        ]
        if as_of:
            # A replay date is a hard information boundary. Without it the
            # worker loads the live series and the vote sees future bars.
            args += ["--as-of", as_of]
        payload = run_worker(
            self.worker_script,
            args,
            timeout_s=self.timeout_s,
            label="confirm",
        )
        device = payload.get("device")
        if isinstance(device, str) and device:
            self.last_device = device
        rows = payload.get("results")
        if not isinstance(rows, list):
            raise KronosError("confirm worker returned no results")
        out: dict[str, tuple[float, ...]] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            symbol = str(row.get("symbol") or "").upper()
            if not symbol:
                continue
            returns: list[float] = []
            for value in row.get("returns") or []:
                if _finite(value):
                    returns.append(float(value))
            out[symbol] = tuple(returns)
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
        entries = list(self.pending)
        exits = list(self.pending_exits)
        self.pending.clear()
        self.pending_exits.clear()
        if not entries and not exits:
            return [], []

        all_items: list[PendingConfirm | PendingExit] = [*entries, *exits]
        resolved: dict[tuple, tuple[tuple[float, ...], bool]] = {}
        missing: dict[str, PendingConfirm | PendingExit] = {}
        for item in all_items:
            key = self._cache_key(item)
            cached = self._cache_get(key)
            if cached is not None:
                resolved[key] = (cached, True)
            else:
                missing.setdefault(item.symbol, item)

        symbols = list(missing)
        checked = symbols[: self.max_checks_per_scan]
        over_cap = set(symbols[self.max_checks_per_scan:])
        failures: dict[str, str] = {}
        fetched: dict[str, tuple[float, ...]] = {}
        if checked:
            # One call per replay date: the batch may mix entry and exit votes,
            # but every symbol must be cut at its own anchor bar.
            groups: dict[str | None, list[str]] = {}
            for symbol in checked:
                as_of = missing[symbol].anchor_date or None
                groups.setdefault(as_of, []).append(symbol)
            for as_of, group in groups.items():
                try:
                    result = self._fetch(group, as_of=as_of)
                    if not isinstance(result, dict):
                        for symbol in group:
                            failures[symbol] = "Kronos worker returned no payload"
                    else:
                        fetched.update(result)
                except KronosUnavailable as exc:
                    for symbol in group:
                        failures[symbol] = f"Kronos unavailable: {exc}"
                except KronosBusy as exc:
                    for symbol in group:
                        failures[symbol] = f"Kronos busy: {exc}"
                except KronosError as exc:
                    for symbol in group:
                        failures[symbol] = f"Kronos worker error: {exc}"
                except Exception as exc:
                    log.exception("Kronos confirm | worker call failed")
                    for symbol in group:
                        failures[symbol] = f"Kronos worker error: {exc}"
            for symbol in checked:
                returns = fetched.get(symbol)
                if not returns:
                    continue
                for item in all_items:
                    if item.symbol == symbol:
                        self._cache_put(self._cache_key(item), tuple(returns))

        def _miss_reason(item: PendingConfirm | PendingExit) -> str:
            if item.symbol in over_cap:
                return "Kronos check cap reached"
            if item.symbol in failures:
                return failures[item.symbol]
            return f"no forecast for {item.symbol}"

        entry_rows: list[tuple[object, object, ConfirmVerdict, dict]] = []
        for item in entries:
            hit = self._hit(item, resolved, fetched, over_cap)
            if hit is None:
                verdict = ConfirmVerdict("no_opinion", _miss_reason(item))
                summary = summarize(item.anchor_close, item.band_pct, ())
                evidence = _evidence(self, item, summary, verdict, cached=False)
            else:
                returns, cached = hit
                summary = summarize(item.anchor_close, item.band_pct, returns)
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
                            self, item, summary, verdict, cached=False, scope="position",
                        ))
                    )
                continue
            returns, cached = hit
            summary = summarize(item.anchor_close, item.band_pct, returns)
            for position in item.positions:
                verdict = evaluate(
                    summary, position.action, min_agree=self.exit_min_agree,
                )
                exit_rows.append(
                    (position, verdict, _evidence(
                        self, item, summary, verdict, cached=cached, scope="position",
                    ))
                )
        return entry_rows, exit_rows

    def _hit(self, item, resolved, fetched, over_cap):
        if self._cache_key(item) in resolved:
            return resolved[self._cache_key(item)]
        if item.symbol in over_cap:
            return None
        returns = fetched.get(item.symbol)
        if returns is None:
            return None
        return tuple(returns), False


def _anchor_date(candle, signal) -> str:
    ts = getattr(candle, "timestamp", None) or getattr(signal, "signal_bar_timestamp", None)
    if ts is None:
        return ""
    try:
        return pd.Timestamp(ts).date().isoformat()
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
) -> dict:
    return {
        "anchor_close": item.anchor_close,
        "atr_pct": item.atr_pct,
        "band_pct": item.band_pct,
        "returns": list(summary.returns),
        "votes": {"up": summary.up, "down": summary.down, "flat": summary.flat},
        "decision": verdict.decision,
        "reason": verdict.reason,
        "mode": gate.mode if scope == "entry" else gate.exit_mode,
        "scope": scope,
        "model": gate.model,
        "device": gate.last_device,
        "samples": gate.samples,
        "cached": bool(cached),
    }
