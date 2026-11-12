"""Entry permissions shared by the scanner and every paper entry path."""

from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import pandas as pd

from config import settings

if TYPE_CHECKING:
    from data.ohlcv_store import OHLCVStore
    from patterns.base_pattern import TradeSignal


def resolve_dii_gate_mode(mode: str | None, *, paper: bool) -> str:
    """Freeze a run's explicit choice, falling back to configured defaults."""
    if mode is None:
        mode = ("shadow" if settings.dii_gate_shadow else
                settings.paper_dii_gate_mode if paper else "filter")
    mode = mode.strip().lower()
    if mode not in ("shadow", "filter"):
        raise ValueError(f"Unsupported DII gate mode: {mode}")
    return mode


def describe_short_entry_rejection(action: str, *, indicators_only: bool = False) -> str | None:
    """Apply the pattern-short switch without interfering with position exits."""
    if action == "SELL" and not indicators_only and not settings.short_entries_enabled:
        return (
            "Short entries are disabled (SHORT_ENTRIES_ENABLED=false). "
            "Set it true to re-enable SELL/short pattern entries."
        )
    return None


def describe_buy_entry_rejection(signal: TradeSignal, store: OHLCVStore | None, *, market: str | None,
                                 as_of: datetime | None = None,
                                 at_open: bool = False) -> str | None:
    """Enforce daily bulls confirmation for every new long, including replays.

    Read the daily tape even for weekly signals. Restrict it to the observation
    date and, at an opening fill, exclude that session's full historical bar.
    Date-only UTC history labels retain their exchange session date.
    """
    if signal.action != "BUY":
        return None
    from analysis.pattern_dii import describe_dii_bulls_uptrend_rejection
    from core.market import get_market, is_closed_session_bar, is_ph_holiday, is_us_holiday, last_closed_session_date
    from data.chart_dates import chart_session_day

    session_tz = get_market(market).session_tz
    cutoff = chart_session_day(as_of or signal.signal_bar_timestamp, session_tz)
    if cutoff is None:
        return "DII bulls uptrend unavailable: BUY requires a dated daily decision."
    if store is None or not callable(getattr(store, "copy_candles", None)):
        return describe_dii_bulls_uptrend_rejection(None)
    daily = {}
    for candle in store.copy_candles(signal.symbol, "1d"):
        day = chart_session_day(candle.timestamp, session_tz)
        if day is None or day > cutoff or (at_open and day == cutoff):
            continue
        if not is_closed_session_bar("1d", day.to_pydatetime(), market=market):
            continue
        daily[day] = candle
    through = cutoff - pd.Timedelta(days=int(at_open))
    expected = min(
        last_closed_session_date(market, datetime.combine(through.date(), time.max, ZoneInfo(session_tz))),
        last_closed_session_date(market),
    )
    holiday = is_ph_holiday if get_market(market).id == "ph" else is_us_holiday
    while expected.weekday() >= 5 or holiday(expected):
        expected -= timedelta(days=1)
    if daily and max(daily).date() < expected:
        return (f"DII bulls uptrend unavailable: completed daily session {expected.isoformat()} "
                "is missing before BUY.")
    # Count each exchange session once; a reprint cannot supply another day.
    frame = pd.DataFrame.from_records(
        [(c.open, c.high, c.low, c.close, c.volume)
         for _, c in sorted(daily.items())],
        columns=["open", "high", "low", "close", "volume"],
    )
    return describe_dii_bulls_uptrend_rejection(frame)
