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
    {"symbol": "AAPL", "timeframe": "1d", "history": true|false}
server replies with one JSON object
    {"candle": {...}, "history": [{...}, ...]}  # history ends at candle
    {"candle": {...}}                            # delta: history omitted
or {"error": "...", "code": "no_data"|"asof_mismatch"|"history_unavailable"}
if the symbol has no bars, no bar on the pinned replay date, or the
history API timed out (retry next scan — do not treat as dead).

Batch:
    {"action": "snapshots", "symbols": ["AAPL", ...],
     "history_for": ["AAPL"]}  # those names get full lookback; others candle-only
    → {"results": {"AAPL": {candle, history?}|{error, code}, ...}}

Preload (fetch all tapes before the first scan):
    {"action": "preload", "symbols": ["AAPL", ...]}
    → {"loaded": N, "empty": N, "unavailable": N, "symbols": N}

Replay control:
    {"action": "pin_asof", "symbol": "AAPL"}  # pin control date to that tape
    {"action": "advance"}                     # move control date one session
"""

from __future__ import annotations

import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import websockets

from config import PATTERN_SCAN_HISTORY_BARS, settings
from data.stream_client import LOCAL_STREAM_WS
from utils.logger import log

# Real daily bars are unix seconds (~1.7e9 in 2026). Unit tests use tiny
# integers as sequential bar ids — those must match exactly, not by calendar day.
_UNIX_TS_FLOOR = 1_000_000_000

# US fixed-date holidays that close the NYSE cash session when they fall on a
# weekday. Floating holidays (MLK, Thanksgiving, …) are not listed — the
# pin_asof skip below still covers weekends + these fixed dates, which is
# what pinned paper replay to empty scans on 2026-01-01 (New Year's).
_US_FIXED_HOLIDAYS = {(1, 1), (6, 19), (7, 4), (12, 25)}


def _session_date_for_ts(ts: int, market: str | None) -> date:
    from core.market import get_market

    tz = ZoneInfo(get_market(market).session_tz)
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(tz).date()


def _is_likely_cash_session(day: date, market: str | None) -> bool:
    """True when the listing's cash session is expected to print a daily bar."""
    if day.weekday() >= 5:
        return False
    from core.market import MARKET_PH, get_market, is_ph_holiday

    if get_market(market).id == MARKET_PH:
        return not is_ph_holiday(day)
    return (day.month, day.day) not in _US_FIXED_HOLIDAYS


def _roll_forward_session_day(day: date, market: str | None) -> date:
    for _ in range(14):
        if _is_likely_cash_session(day, market):
            return day
        day += timedelta(days=1)
    return day


def _parse_start_ts(
    start_date: str | None, market: str | None = None,
) -> int | None:
    if not start_date:
        return None
    try:
        day = datetime.strptime(start_date.strip(), "%Y-%m-%d").date()
    except ValueError:
        log.warning(f"StreamServer | invalid start_date {start_date!r} — ignoring")
        return None
    rolled = _roll_forward_session_day(day, market)
    if rolled != day:
        log.info(
            f"StreamServer | start_date {day.isoformat()} is a weekend/holiday "
            f"— rolling forward to {rolled.isoformat()}"
        )
    # Midnight UTC of the session calendar day. Session-tz conversion happens
    # when classifying bars; this matches the prior YYYY-MM-DD → UTC parse.
    dt = datetime(rolled.year, rolled.month, rolled.day, tzinfo=timezone.utc)
    return int(dt.timestamp())


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

    def snapshot(
        self, asof_ts: int | None = None, *, include_history: bool = True,
    ) -> dict | None:
        lookback = _stream_history_bars()
        if asof_ts is not None:
            idx = self.index_for_asof(asof_ts)
            if idx is None:
                return None
            end = idx + 1
        else:
            end = self.cursor + 1
        start = max(0, end - lookback)
        history = self.rows[start:end]
        if not history:
            return None
        payload: dict = {"candle": history[-1]}
        if include_history:
            payload["history"] = history
        return payload

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


def _load_symbol_db(
    symbol: str,
    start_ts: int | None = None,
    market: str | None = None,
) -> list[dict] | None:
    """Load a symbol's daily history from GET /api/history.

    Near-end replay fetches only lookback bars; a start date uses after_ts.
    `market=ph` maps BDO → BDO.PS so the stream does not 404 / hit US SM.
    """
    lookback = _stream_history_bars()
    after_ts = None
    if start_ts is not None:
        after_ts = int(start_ts) - lookback * 86400 * 2
        now_ts = int(datetime.now(timezone.utc).timestamp())
        from_start = max(0, now_ts - int(start_ts)) // 86400
        # Always send a LIMIT so 33ai does an index-bounded fetch instead of
        # dumping every bar after after_ts (that stalled uvicorn + swap).
        limit = lookback + from_start + 40
    else:
        limit = lookback
    try:
        from data.history import load_daily_tape_rows

        return load_daily_tape_rows(
            symbol, after_ts=after_ts, limit=limit, market=market,
        )
    except Exception as exc:
        log.warning(f"StreamServer | history facade failed for {symbol}: {exc}")
        return None


class StreamServer:
    def __init__(self, start_date: str | None = None, market: str | None = None):
        from core.market import resolve_market_id

        self._market = resolve_market_id(market)
        self._start_ts = _parse_start_ts(
            start_date if start_date is not None else settings.papertrade_stream_start_date,
            market=self._market,
        )
        self._tapes: dict[str, _SymbolTape] = {}
        self._known_empty: set[str] = set()
        self._asof_ts: int | None = None
        self._tape_lock = threading.Lock()
        pool_n = max(4, int(settings.papertrade_stream_preload_workers))
        self._load_pool = ThreadPoolExecutor(
            max_workers=pool_n, thread_name_prefix="stream-hist",
        )

    def _tape_lookup(self, symbol: str) -> tuple[_SymbolTape | None, str | None]:
        """Load-or-cache a tape.

        Returns (tape, None) on success. On miss: (None, "no_data") for an
        empty/404 symbol, (None, "history_unavailable") for a transport
        failure — the latter must not be cached, so the next scan retries.
        """
        symbol = symbol.upper()
        with self._tape_lock:
            tape = self._tapes.get(symbol)
            if tape is not None:
                return tape, None
            if symbol in self._known_empty:
                return None, "no_data"
        rows = _load_symbol_db(
            symbol, start_ts=self._start_ts, market=self._market,
        )
        with self._tape_lock:
            existing = self._tapes.get(symbol)
            if existing is not None:
                return existing, None
            if rows is None:
                return None, "history_unavailable"
            if not rows:
                self._known_empty.add(symbol)
                return None, "no_data"
            tape = _SymbolTape(rows, start_ts=self._start_ts)
            self._tapes[symbol] = tape
            return tape, None

    def _tape_for(self, symbol: str) -> _SymbolTape | None:
        tape, _err = self._tape_lookup(symbol)
        return tape

    async def _tape_lookup_async(
        self, symbol: str,
    ) -> tuple[_SymbolTape | None, str | None]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._load_pool, self._tape_lookup, symbol)

    def _skip_message(self, symbol: str, code: str) -> dict:
        symbol = (symbol or "").upper()
        if code == "asof_mismatch":
            asof = asof_key(self._asof_ts) if self._asof_ts is not None else "?"
            return {
                "error": f"asof mismatch: {symbol} has no bar on {asof}",
                "code": "asof_mismatch",
            }
        if code == "history_unavailable":
            return {
                "error": f"history unavailable for {symbol}",
                "code": "history_unavailable",
            }
        return {"error": f"no data for {symbol}", "code": "no_data"}

    def _snapshot_message(self, symbol: str, include_history: bool = True) -> dict:
        tape, err = self._tape_lookup(symbol)
        if tape is None:
            return self._skip_message(symbol, err or "no_data")
        snap = tape.snapshot(self._asof_ts, include_history=include_history)
        if snap is None:
            return self._skip_message(symbol, "asof_mismatch")
        return snap

    def _snapshot_if_cached(self, symbol: str, include_history: bool) -> dict | None:
        """In-memory tape or known-empty. None = still needs a history fetch."""
        symbol = symbol.upper()
        with self._tape_lock:
            tape = self._tapes.get(symbol)
            if tape is None:
                if symbol in self._known_empty:
                    return self._skip_message(symbol, "no_data")
                return None
        snap = tape.snapshot(self._asof_ts, include_history=include_history)
        if snap is None:
            return self._skip_message(symbol, "asof_mismatch")
        return snap

    def snapshots_payload(
        self,
        symbols: list[str],
        history_for: set[str] | None = None,
    ) -> dict:
        """Batch reply; cache hits stay on this thread (no HTTP)."""
        want_hist = {str(s).upper().strip() for s in (history_for or ()) if s}
        results: dict[str, dict] = {}
        for raw in symbols:
            symbol = str(raw or "").upper().strip()
            if not symbol:
                continue
            include_history = symbol in want_hist
            cached = self._snapshot_if_cached(symbol, include_history)
            results[symbol] = (
                cached
                if cached is not None
                else self._snapshot_message(symbol, include_history)
            )
        return {"results": results}

    def preload_symbols(self, symbols: list[str]) -> dict:
        """Fetch every tape up front so the first scan is in-memory."""
        from data.history_client import inflight_slots

        uniq: list[str] = []
        seen: set[str] = set()
        for raw in symbols:
            symbol = str(raw or "").upper().strip()
            if not symbol or symbol in seen:
                continue
            seen.add(symbol)
            uniq.append(symbol)
        workers = max(4, int(settings.papertrade_stream_preload_workers))
        with inflight_slots(workers):
            with ThreadPoolExecutor(
                max_workers=workers, thread_name_prefix="stream-preload",
            ) as pool:
                pairs = list(pool.map(self._tape_lookup, uniq))
        loaded = empty = unavailable = 0
        for tape, err in pairs:
            if tape is not None:
                loaded += 1
            elif err == "no_data":
                empty += 1
            else:
                unavailable += 1
        log.info(
            f"StreamServer | preload {len(uniq)} symbols: "
            f"loaded={loaded} empty={empty} unavailable={unavailable}"
        )
        return {
            "loaded": loaded,
            "empty": empty,
            "unavailable": unavailable,
            "symbols": len(uniq),
        }

    def _history_for(self, req: dict, symbols: list[str]) -> set[str]:
        if req.get("history_for"):
            return {str(s).upper().strip() for s in req["history_for"] if s}
        if req.get("history") is False:
            return set()
        if req.get("history") is True:
            return set(symbols)
        # Old clients omit the flag — send full lookback.
        return set(symbols)

    async def _snapshots_reply(self, req: dict) -> dict:
        raw_symbols = req.get("symbols") or []
        symbols = [str(s).upper().strip() for s in raw_symbols if str(s).strip()]
        want_hist = self._history_for(req, symbols)
        results: dict[str, dict] = {}
        misses: list[str] = []
        for symbol in symbols:
            cached = self._snapshot_if_cached(symbol, symbol in want_hist)
            if cached is None:
                misses.append(symbol)
            else:
                results[symbol] = cached
        if misses:
            loop = asyncio.get_running_loop()
            loaded = await asyncio.gather(*[
                loop.run_in_executor(
                    self._load_pool,
                    self._snapshot_message,
                    symbol,
                    symbol in want_hist,
                )
                for symbol in misses
            ])
            for symbol, payload in zip(misses, loaded):
                results[symbol] = payload
        return {"results": results}

    def pin_asof(self, symbol: str) -> int | None:
        ts, _err = self._pin_asof(symbol)
        return ts

    def _pin_asof(self, symbol: str) -> tuple[int | None, str | None]:
        """Pin the replay control date to `symbol`'s current cursor bar.

        Skips weekend / fixed-holiday prints on the control tape. Illiquid
        names sometimes carry a New Year's print that would otherwise pin
        the whole universe to a day most symbols have no bar for — paper
        then reports sim_days=1 with zero signals while drowning in
        asof_mismatch skips.
        """
        if self._asof_ts is not None:
            return self._asof_ts, None
        tape, err = self._tape_lookup(symbol)
        if tape is None or not tape.rows:
            return None, err or "no_data"
        idx = tape.cursor
        chosen_ts: int | None = None
        while idx < len(tape.rows):
            ts = int(tape.rows[idx]["timestamp"])
            if ts < _UNIX_TS_FLOOR:
                # Synthetic unit-test bar ids — keep exact match semantics.
                chosen_ts = ts
                break
            day = _session_date_for_ts(ts, self._market)
            if _is_likely_cash_session(day, self._market):
                chosen_ts = ts
                break
            idx += 1
        if chosen_ts is None:
            chosen_ts = int(tape.rows[tape.cursor]["timestamp"])
            log.warning(
                f"StreamServer | pin_asof {symbol.upper()}: no weekday/session "
                f"bar on/after cursor — falling back to "
                f"{asof_key(chosen_ts)}"
            )
        else:
            tape.cursor = idx
        self._asof_ts = chosen_ts
        log.info(
            f"StreamServer | pinned asof={asof_key(self._asof_ts)} "
            f"from {symbol.upper()}"
        )
        return self._asof_ts, None

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
                    ts, err = (
                        await asyncio.get_running_loop().run_in_executor(
                            self._load_pool, self._pin_asof, symbol,
                        )
                        if symbol else (None, "no_data")
                    )
                    if ts is None:
                        await ws.send(json.dumps(
                            self._skip_message(symbol, err or "no_data"),
                        ))
                    else:
                        await ws.send(json.dumps({
                            "asof": ts,
                            "asof_day": asof_key(ts),
                        }))
                    continue

                if req.get("action") == "preload":
                    summary = await asyncio.to_thread(
                        self.preload_symbols, req.get("symbols") or [],
                    )
                    await ws.send(json.dumps(summary))
                    continue

                if req.get("action") == "snapshots":
                    await ws.send(json.dumps(await self._snapshots_reply(req)))
                    continue

                try:
                    symbol = req["symbol"]
                except (KeyError, TypeError):
                    await ws.send(json.dumps({"error": "bad request"}))
                    continue
                include_history = req.get("history", True) is not False
                cached = self._snapshot_if_cached(str(symbol), include_history)
                if cached is not None:
                    await ws.send(json.dumps(cached))
                    continue
                loop = asyncio.get_running_loop()
                payload = await loop.run_in_executor(
                    self._load_pool,
                    self._snapshot_message,
                    str(symbol),
                    include_history,
                )
                await ws.send(json.dumps(payload))
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
                f"market={self._market} | source={source} | "
                f"start={start_note} | scanner-controlled atomic advancement"
            )
            await asyncio.Future()


async def run_stream_server(
    start_date: str | None = None, market: str | None = None,
) -> None:
    from data.history import enable_ui_web_history

    # Child process of --web/--ui: inject https://33ai.edos.uk when URL is empty.
    enable_ui_web_history()
    await StreamServer(start_date=start_date, market=market).run()
