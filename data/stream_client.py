"""
data/stream_client.py — Client for the paper trade stream server
(main.py --papertrade-stream). Drop-in replacement for TVClient inside
MarketScanner: same mcp_session()/fetch_snapshot() shape, backed by the
history replay server instead of TradingView.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import websockets

from config import settings
from data.ohlcv_store import OHLCVStore
from data.tv_client import MarketSnapshot, OHLCVCandle
from utils.logger import log

# Localhost replay: protocol pings are harmful. Default ping_timeout=20s closes
# every scanner worker while the server is still fetching 33ai history.
# 16MiB covers a first-fill batch of ~50 symbols × 420 lookback bars.
LOCAL_STREAM_WS = {
    "ping_interval": None,
    "ping_timeout": None,
    "close_timeout": 5,
    "max_size": 16 * 1024 * 1024,
}


class FetchSkip(Exception):
    """Stream snapshot skipped — not a transport failure."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def _candle_from_row(row: dict) -> OHLCVCandle:
    return OHLCVCandle(
        open=row["open"], high=row["high"], low=row["low"],
        close=row["close"], volume=row["volume"],
        timestamp=datetime.fromtimestamp(row["timestamp"], tz=timezone.utc),
    )


class StreamClient:
    """Same call shape as TVClient, backed by the paper trade stream server."""

    def __init__(self, host: str | None = None, port: int | None = None):
        self._host = host or settings.papertrade_stream_host
        self._port = port or settings.papertrade_stream_port
        # (symbol, timeframe) keys that already have lookback in OHLCVStore.
        # Later scans request candle-only deltas.
        self._warm: set[tuple[str, str]] = set()
        self.snapshot_batch_size = int(settings.papertrade_stream_batch_size)

    @asynccontextmanager
    async def mcp_session(self):
        uri = f"ws://{self._host}:{self._port}"
        async with websockets.connect(uri, **LOCAL_STREAM_WS) as ws:
            yield ws

    def _needs_history(self, symbol: str, timeframe: str, store: OHLCVStore | None) -> bool:
        key = (symbol.upper(), timeframe)
        if key in self._warm:
            return False
        if store is not None and store.available(symbol, timeframe) > 0:
            self._warm.add(key)
            return False
        return True

    def _snapshot_from_reply(
        self,
        symbol: str,
        timeframe: str,
        reply: dict | None,
        store: OHLCVStore | None,
    ) -> MarketSnapshot:
        if not reply or "error" in reply:
            code = str((reply or {}).get("code") or "no_data")
            message = str((reply or {}).get("error") or f"no data for {symbol}")
            if code in ("asof_mismatch", "history_unavailable"):
                log.debug(f"StreamClient | {symbol}: {message}")
            else:
                log.warning(f"StreamClient | {symbol}: {message}")
            raise FetchSkip(code, message)

        candle = _candle_from_row(reply["candle"])
        if store is not None:
            history_rows = reply.get("history") or []
            if history_rows:
                store.replace_all(
                    symbol, timeframe, [_candle_from_row(r) for r in history_rows],
                )
            else:
                store.apply_candle(symbol, timeframe, candle)
            self._warm.add((symbol.upper(), timeframe))

        return MarketSnapshot(
            symbol=symbol,
            timeframe=timeframe,
            timestamp=datetime.now(timezone.utc),
            candle=candle,
            indicators={},
            summary={},
            oscillators={},
            moving_avgs={},
        )

    async def fetch_snapshot(
        self,
        symbol: str,
        timeframe: str = "1d",
        store: OHLCVStore | None = None,
        mcp_session=None,
    ) -> MarketSnapshot | None:
        results = await self.fetch_snapshots(
            [symbol], timeframe, store=store, mcp_session=mcp_session,
        )
        if not results:
            return None
        result = results.get(symbol)
        if isinstance(result, FetchSkip):
            raise result
        return result

    async def fetch_snapshots(
        self,
        symbols: list[str],
        timeframe: str = "1d",
        store: OHLCVStore | None = None,
        mcp_session=None,
    ) -> dict[str, MarketSnapshot | FetchSkip | None]:
        """One WS round-trip for a batch. Full history only for cold symbols."""
        if mcp_session is None:
            log.error("StreamClient | fetch_snapshots called without a session")
            return {s: None for s in symbols}
        if not symbols:
            return {}
        history_for = [
            s for s in symbols if self._needs_history(s, timeframe, store)
        ]
        try:
            await mcp_session.send(json.dumps({
                "action": "snapshots",
                "symbols": symbols,
                "timeframe": timeframe,
                "history_for": history_for,
            }))
            reply = json.loads(await mcp_session.recv())
        except websockets.exceptions.ConnectionClosed as exc:
            log.warning(f"StreamClient | batch: connection closed ({exc})")
            return {s: None for s in symbols}
        except Exception as exc:
            log.error(f"StreamClient | batch request failed: {exc}")
            return {s: None for s in symbols}

        payloads = reply.get("results") if isinstance(reply, dict) else None
        if not isinstance(payloads, dict):
            log.error("StreamClient | batch reply missing results")
            return {s: None for s in symbols}

        out: dict[str, MarketSnapshot | FetchSkip | None] = {}
        for symbol in symbols:
            payload = payloads.get(symbol) or payloads.get(symbol.upper())
            try:
                out[symbol] = self._snapshot_from_reply(
                    symbol, timeframe, payload, store,
                )
            except FetchSkip as exc:
                out[symbol] = exc
        return out

    async def preload_universe(self, symbols: list[str], mcp_session=None) -> dict:
        """Ask the stream server to fetch every tape before the first scan."""
        if mcp_session is None:
            try:
                async with self.mcp_session() as ws:
                    return await self.preload_universe(symbols, ws)
            except Exception as exc:
                log.error(f"StreamClient | preload failed: {exc}")
                return {}
        try:
            await mcp_session.send(json.dumps({
                "action": "preload",
                "symbols": list(symbols),
            }))
            reply = json.loads(await mcp_session.recv())
        except Exception as exc:
            log.error(f"StreamClient | preload failed: {exc}")
            return {}
        if not isinstance(reply, dict) or "error" in reply:
            log.warning(f"StreamClient | preload rejected: {reply}")
            return {}
        return reply

    async def pin_replay_asof(self, symbol: str, mcp_session=None) -> str | None:
        """Pin the stream server's control date to `symbol`'s current bar."""
        if mcp_session is None:
            log.error("StreamClient | pin_replay_asof called without a session")
            return None
        try:
            await mcp_session.send(json.dumps({"action": "pin_asof", "symbol": symbol}))
            reply = json.loads(await mcp_session.recv())
        except Exception as exc:
            log.error(f"StreamClient | pin_asof failed: {exc}")
            return None
        if "error" in reply:
            code = str(reply.get("code") or "")
            if code == "history_unavailable":
                log.debug(f"StreamClient | pin_asof {symbol}: {reply['error']}")
            else:
                log.warning(f"StreamClient | pin_asof {symbol}: {reply['error']}")
            return None
        asof_day = reply.get("asof_day")
        return str(asof_day) if asof_day is not None else None

    async def advance_replay(self, mcp_session=None) -> bool:
        """Advance the historical replay by exactly one bar after a full scan."""
        if mcp_session is None:
            # Use a short-lived control connection when the scanner's worker
            # sessions are not available. This method is normally called with
            # the scanner's first persistent session.
            try:
                async with self.mcp_session() as ws:
                    await ws.send(json.dumps({"action": "advance"}))
                    reply = json.loads(await ws.recv())
                    return "error" not in reply
            except Exception as exc:
                log.error(f"StreamClient | replay advance failed: {exc}")
                return False
        try:
            await mcp_session.send(json.dumps({"action": "advance"}))
            reply = json.loads(await mcp_session.recv())
            if "error" in reply:
                log.error(f"StreamClient | replay advance failed: {reply['error']}")
                return False
            return True
        except Exception as exc:
            log.error(f"StreamClient | replay advance failed: {exc}")
            return False
