"""
data/stream_client.py — Client for the paper trade stream server
(main.py --papertrade-stream). Drop-in replacement for TVClient inside
MarketScanner: same mcp_session()/fetch_snapshot() shape, backed by the
CSV replay server instead of TradingView.
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
LOCAL_STREAM_WS = {
    "ping_interval": None,
    "ping_timeout": None,
    "close_timeout": 5,
    "max_size": 8 * 1024 * 1024,
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

    @asynccontextmanager
    async def mcp_session(self):
        uri = f"ws://{self._host}:{self._port}"
        async with websockets.connect(uri, **LOCAL_STREAM_WS) as ws:
            yield ws

    async def fetch_snapshot(
        self,
        symbol: str,
        timeframe: str = "1d",
        store: OHLCVStore | None = None,
        mcp_session=None,
    ) -> MarketSnapshot | None:
        if mcp_session is None:
            log.error("StreamClient | fetch_snapshot called without a session")
            return None
        try:
            await mcp_session.send(json.dumps({"symbol": symbol, "timeframe": timeframe}))
            reply = json.loads(await mcp_session.recv())
        except websockets.exceptions.ConnectionClosed as exc:
            log.warning(f"StreamClient | {symbol}: connection closed ({exc})")
            return None
        except Exception as exc:
            log.error(f"StreamClient | {symbol} request failed: {exc}")
            return None

        if "error" in reply:
            code = str(reply.get("code") or "no_data")
            message = str(reply["error"])
            if code == "asof_mismatch":
                log.debug(f"StreamClient | {symbol}: {message}")
            else:
                log.warning(f"StreamClient | {symbol}: {message}")
            raise FetchSkip(code, message)

        candle = _candle_from_row(reply["candle"])
        if store is not None:
            history = [_candle_from_row(r) for r in reply["history"]]
            store.replace_all(symbol, timeframe, history)

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
