"""Read-only daily-history adapter with explicit failure and date semantics."""
from __future__ import annotations

import math
from functools import lru_cache
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd

from core.market import get_market, is_ph_holiday, is_us_holiday, last_closed_session_date, ph_history_symbol


class HistoryUnavailable(RuntimeError):
    pass


def top_market_symbols(market: str, n: int) -> list[str]:
    """Top liquid names for this market that also have daily history.

    Ranking matches the paper book: US dollar volume with the ADV floor, PH
    peso turnover. Screener names missing from history are skipped. They are
    not replaced by stale listings further down the history catalog.
    """
    from data.tv_client import TVClient

    from utils.logger import log

    profile = get_market(market)
    rows = TVClient.fetch_universe_cached(int(n), profile.id)
    ranked: list[str] = []
    for symbol, _exchange in rows:
        key = ph_history_symbol(symbol) if profile.id == "ph" else str(symbol).strip().upper()
        if key and key not in ranked:
            ranked.append(key)
    if not ranked:
        raise HistoryUnavailable(f"No top symbols were returned for the {profile.id} market")
    listed = set(available_symbols(profile.id, []))
    chosen = [symbol for symbol in ranked if symbol in listed]
    skipped = len(ranked) - len(chosen)
    if skipped:
        log.info(
            f"Experiments | skipped {skipped} of the top {len(ranked)} {profile.id} "
            "symbols with no daily history"
        )
    if not chosen:
        raise HistoryUnavailable(
            f"None of the top {n} {profile.id} symbols have daily history")
    return chosen


def resolve_universe(market: str, selected: list[str] | None = None,
                     n_symbols: int | None = None) -> list[str]:
    """Explicit tickers win. Otherwise a count is the paper top-N universe."""
    chosen = list(selected or [])
    if chosen or not n_symbols:
        return available_symbols(market, chosen)
    return top_market_symbols(market, int(n_symbols))


def available_symbols(market: str, selected: list[str]) -> list[str]:
    from data.history import list_history_symbols

    metadata = list_history_symbols(market=market)
    if not metadata:
        # The facade currently folds enumeration errors into []; never claim
        # a successful zero-symbol research run when this happens.
        raise HistoryUnavailable("History symbol enumeration returned no symbols or failed; check the history service")
    symbols = sorted({str(row["symbol"]).strip().upper() for row in metadata
                      if row.get("symbol") and row.get("market", market) == market})
    if market == "ph":
        symbols = sorted({ph_history_symbol(symbol) for symbol in symbols})
    if selected:
        chosen = {ph_history_symbol(s) if market == "ph" else s for s in selected}
        missing = chosen - set(symbols)
        if missing:
            raise ValueError("Symbols not available in the selected history market: " + ", ".join(sorted(missing)))
        symbols = [s for s in symbols if s in chosen]
    if not symbols:
        raise HistoryUnavailable("No symbols are available for this market")
    return symbols


_PRICE_KEYS = ("open", "high", "low", "close", "volume")
_SESSION_OPEN = time(9, 30)


def _valid_ohlcv(bar: dict) -> bool:
    o, h, l, c, v = (bar[key] for key in _PRICE_KEYS)
    return min(o, h, l, c) > 0 and v >= 0 and l <= min(o, c) <= max(o, c) <= h


@lru_cache(maxsize=65536)
def is_session(market: str, day: date) -> bool:
    holiday = is_ph_holiday if market == "ph" else is_us_holiday
    return day.weekday() < 5 and not holiday(day)


@lru_cache(maxsize=131072)
def shift_session(market: str, day: date, steps: int) -> date:
    direction = 1 if steps > 0 else -1
    for _ in range(abs(steps)):
        day += timedelta(days=direction)
        while not is_session(market, day):
            day += timedelta(days=direction)
    return day


def evaluable_session(market: str, end: date) -> date:
    """Latest session a daily tape can contain for this request.

    History stops at the last closed cash session, so an end date of today
    (or a later date) is not a missing bar. A mark before the returned
    session is stale. A mark on it is the last traded close.
    """
    final = end if is_session(market, end) else shift_session(market, end, -1)
    closed = last_closed_session_date(market)
    if not is_session(market, closed):
        closed = shift_session(market, closed, -1)
    return min(final, closed)


def normalize_bars(rows: list[dict], market: str, end_date: date,
                   *, closed_through: date | None = None,
                   issues: list[dict] | None = None) -> list[dict]:
    """Admit declared daily bars or the history feed's regular-open daily stamp."""
    zone = ZoneInfo(get_market(market).session_tz)
    cutoff = min(end_date, closed_through or last_closed_session_date(market))
    grouped: dict[date, dict[int, dict]] = {}
    seen: dict[int, dict] = {}
    invalid = []
    for raw in rows:
        ts = int(raw["ts"])
        local = datetime.fromtimestamp(ts, timezone.utc).astimezone(zone)
        session = date.fromisoformat(str(raw["date"])) if raw.get("date") else local.date()
        if session > cutoff:
            continue
        numbers = {key: float(raw[key]) for key in _PRICE_KEYS}
        if not all(math.isfinite(value) for value in numbers.values()):
            raise ValueError(f"Non-finite OHLCV on {session}")
        bar = {"ts": ts, "date": session.isoformat(), **numbers}
        interval = raw.get("interval", raw.get("timeframe"))
        reason = None
        if not is_session(market, session):
            reason = "outside exchange session"
        elif interval is not None and str(interval).lower() not in {"1d", "d", "daily"}:
            reason = "non-daily interval"
        elif interval is None and (local.date() != session or local.time() != _SESSION_OPEN):
            reason = "unverified daily interval"
        elif not _valid_ohlcv(bar):
            reason = "invalid OHLCV"
            invalid.append(session)
        if reason:
            if issues is not None:
                issues.append({"date": session.isoformat(), "ts": ts, "reason": reason})
            continue
        if ts in seen and seen[ts] != bar:
            raise ValueError(f"Conflicting daily rows for session {session}")
        seen[ts] = bar
        grouped.setdefault(session, {})[ts] = bar
    kept = []
    for session, by_ts in sorted(grouped.items()):
        candidates = list(by_ts.values())
        if any(any(row[k] != candidates[0][k] for k in _PRICE_KEYS) for row in candidates[1:]):
            if issues is not None:
                issues.extend({"date": session.isoformat(), "ts": ts, "reason": "conflicting daily prints"} for ts in by_ts)
            continue
        kept.append(min(candidates, key=lambda row: row["ts"]))
    if invalid and not kept:
        raise ValueError(f"Invalid OHLCV on {min(invalid)}")
    return kept


def raw_symbol(symbol: str, market: str, end_date: date) -> list[dict]:
    from data.history import load_daily_bars

    raw = load_daily_bars(symbol, market=market)
    if raw is None:
        raise HistoryUnavailable(f"Daily history request failed for {symbol}")
    return raw


def load_symbol(symbol: str, market: str, end_date: date) -> list[dict]:
    return normalize_bars(raw_symbol(symbol, market, end_date), market, end_date)


def frame(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=["ts", "date", "open", "high", "low", "close", "volume"])
