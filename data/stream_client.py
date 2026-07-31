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
        async with websockets.connect(uri) as ws:
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
        except Exception as exc:
            log.error(f"StreamClient | {symbol} request failed: {exc}")
            return None

        if "error" in reply:
            log.warning(f"StreamClient | {symbol}: {reply['error']}")
            return None

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
