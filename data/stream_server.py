"""
data/stream_server.py — Paper trade stream server.

Replays historical daily CSVs from settings.papertrade_stream_dir over a
WebSocket so paper trading can run when US markets are closed. Each symbol
gets its own cursor into its CSV; a background loop advances every cursor by
one bar every settings.papertrade_stream_interval_seconds, wrapping to the
start once a CSV is exhausted so a session can run indefinitely.

Optional start_date (YYYY-MM-DD) sets the initial cursor to the first bar
on/after that date. Without it, each tape starts near the end of its CSV
(last papertrade_stream_lookback_bars).

CSV layout: <dir>/<FIRST_LETTER>/<SYMBOL>.csv with columns
low,open,volume,high,close,timestamp (unix seconds, one row per day).

Protocol: client connects, sends one JSON object per request
    {"symbol": "AAPL", "timeframe": "1d"}
server replies with one JSON object
    {"candle": {...}, "history": [{...}, ...]}  # history ends at candle
or {"error": "..."} if the symbol has no CSV.
"""

from __future__ import annotations

import asyncio
import csv
import json
from datetime import datetime, timezone
from pathlib import Path

import websockets

from config import settings
from utils.logger import log


def _parse_start_ts(start_date: str | None) -> int | None:
    if not start_date:
        return None
    try:
        dt = datetime.strptime(start_date.strip(), "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        log.warning(f"StreamServer | invalid start_date {start_date!r} — ignoring")
        return None
    return int(dt.timestamp())


class _SymbolTape:
    def __init__(self, rows: list[dict], start_ts: int | None = None):
        self.rows = rows  # sorted by timestamp ascending
        if start_ts is not None:
            idx = next(
                (i for i, r in enumerate(rows) if r["timestamp"] >= start_ts),
                None,
            )
            if idx is None:
                # All bars are before start_ts — fall back to near-end window.
                idx = max(0, len(rows) - settings.papertrade_stream_lookback_bars)
            self.start = idx
        else:
            self.start = max(0, len(rows) - settings.papertrade_stream_lookback_bars)
        self.cursor = self.start

    def advance(self) -> None:
        # Wrap back to this tape's own recent window, not row 0 of the whole
        # CSV — modulo-ing by len(rows) would snap the price back to the
        # earliest bar on file (a different era, often a different
        # split-adjustment level), producing a fake multi-year price cliff
        # mid-session instead of a smooth loop.
        self.cursor += 1
        if self.cursor >= len(self.rows):
            self.cursor = self.start

    def snapshot(self) -> dict:
        lookback = settings.papertrade_stream_lookback_bars
        end = self.cursor + 1
        start = max(0, end - lookback)
        history = self.rows[start:end]
        return {"candle": history[-1], "history": history}


def _load_symbol_csv(base_dir: Path, symbol: str) -> list[dict] | None:
    path = base_dir / symbol[0].upper() / f"{symbol.upper()}.csv"
    if not path.exists():
        return None
    rows = []
    with path.open(newline="") as f:
        for row in csv.DictReader(f):
            # Some tickers have gap rows with only a timestamp and blank
            # OHLCV fields (e.g. a halted/delisted day) — skip rather than
            # fail the whole symbol.
            try:
                rows.append({
                    "open": float(row["open"]),
                    "high": float(row["high"]),
                    "low": float(row["low"]),
                    "close": float(row["close"]),
                    "volume": float(row["volume"]),
                    "timestamp": int(float(row["timestamp"])),
                })
            except (ValueError, KeyError):
                continue
    rows.sort(key=lambda r: r["timestamp"])
    return rows or None


class StreamServer:
    def __init__(self, base_dir: str | None = None, start_date: str | None = None):
        self._base_dir = Path(base_dir or settings.papertrade_stream_dir)
        self._start_ts = _parse_start_ts(
            start_date if start_date is not None else settings.papertrade_stream_start_date
        )
        self._tapes: dict[str, _SymbolTape] = {}

    def _tape_for(self, symbol: str) -> _SymbolTape | None:
        symbol = symbol.upper()
        tape = self._tapes.get(symbol)
        if tape is not None:
            return tape
        rows = _load_symbol_csv(self._base_dir, symbol)
        if rows is None:
            return None
        tape = _SymbolTape(rows, start_ts=self._start_ts)
        self._tapes[symbol] = tape
        return tape

    async def _advance_loop(self) -> None:
        while True:
            await asyncio.sleep(settings.papertrade_stream_interval_seconds)
            for symbol, tape in list(self._tapes.items()):
                try:
                    tape.advance()
                except Exception:
                    log.exception(f"StreamServer | failed to advance {symbol} — dropping it")
                    del self._tapes[symbol]
            log.debug(f"StreamServer | advanced {len(self._tapes)} symbol(s) to next bar")

    async def _handle(self, ws) -> None:
        # A single bad request/symbol must never take the whole connection
        # down — every concurrent scanner worker shares this one server
        # process, so an unhandled exception here (previously: any CSV or
        # lookup error) closed that worker's socket with code 1011 and
        # surfaced as a scary-looking error for an otherwise fine symbol.
        async for raw in ws:
            try:
                try:
                    req = json.loads(raw)
                    symbol = req["symbol"]
                except (json.JSONDecodeError, KeyError, TypeError):
                    await ws.send(json.dumps({"error": "bad request"}))
                    continue
                tape = self._tape_for(symbol)
                if tape is None:
                    await ws.send(json.dumps({"error": f"no data for {symbol}"}))
                    continue
                await ws.send(json.dumps(tape.snapshot()))
            except Exception as exc:
                log.exception(f"StreamServer | request handling failed: {raw!r}")
                await ws.send(json.dumps({"error": f"server error: {exc}"}))

    async def run(self) -> None:
        host, port = settings.papertrade_stream_host, settings.papertrade_stream_port
        start_note = (
            datetime.fromtimestamp(self._start_ts, tz=timezone.utc).strftime("%Y-%m-%d")
            if self._start_ts is not None
            else "near-end lookback"
        )
        async with websockets.serve(self._handle, host, port):
            log.info(
                f"Paper trade stream server | {host}:{port} | "
                f"source={self._base_dir} | start={start_note} | advancing every "
                f"{settings.papertrade_stream_interval_seconds}s"
            )
            await self._advance_loop()


async def run_stream_server(start_date: str | None = None) -> None:
    await StreamServer(start_date=start_date).run()
