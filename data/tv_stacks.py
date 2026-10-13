"""Saved TradingView study stacks and the paper detector that trades them."""

from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from data.tv_studies import PRICE_SOURCES, TvStudyError, get_client
from indicators._math import wilder_atr
from indicators._signal import BaseIndicator, emit
from utils.logger import log

STACKS_PATH = Path("data/cache/tv_stacks.json")
_LOCK = threading.Lock()


def child_count(studies: list[dict]) -> int:
    count = 0
    for index, study in enumerate(studies):
        source = study.get("source") or {}
        kind = source.get("type")
        if kind == "price":
            if source.get("series") not in PRICE_SOURCES:
                raise ValueError("Price source must be open, high, low, close, hl2, hlc3, or ohlc4.")
            continue
        if kind != "study":
            raise ValueError("Source must be price or study.")
        if index == 0:
            raise ValueError("The first study must use a price source.")
        parent = int(source.get("study_index", -1))
        if parent < 0 or parent >= index:
            raise ValueError("A study source must point at an earlier study.")
        count += 1
    return count


def validate_stack(raw: dict, *, study_limit: int | None = None) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("Stack must be an object.")
    name = str(raw.get("name") or "").strip()
    if not name or len(name) > 80:
        raise ValueError("Stack name is required (80 characters max).")
    timeframe = raw.get("timeframe") or "1d"
    if timeframe not in ("1d", "1W"):
        raise ValueError("Timeframe must be 1d or 1W.")
    studies = raw.get("studies")
    if not isinstance(studies, list) or not studies:
        raise ValueError("A stack needs at least one study.")
    cleaned_studies = []
    for study in studies:
        if not isinstance(study, dict) or not str(study.get("id") or "").strip():
            raise ValueError("Each study needs an id from the catalog.")
        cleaned_studies.append({
            "id": str(study["id"]).strip(),
            "version": str(study.get("version") or "last"),
            "inputs": dict(study.get("inputs") or {}),
            "source": dict(study.get("source") or {"type": "price", "series": "close"}),
        })
    children = child_count(cleaned_studies)
    limit = get_client().study_limit if study_limit is None else study_limit
    if children > limit:
        raise ValueError(
            f"This stack has {children} indicator-on-indicator studies; the plan cap is {limit}."
        )
    signal = _validate_signal(raw.get("signal") or {}, len(cleaned_studies))
    stop = _validate_stop(raw.get("stop") or {}, len(cleaned_studies))
    stack_id = str(raw.get("id") or "").strip() or uuid.uuid4().hex
    return {
        "id": stack_id,
        "name": name,
        "timeframe": timeframe,
        "studies": cleaned_studies,
        "signal": signal,
        "stop": stop,
        "exit_on_opposite": bool(raw.get("exit_on_opposite", True)),
    }


def _validate_signal(signal: dict, n_studies: int) -> dict:
    kind = signal.get("kind")
    if kind not in ("cross", "threshold", "strategy"):
        raise ValueError("Signal kind must be cross, threshold, or strategy.")
    direction = signal.get("direction") or "above"
    if direction not in ("above", "below"):
        raise ValueError("Signal direction must be above or below.")
    study_index = int(signal.get("study_index") if signal.get("study_index") is not None else n_studies - 1)
    if study_index < 0 or study_index >= n_studies:
        raise ValueError("Signal study is outside the stack.")
    plot = str(signal.get("plot") or "plot_0")
    level = signal.get("level")
    other = signal.get("other")
    other_index = signal.get("other_study_index")
    if kind in ("cross", "threshold") and other is None and level is None:
        raise ValueError("A cross or threshold needs a level or another plot.")
    if other_index is not None:
        other_index = int(other_index)
        if other_index < 0 or other_index >= n_studies:
            raise ValueError("The other signal study is outside the stack.")
    return {
        "kind": kind,
        "plot": plot,
        "study_index": study_index,
        "other": None if other in (None, "") else str(other),
        "other_study_index": other_index,
        "level": None if level in (None, "") else float(level),
        "direction": direction,
    }


def _validate_stop(stop: dict, n_studies: int) -> dict:
    kind = stop.get("kind") or "percent"
    if kind not in ("plot", "percent", "atr"):
        raise ValueError("Stop kind must be plot, percent, or atr.")
    percent = float(stop.get("percent") if stop.get("percent") not in (None, "") else 2.0)
    if not 0 < percent <= 50:
        raise ValueError("Stop percent must be between 0 and 50.")
    atr_period = int(stop.get("atr_period") or 14)
    if not 1 <= atr_period <= 100:
        raise ValueError("ATR period must be from 1 to 100.")
    study_index = stop.get("study_index")
    if kind == "plot":
        study_index = int(0 if study_index is None else study_index)
        if study_index < 0 or study_index >= n_studies:
            raise ValueError("Stop study is outside the stack.")
    return {
        "kind": kind,
        "plot": str(stop.get("plot") or "plot_0"),
        "study_index": study_index,
        "percent": percent,
        "atr_period": atr_period,
    }


def load_stacks(path: Path | None = None) -> list[dict]:
    file = path or STACKS_PATH
    if not file.exists():
        return []
    data = json.loads(file.read_text(encoding="utf-8"))
    return list(data.get("stacks") or [])


def get_stack(stack_id: str, path: Path | None = None) -> dict | None:
    for stack in load_stacks(path):
        if stack.get("id") == stack_id:
            return stack
    return None


def stack_in_use(stack_id: str) -> bool:
    from core.paper_books import paper_books

    for book in paper_books.books.values():
        with book.lock:
            if book.running and book.tv_stack_id == stack_id:
                return True
    return False


def save_stack(raw: dict, path: Path | None = None) -> dict:
    stack = validate_stack(raw)
    file = path or STACKS_PATH
    with _LOCK:
        if path is None and stack_in_use(stack["id"]):
            raise ValueError("Stop the paper book using this stack before editing it.")
        stacks = [item for item in load_stacks(file) if item.get("id") != stack["id"]]
        stacks.append(stack)
        _write(file, stacks)
    return stack


def delete_stack(stack_id: str, path: Path | None = None) -> None:
    file = path or STACKS_PATH
    with _LOCK:
        if path is None and stack_in_use(stack_id):
            raise ValueError("Stop the paper book using this stack before deleting it.")
        stacks = [item for item in load_stacks(file) if item.get("id") != stack_id]
        _write(file, stacks)


def _write(file: Path, stacks: list[dict]) -> None:
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(json.dumps({"stacks": stacks}, indent=2), encoding="utf-8")


def require_stack_ready(stack_id: str) -> str | None:
    """None when this stack may start a paper book."""
    stack = get_stack(stack_id)
    if stack is None:
        return "Unknown TradingView stack."
    result = get_client().session()
    if result.get("ok"):
        return None
    if result.get("reason") == "disabled":
        return "TradingView studies are off — set TV_SESSIONID and TV_SESSIONID_SIGN."
    return "TradingView session expired — update TV_SESSIONID."


def _bar_day(value) -> datetime.date:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).date()
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(int(value), timezone.utc).date()
    text = str(value)
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc).date()


def _plot(study: dict, name: str, when: int) -> float | None:
    for row in study.get("plots") or []:
        if int(row["time"]) == when and name in row:
            value = row[name]
            if value is None or (isinstance(value, float) and not np.isfinite(value)):
                return None
            return float(value)
    return None


def _match_time(studies: list[dict], bar_time) -> int | None:
    want = _bar_day(bar_time)
    times = []
    for study in studies:
        times.extend(int(row["time"]) for row in study.get("plots") or [])
    for ts in sorted(set(times)):
        if _bar_day(ts) == want:
            return ts
    return None


def evaluate(stack: dict, fetched: dict, bar_time) -> dict | None:
    """BUY/SELL for the bar that matches ``bar_time``, or None."""
    studies = fetched.get("studies") or []
    when = _match_time(studies, bar_time)
    if when is None:
        return None
    signal = stack["signal"]
    kind = signal["kind"]
    if kind == "strategy":
        action = _strategy_action(studies[signal["study_index"]], when)
        if action is None:
            return None
        return {"action": action, "time": when}
    study = studies[signal["study_index"]]
    times = sorted(int(row["time"]) for row in study.get("plots") or [])
    if when not in times:
        return None
    previous = times[times.index(when) - 1] if times.index(when) else None
    if previous is None:
        return None
    last = _plot(study, signal["plot"], when)
    prev = _plot(study, signal["plot"], previous)
    if last is None or prev is None:
        return None
    if signal.get("other"):
        other_study = studies[int(signal["other_study_index"] if signal.get("other_study_index") is not None else signal["study_index"])]
        last_level = _plot(other_study, signal["other"], when)
        prev_level = _plot(other_study, signal["other"], previous)
        if last_level is None or prev_level is None:
            return None
    else:
        last_level = prev_level = float(signal["level"])
    direction = signal["direction"]
    crossed = (
        prev <= prev_level and last > last_level
        if direction == "above"
        else prev >= prev_level and last < last_level
    )
    if not crossed:
        return None
    return {"action": "BUY" if direction == "above" else "SELL", "time": when}


def _strategy_action(study: dict, when: int) -> str | None:
    for entry in study.get("strategy_entries") or []:
        if _bar_day(entry.get("time")) != _bar_day(when):
            continue
        action = str(entry.get("action") or "").upper()
        if action in ("BUY", "SELL"):
            return action
    return None


def holding_state(stack: dict, fetched: dict) -> tuple[str, float] | None:
    """Current side and protective stop while the rule still holds."""
    studies = fetched.get("studies") or []
    if not studies:
        return None
    signal = stack["signal"]
    if signal["kind"] == "strategy":
        return None
    study = studies[signal["study_index"]]
    rows = study.get("plots") or []
    if not rows:
        return None
    last = rows[-1]
    value = last.get(signal["plot"])
    if value is None:
        return None
    if signal.get("other"):
        other_study = studies[int(signal["other_study_index"] if signal.get("other_study_index") is not None else signal["study_index"])]
        other_rows = other_study.get("plots") or []
        level = other_rows[-1].get(signal["other"]) if other_rows else None
    else:
        level = signal.get("level")
    if level is None:
        return None
    if signal["direction"] == "above" and float(value) > float(level):
        action = "BUY"
    elif signal["direction"] == "below" and float(value) < float(level):
        action = "SELL"
    else:
        return None
    price = _close_at(fetched, int(last["time"]))
    if price is None:
        return None
    stop = _stop_price(stack, fetched, action, price, int(last["time"]))
    if stop is None:
        return None
    return action, stop


def _close_at(fetched: dict, when: int) -> float | None:
    for bar in fetched.get("bars") or []:
        if int(bar["time"]) == when:
            return float(bar["close"])
    return None


def _stop_price(stack: dict, fetched: dict, action: str, price: float, when: int) -> float | None:
    stop = stack["stop"]
    if stop["kind"] == "percent":
        distance = price * (float(stop["percent"]) / 100.0)
        return price - distance if action == "BUY" else price + distance
    if stop["kind"] == "plot":
        study = fetched["studies"][int(stop["study_index"])]
        level = _plot(study, stop["plot"], when)
        if level is None:
            return None
        if action == "BUY" and level < price:
            return level
        if action == "SELL" and level > price:
            return level
        return None
    bars = fetched.get("bars") or []
    if len(bars) < int(stop["atr_period"]) + 1:
        return None
    high = np.array([float(bar["high"]) for bar in bars], dtype=float)
    low = np.array([float(bar["low"]) for bar in bars], dtype=float)
    close = np.array([float(bar["close"]) for bar in bars], dtype=float)
    atr = float(wilder_atr(high, low, close, int(stop["atr_period"]))[-1])
    if not np.isfinite(atr) or atr <= 0:
        return None
    return price - atr if action == "BUY" else price + atr


def _frame(fetched: dict) -> pd.DataFrame | None:
    bars = fetched.get("bars") or []
    if not bars:
        return None
    index = [
        datetime.fromtimestamp(int(bar["time"]), timezone.utc) for bar in bars
    ]
    return pd.DataFrame(
        {
            "open": [float(bar["open"]) for bar in bars],
            "high": [float(bar["high"]) for bar in bars],
            "low": [float(bar["low"]) for bar in bars],
            "close": [float(bar["close"]) for bar in bars],
            "volume": [float(bar.get("volume") or 0) for bar in bars],
        },
        index=pd.DatetimeIndex(index),
    )


class TvStackIndicator(BaseIndicator):
    """One saved stack. Not discovered with the local indicators."""

    MIN_BARS = 2

    def __init__(self, stack: dict, fetch=None):
        self._stack = stack
        self._fetch = fetch or _live_fetch
        self.trails = bool(stack.get("exit_on_opposite", True))
        self.error = ""
        self._symbol = ""
        self._exchange = "NASDAQ"
        self._cache: dict | None = None
        self._cache_key: tuple | None = None

    @property
    def name(self) -> str:
        return self._stack["name"]

    @property
    def timeframes(self) -> list[str]:
        return [self._stack["timeframe"]]

    def bind(self, symbol: str, timeframe: str, exchange: str = "NASDAQ") -> None:
        self._symbol = symbol
        self._exchange = exchange or "NASDAQ"

    def prepare(self, symbol: str, exchange: str, bar_time) -> dict | None:
        key = (symbol, _bar_day(bar_time).isoformat())
        if self._cache_key == key and self._cache is not None:
            return self._cache
        try:
            fetched = self._fetch(symbol, exchange, self._stack)
        except TvStudyError as exc:
            self.error = str(exc)
            log.warning(f"TV stack | {symbol} {exc}")
            raise
        self.error = ""
        self._cache_key = key
        self._cache = fetched
        return fetched

    def analyze(self, snapshot, store):
        try:
            fetched = self.prepare(
            snapshot.symbol, self._exchange or _exchange(snapshot), snapshot.candle.timestamp,
        )
        except TvStudyError:
            return None
        if fetched is None:
            return None
        when = _match_time(fetched.get("studies") or [], snapshot.candle.timestamp)
        if when is None:
            log.info(
                f"TV stack | {snapshot.symbol} has no TradingView study bar "
                f"for {_bar_day(snapshot.candle.timestamp).isoformat()} — skipped"
            )
            return None
        fired = evaluate(self._stack, fetched, snapshot.candle.timestamp)
        if fired is None:
            return None
        frame = _frame({
            "bars": [bar for bar in (fetched.get("bars") or []) if int(bar["time"]) <= when],
        })
        if frame is None:
            return None
        price = float(frame["close"].iloc[-1])
        stop = _stop_price(self._stack, fetched, fired["action"], price, when)
        if stop is None:
            return None
        target = None
        if not self.trails:
            risk = abs(price - stop)
            if risk <= 0:
                return None
            target = price + 2 * risk if fired["action"] == "BUY" else price - 2 * risk
        signal = emit(
            self, frame, snapshot, action=fired["action"], stop=float(stop),
            target=target,
            notes=f"TradingView stack {self.name} {fired['action']}",
        )
        if signal is not None:
            signal.provenance = "tv_indicator"
        return signal

    def trail(self, df):
        if not self.trails:
            return None
        if self._symbol and df is not None and len(df):
            self.prepare(self._symbol, self._exchange, df.index[-1])
        if self._cache is None:
            raise TvStudyError("error", self.error or "TradingView study is not loaded.")
        return holding_state(self._stack, self._cache)


def _exchange(snapshot) -> str:
    return getattr(snapshot, "exchange", None) or "NASDAQ"


def _live_fetch(symbol: str, exchange: str, stack: dict) -> dict:
    return get_client().fetch(symbol, exchange, stack["timeframe"], stack["studies"])
