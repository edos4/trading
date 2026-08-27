"""Chart-explorer pattern scan.

`BasePattern.analyze()` is a *trigger* detector: it returns a signal only when
the latest bar *is* the entry bar. That is required for bar-by-bar backtests
and the live scanner (otherwise the same setup would re-fire every day).

A human looking at a chart is asking a different question: is there a pattern
that formed over the last month and is almost complete / recently triggered?
This helper walks the last PATTERN_SCAN_HISTORY_BARS closes so explorer/UI
are not stuck looking at "day 1 of the signal" (today only) and coming back
empty. The candle list must include those 30 days plus whatever prefix
`MIN_BARS` needs — analyze() sees the full prefix, not a 30-bar stub.
"""

from __future__ import annotations

from datetime import datetime, timezone

from data.ohlcv_store import OHLCVStore
from data.tv_client import MarketSnapshot, OHLCVCandle
from config import PATTERN_SCAN_HISTORY_BARS
from patterns.base_pattern import BasePattern, TradeSignal
from utils.logger import log


def _snapshot(symbol: str, timeframe: str, candle: OHLCVCandle) -> MarketSnapshot:
    ts = candle.timestamp or datetime.now(timezone.utc)
    return MarketSnapshot(
        symbol=symbol,
        timeframe=timeframe,
        timestamp=ts,
        candle=candle,
        indicators={},
        summary={"RECOMMENDATION": "NEUTRAL"},
        oscillators={},
        moving_avgs={},
    )


def latest_signals_over_lookback(
    patterns: list[BasePattern],
    symbol: str,
    timeframe: str,
    candles: list[OHLCVCandle],
    *,
    lookback: int = PATTERN_SCAN_HISTORY_BARS,
    session_tz: str,
) -> list[TradeSignal]:
    """Newest signal per pattern across the last `lookback` bars, if any.

    Refuses to run unless the series has at least PATTERN_SCAN_HISTORY_BARS
    so forming setups have a month of prior closes.
    """
    if not candles:
        return []
    lookback = max(int(lookback), PATTERN_SCAN_HISTORY_BARS)

    n = len(candles)
    if n < PATTERN_SCAN_HISTORY_BARS:
        return []
    start = max(0, n - lookback)
    out: list[TradeSignal] = []

    for pattern in patterns:
        if timeframe not in pattern.timeframes:
            continue
        min_bars = int(getattr(pattern, "MIN_BARS", 2) or 2)
        if n < min_bars:
            continue

        scratch = OHLCVStore(window=max(n, min_bars), session_tz=session_tz)
        seed_end = max(start, min_bars - 1)
        scratch.replace_all(symbol, timeframe, candles[:seed_end])

        found: TradeSignal | None = None
        age = 0
        for i in range(seed_end, n):
            candle = candles[i]
            scratch.append_candle(symbol, timeframe, candle)
            try:
                sig = pattern.analyze(_snapshot(symbol, timeframe, candle), scratch)
            except Exception as exc:
                log.warning(
                    f"Chart scan | {pattern.name} failed on {symbol} {timeframe}: {exc}"
                )
                continue
            if sig is None:
                continue
            found = sig
            age = n - 1 - i

        if found is None:
            continue
        if age > 0:
            found.notes = (
                f"{found.notes} | chart-scan: triggered {age} bar(s) ago "
                f"(lookback={lookback})"
            ).strip(" |")
        out.append(found)
    return out
