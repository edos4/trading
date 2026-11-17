"""Read-only daily-history adapter with explicit failure and date semantics."""
from __future__ import annotations

import math
from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

import pandas as pd

from core.market import get_market, last_closed_session_date, ph_history_symbol


class HistoryUnavailable(RuntimeError):
    pass


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


def _session_clock(ts: int, zone: ZoneInfo) -> time:
    return datetime.fromtimestamp(ts, timezone.utc).astimezone(zone).time()


def _resolve_session(session: date, bars: list[dict], zone: ZoneInfo) -> dict | None:
    """Keep one daily bar. Same-timestamp disagreements stay an error.

    A 09:30 session bar is the daily print used by the current history
    loader. A local-midnight companion is a month-label or older stamp, and
    other extra prints are intraday rows stored on the same session. Those
    are dropped. A session with no valid daily bar is omitted.
    """
    by_ts: dict[int, dict] = {}
    for bar in bars:
        previous = by_ts.get(bar["ts"])
        if previous is not None and previous != bar:
            raise ValueError(f"Conflicting daily rows for session {session}")
        by_ts[bar["ts"]] = bar
    valid = [bar for bar in by_ts.values() if _valid_ohlcv(bar)]
    if not valid:
        return None
    unique: list[dict] = []
    for bar in valid:
        match = next((item for item in unique if all(item[key] == bar[key] for key in _PRICE_KEYS)), None)
        if match is None:
            unique.append(bar)
        elif _session_clock(bar["ts"], zone) == _SESSION_OPEN:
            unique[unique.index(match)] = bar
    if len(unique) == 1:
        return unique[0]
    opens = [bar for bar in unique if _session_clock(bar["ts"], zone) == _SESSION_OPEN]
    if len(opens) == 1:
        return opens[0]
    return None


def normalize_bars(rows: list[dict], market: str, end_date: date,
                   *, closed_through: date | None = None,
                   issues: list[dict] | None = None) -> list[dict]:
    """Retain warm-up and cut at end-date.

    Non-finite values and two different bars with the same timestamp still
    fail the symbol. An invalid print, or several incompatible prints that
    are not the 09:30 daily bar, drops that session only.
    """
    zone = ZoneInfo(get_market(market).session_tz)
    cutoff = min(end_date, closed_through or last_closed_session_date(market))
    grouped: dict[date, list[dict]] = {}
    for raw in rows:
        ts = int(raw["ts"])
        session = date.fromisoformat(str(raw["date"])) if raw.get("date") else datetime.fromtimestamp(ts, timezone.utc).astimezone(zone).date()
        if session > cutoff:
            continue
        numbers = {key: float(raw[key]) for key in _PRICE_KEYS}
        if not all(math.isfinite(value) for value in numbers.values()):
            raise ValueError(f"Non-finite OHLCV on {session}")
        grouped.setdefault(session, []).append({"ts": ts, "date": session.isoformat(), **numbers})
    kept, identities = [], set()
    for session in sorted(grouped):
        chosen = _resolve_session(session, grouped[session], zone)
        if chosen is None:
            if issues is not None:
                issues.append({"date": session.isoformat(), "reason": "unusable session omitted"})
            continue
        if chosen["ts"] in identities:
            raise ValueError(f"Duplicate bar identity {chosen['ts']}")
        identities.add(chosen["ts"])
        kept.append(chosen)
    if grouped and not kept:
        raise ValueError(f"Invalid OHLCV on {min(grouped)}")
    return kept


def load_symbol(symbol: str, market: str, end_date: date) -> list[dict]:
    from data.history import load_daily_bars
    from utils.logger import log

    # No newest-N limit: it can entirely miss an old requested interval.
    raw = load_daily_bars(symbol, market=market)
    if raw is None:
        raise HistoryUnavailable(f"Daily history request failed for {symbol}")
    issues: list[dict] = []
    rows = normalize_bars(raw, market, end_date, issues=issues)
    if issues:
        log.warning(f"Experiments | {symbol}: omitted {len(issues)} unusable session(s); remaining daily bars kept")
    return rows


def frame(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=["ts", "date", "open", "high", "low", "close", "volume"])
