"""
data/stream_server.py — Paper trade stream server.

Replays historical daily bars over a WebSocket so paper trading can run when
US markets are closed. Each symbol gets its own cursor into its data. The
scanner advances the entire replay atomically once a complete scan cycle
finishes, so all symbols in a cycle see the same simulated market bar.

Bars come from GET /api/history (STOCKS_HISTORY_URL, default 33ai.edos.uk).
No CSV files and no local Postgres.

Optional start_date (YYYY-MM-DD) sets the initial cursor to the first bar
on/after that date. Without it, each tape starts near the end of its data
(last papertrade_stream_lookback_bars).

Protocol: client connects, sends one JSON object per request
    {"symbol": "AAPL", "timeframe": "1d"}
server replies with one JSON object
    {"candle": {...}, "history": [{...}, ...]}  # history ends at candle
or {"error": "...", "code": "no_data"|"asof_mismatch"} if the symbol
has no data / no bar on the pinned replay date.

Replay control:
    {"action": "pin_asof", "symbol": "AAPL"}  # pin control date to that tape
    {"action": "advance"}                     # move control date one session
"""

from __future__ import annotations

import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import websockets

from config import PATTERN_SCAN_HISTORY_BARS, settings
from data.stream_client import LOCAL_STREAM_WS
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


# Real daily bars are unix seconds (~1.7e9 in 2026). Unit tests use tiny
# integers as sequential bar ids — those must match exactly, not by calendar day.
_UNIX_TS_FLOOR = 1_000_000_000


def asof_key(ts: int) -> int | str:
    if ts >= _UNIX_TS_FLOOR:
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
    return int(ts)


def _stream_history_bars() -> int:
    """Bars sent with each snapshot — at least 30 days for pattern scans."""
    return max(int(settings.papertrade_stream_lookback_bars), PATTERN_SCAN_HISTORY_BARS)


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
                idx = max(0, len(rows) - _stream_history_bars())
            self.start = idx
        else:
            self.start = max(0, len(rows) - _stream_history_bars())
        self.cursor = self.start

    def advance(self) -> None:
        # Wrap back to this tape's own recent window, not row 0 of the whole
        # series — modulo-ing by len(rows) would snap the price back to the
        # earliest bar on file (a different era, often a different
        # split-adjustment level), producing a fake multi-year price cliff
        # mid-session instead of a smooth loop.
        self.cursor += 1
        if self.cursor >= len(self.rows):
            self.cursor = self.start

    def snapshot(self, asof_ts: int | None = None) -> dict | None:
        lookback = _stream_history_bars()
        if asof_ts is not None:
            idx = self.index_for_asof(asof_ts)
            if idx is None:
                return None
            end = idx + 1
            start = max(0, end - lookback)
            history = self.rows[start:end]
            return {"candle": history[-1], "history": history}
        end = self.cursor + 1
        start = max(0, end - lookback)
        history = self.rows[start:end]
        return {"candle": history[-1], "history": history}

    def index_for_asof(self, asof_ts: int) -> int | None:
        key = asof_key(asof_ts)
        idx = None
        for i, row in enumerate(self.rows):
            row_key = asof_key(row["timestamp"])
            if row_key == key:
                idx = i
            elif idx is not None:
                break
            elif row_key > key:
                break
        return idx

    def next_ts_after(self, asof_ts: int) -> int | None:
        key = asof_key(asof_ts)
        for row in self.rows:
            if asof_key(row["timestamp"]) > key:
                return int(row["timestamp"])
        return None


def _load_symbol_db(symbol: str, start_ts: int | None = None) -> list[dict] | None:
    """Load a symbol's daily history from GET /api/history.

    Near-end replay fetches only lookback bars; a start date uses after_ts.
    """
    lookback = _stream_history_bars()
    after_ts = None
    limit = None
    if start_ts is not None:
        after_ts = int(start_ts) - lookback * 86400 * 2
    else:
        limit = lookback
    try:
        from data.history import load_daily_tape_rows

        return load_daily_tape_rows(symbol, after_ts=after_ts, limit=limit)
    except Exception as exc:
        log.warning(f"StreamServer | history facade failed for {symbol}: {exc}")
        return None


class StreamServer:
    def __init__(self, start_date: str | None = None):
        self._start_ts = _parse_start_ts(
            start_date if start_date is not None else settings.papertrade_stream_start_date
        )
        self._tapes: dict[str, _SymbolTape] = {}
        self._asof_ts: int | None = None
        self._tape_lock = threading.Lock()
        self._load_pool = ThreadPoolExecutor(
            max_workers=4, thread_name_prefix="stream-hist",
        )

    def _tape_for(self, symbol: str) -> _SymbolTape | None:
        symbol = symbol.upper()
        with self._tape_lock:
            tape = self._tapes.get(symbol)
            if tape is not None:
                return tape
        rows = _load_symbol_db(symbol, start_ts=self._start_ts)
        with self._tape_lock:
            existing = self._tapes.get(symbol)
            if existing is not None:
                return existing
            if rows is None:
                return None
            tape = _SymbolTape(rows, start_ts=self._start_ts)
            self._tapes[symbol] = tape
            return tape

    async def _tape_for_async(self, symbol: str) -> _SymbolTape | None:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._load_pool, self._tape_for, symbol)

    def pin_asof(self, symbol: str) -> int | None:
        """Pin the replay control date to `symbol`'s current cursor bar."""
        tape = self._tape_for(symbol)
        if tape is None or not tape.rows:
            return None
        if self._asof_ts is None:
            self._asof_ts = int(tape.rows[tape.cursor]["timestamp"])
            log.info(
                f"StreamServer | pinned asof={asof_key(self._asof_ts)} "
                f"from {symbol.upper()}"
            )
        return self._asof_ts

    def advance(self) -> int:
        """Advance every loaded tape exactly one bar.

        This is deliberately synchronous and atomic from the server's event
        loop perspective. The scanner calls it only after it has completed a
        full universe scan, eliminating wall-clock drift when a scan takes
        longer than the old fixed 60-second stream interval.
        """
        if self._asof_ts is not None:
            nxt = None
            on_asof = 0
            for tape in self._tapes.values():
                if tape.index_for_asof(self._asof_ts) is None:
                    continue
                on_asof += 1
                cand = tape.next_ts_after(self._asof_ts)
                if cand is not None and (nxt is None or cand < nxt):
                    nxt = cand
            if nxt is None:
                log.debug("StreamServer | asof advance: no later bar among pinned tapes")
                return 0
            self._asof_ts = nxt
            log.debug(
                f"StreamServer | advanced asof to {asof_key(nxt)} "
                f"({on_asof} tape(s) were on the prior date)"
            )
            return on_asof

        advanced = 0
        for symbol, tape in list(self._tapes.items()):
            try:
                tape.advance()
                advanced += 1
            except Exception:
                log.exception(f"StreamServer | failed to advance {symbol} — dropping it")
                del self._tapes[symbol]
        log.debug(f"StreamServer | advanced {advanced} symbol(s) to next bar")
        return advanced

    async def _handle(self, ws) -> None:
        # A single bad request/symbol must never take the whole connection
        # down — every concurrent scanner worker shares this one server
        # process, so an unhandled exception here (previously: any history or
        # lookup error) closed that worker's socket with code 1011 and
        # surfaced as a scary-looking error for an otherwise fine symbol.
        async for raw in ws:
            try:
                try:
                    req = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    await ws.send(json.dumps({"error": "bad request"}))
                    continue

                # Replay control is owned by the scanner, not by a background
                # wall-clock timer. This keeps every symbol on one simulated
                # date even when a 1,000-symbol scan takes >60 seconds.
                if req.get("action") == "advance":
                    advanced = self.advance()
                    payload: dict = {"advanced": advanced}
                    if self._asof_ts is not None:
                        payload["asof"] = self._asof_ts
                        payload["asof_day"] = asof_key(self._asof_ts)
                    await ws.send(json.dumps(payload))
                    continue

                if req.get("action") == "pin_asof":
                    symbol = str(req.get("symbol") or "")
                    ts = (
                        await asyncio.get_running_loop().run_in_executor(
                            self._load_pool, self.pin_asof, symbol,
                        )
                        if symbol else None
                    )
                    if ts is None:
                        await ws.send(json.dumps({
                            "error": f"no data for {symbol}",
                            "code": "no_data",
                        }))
                    else:
                        await ws.send(json.dumps({
                            "asof": ts,
                            "asof_day": asof_key(ts),
                        }))
                    continue

                try:
                    symbol = req["symbol"]
                except (KeyError, TypeError):
                    await ws.send(json.dumps({"error": "bad request"}))
                    continue
                tape = await self._tape_for_async(symbol)
                if tape is None:
                    await ws.send(json.dumps({
                        "error": f"no data for {symbol}",
                        "code": "no_data",
                    }))
                    continue
                snap = tape.snapshot(self._asof_ts)
                if snap is None:
                    await ws.send(json.dumps({
                        "error": (
                            f"asof mismatch: {symbol} has no bar on "
                            f"{asof_key(self._asof_ts) if self._asof_ts is not None else '?'}"
                        ),
                        "code": "asof_mismatch",
                    }))
                    continue
                await ws.send(json.dumps(snap))
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
        from data.history import DEFAULT_STOCKS_HISTORY_URL

        source = (
            f"history API {(settings.stocks_history_url or DEFAULT_STOCKS_HISTORY_URL).rstrip('/')}"
        )
        async with websockets.serve(self._handle, host, port, **LOCAL_STREAM_WS):
            log.info(
                f"Paper trade stream server | {host}:{port} | "
                f"source={source} | "
                f"start={start_note} | scanner-controlled atomic advancement"
            )
            await asyncio.Future()


async def run_stream_server(start_date: str | None = None) -> None:
    from data.history import enable_ui_web_history

    # Child process of --web/--ui: inject https://33ai.edos.uk when URL is empty.
    enable_ui_web_history()
    await StreamServer(start_date=start_date).run()
