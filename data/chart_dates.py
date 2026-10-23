"""Session-date adapters for charts; trading candle timestamps stay unchanged."""

from __future__ import annotations

import pandas as pd


def chart_session_index(index: pd.DatetimeIndex, session_tz: str) -> pd.DatetimeIndex:
    """Keep UTC-midnight session labels and convert real prints to market dates."""
    if index.tz is None:
        return index.normalize()
    local_labels = index.tz_localize(None)
    utc_times = index.tz_convert("UTC").tz_localize(None)
    # Date-only history/replay rows are represented as midnight UTC. Treat
    # each one as a session label, even when mixed with exchange-open prints.
    date_labels = (local_labels == local_labels.normalize()) & (local_labels == utc_times)
    market_dates = index.tz_convert(session_tz).tz_localize(None).normalize()
    return market_dates.where(~date_labels, local_labels.normalize())


def chart_session_day(when, session_tz: str) -> pd.Timestamp | None:
    if when is None:
        return None
    try:
        stamp = pd.Timestamp(when)
    except (ValueError, TypeError):
        return None
    if pd.isna(stamp):
        return None
    return chart_session_index(pd.DatetimeIndex([stamp]), session_tz)[0]
