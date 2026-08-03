"""
data/tv_client.py — Market data via TradingView MCP + TradingView screener.

OHLCV comes from tradingview-screener (TradingView API). Indicators and
recommendations come from tradingview-mcp-server (coin_analysis). History
builds in OHLCVStore across scan cycles — no Yahoo Finance.
"""

from __future__ import annotations

import json
import shutil
import sys
import threading
import time
import urllib.request
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pandas as pd
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from tradingview_screener import Query, col

from config import settings
from utils.logger import log

if TYPE_CHECKING:
    from data.ohlcv_store import OHLCVStore


# ── Screener rate limit + 429 retry (process-wide) ───────────────────────────
# TradingView's public scanner has no documented quota. Unpaced concurrent
# POSTs (~10+ /s) return HTTP 429 with an empty body mid-universe scan.
_screener_lock = threading.Lock()
_screener_next_allowed = 0.0


def _is_screener_429(exc: BaseException) -> bool:
    response = getattr(exc, "response", None)
    if response is not None and getattr(response, "status_code", None) == 429:
        return True
    text = str(exc)
    return "429" in text and "Too Many Requests" in text


def _throttle_screener() -> None:
    """Enforce min gap between scanner POSTs across all threads/workers."""
    global _screener_next_allowed
    interval = settings.tv_screener_min_interval_seconds
    if interval <= 0:
        return
    with _screener_lock:
        now = time.monotonic()
        wait = _screener_next_allowed - now
        _screener_next_allowed = max(now, _screener_next_allowed) + interval
    if wait > 0:
        time.sleep(wait)


def _get_scanner_data(query: Query, **kwargs: Any) -> tuple[int, pd.DataFrame]:
    """Rate-limited Query.get_scanner_data with exponential backoff on 429."""
    max_retries = settings.tv_screener_max_retries
    backoff = settings.tv_screener_retry_backoff_seconds
    last_exc: BaseException | None = None
    for attempt in range(max_retries + 1):
        _throttle_screener()
        try:
            return query.get_scanner_data(**kwargs)
        except Exception as exc:
            last_exc = exc
            if not _is_screener_429(exc) or attempt >= max_retries:
                raise
            sleep_for = backoff * (2**attempt)
            log.warning(
                f"TVClient | Screener 429 (attempt {attempt + 1}/{max_retries + 1}) "
                f"— sleeping {sleep_for:.1f}s"
            )
            time.sleep(sleep_for)
    assert last_exc is not None
    raise last_exc


def reset_screener_throttle_for_tests() -> None:
    """Reset throttle clock — tests only."""
    global _screener_next_allowed
    with _screener_lock:
        _screener_next_allowed = 0.0


def _resolve_mcp_command() -> str:
    venv_bin = Path(sys.executable).parent / "tradingview-mcp"
    if venv_bin.exists():
        return str(venv_bin)
    found = shutil.which("tradingview-mcp")
    if found:
        return found
    raise FileNotFoundError(
        "tradingview-mcp not found. Install with: pip install tradingview-mcp-server"
    )


MCP_COMMAND = _resolve_mcp_command()

MCP_TIMEFRAME_MAP: dict[str, str] = {
    "1m": "15m",
    "5m": "15m",
    "15m": "15m",
    "30m": "30m",
    "1h": "1h",
    "2h": "4h",
    "4h": "4h",
    "1d": "1D",
    "1W": "1W",
    "1M": "1M",
}

# Screener column names per bot timeframe (TradingView screener API)
SCREENER_FIELDS: dict[str, tuple[str, str, str, str, str]] = {
    "1d": ("open", "high", "low", "close", "volume"),
    "1W": ("open|1W", "high|1W", "low|1W", "close|1W", "volume|1W"),
}

# Daily history via screener bar offsets: close[0] = latest, close[1] = prior day, ...
# Screener offsets cap at ~3 bars; bulk history comes from the public chart API.

_CHART_API = "https://query1.finance.yahoo.com/v8/finance/chart"
_CHART_UA = "trading-bot/2.0 chart-history"

# interval, range, max bars to keep
# Daily uses 2y so Kronos gate can take LOOKBACK=400 (1y ≈ 252 bars is too short).
_CHART_SPECS: dict[str, tuple[str, str, int]] = {
    "1d": ("1d", "2y", 512),
    "1W": ("1wk", "5y", 65),
}

SYMBOL_EXCHANGE_OVERRIDES: dict[str, str] = {
    "SPY": "AMEX",
    "IVV": "AMEX",
    "VOO": "AMEX",
}

# ── Top-symbols screener cache ─────────────────────────────────────────────
# The screener query for "top N by market cap" is re-run on every backtest
# invocation (CLI and UI); caching it to disk avoids a network round-trip
# each time within the TTL window.
_SYMBOLS_CACHE_DIR = Path("data/cache")
_SYMBOLS_CACHE_TTL_SECONDS = 6 * 3600


@dataclass
class OHLCVCandle:
    open: float
    high: float
    low: float
    close: float
    volume: float
    timestamp: datetime | None = None


@dataclass
class MarketSnapshot:
    symbol: str
    timeframe: str
    timestamp: datetime
    candle: OHLCVCandle
    indicators: dict
    summary: dict
    oscillators: dict
    moving_avgs: dict

    def indicator(self, key: str, default=None):
        return self.indicators.get(key, default)

    @property
    def tv_recommendation(self) -> str:
        return self.summary.get("RECOMMENDATION", "NEUTRAL")

    def as_series(self) -> pd.Series:
        return pd.Series(self.indicators)


class TVClient:
    """Fetch snapshots through TradingView MCP + screener."""

    @staticmethod
    def fetch_top_symbols(n: int = 100, screener: str = "america") -> list[str]:
        """Return top N symbols by market cap from TradingView screener."""
        q = (
            Query()
            .select("name")
            .set_markets(screener)
            .order_by("market_cap_basic", ascending=False)
            .limit(n)
        )
        _, df = _get_scanner_data(q)
        if df is None or df.empty:
            return []
        return df["name"].tolist()

    @staticmethod
    def fetch_top_symbols_with_exchanges(
        n: int = 100, screener: str = "america"
    ) -> list[tuple[str, str]]:
        """Return top N (symbol, exchange) pairs by market cap."""
        q = (
            Query()
            .select("name", "exchange")
            .set_markets(screener)
            .order_by("market_cap_basic", ascending=False)
            .limit(n)
        )
        _, df = _get_scanner_data(q)
        if df is None or df.empty:
            return []
        return [
            (str(row["name"]).upper(), str(row["exchange"]).upper())
            for _, row in df.iterrows()
            if row.get("name") and row.get("exchange")
        ]

    @staticmethod
    def fetch_top_symbols_with_exchanges_cached(
        n: int = 100,
        screener: str = "america",
        ttl_seconds: int = _SYMBOLS_CACHE_TTL_SECONDS,
    ) -> list[tuple[str, str]]:
        """Same as fetch_top_symbols_with_exchanges, cached to data/cache/.

        Backtests (CLI and UI) re-run the same top-N screener query on every
        invocation; this reads the last fetch from disk instead when it's
        still fresh, and re-fetches + saves when it's missing or stale.
        """
        path = _SYMBOLS_CACHE_DIR / f"symbols_{screener}_{n}.json"
        if path.exists():
            age = time.time() - path.stat().st_mtime
            if age < ttl_seconds:
                try:
                    rows = json.loads(path.read_text(encoding="utf-8"))
                    return [(s, e) for s, e in rows]
                except Exception:
                    pass
        rows = TVClient.fetch_top_symbols_with_exchanges(n, screener)
        if rows:
            _SYMBOLS_CACHE_DIR.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(rows), encoding="utf-8")
        return rows

    def __init__(
        self,
        screener: str,
        exchange: str,
        exchange_overrides: dict[str, str] | None = None,
    ):
        self._screener = screener
        self._default_exchange = exchange
        self._exchange_overrides = {
            symbol.upper(): exchange.upper()
            for symbol, exchange in (exchange_overrides or {}).items()
        }

    @asynccontextmanager
    async def mcp_session(self):
        import subprocess as _subprocess
        server_params = StdioServerParameters(command=MCP_COMMAND, args=[])
        async with stdio_client(server_params, errlog=_subprocess.DEVNULL) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session

    def _exchange_for(self, symbol: str) -> str:
        key = symbol.upper()
        return (
            self._exchange_overrides.get(key)
            or settings.symbol_exchange_overrides.get(key)
            or SYMBOL_EXCHANGE_OVERRIDES.get(key)
            or self._default_exchange
        )

    def _max_history_offsets(self, timeframe: str) -> int:
        if timeframe == "1d":
            return settings.tv_history_days
        return 1

    async def fetch_snapshot(
        self,
        symbol: str,
        timeframe: str = "1h",
        store: OHLCVStore | None = None,
        mcp_session: ClientSession | None = None,
    ) -> MarketSnapshot | None:
        if timeframe not in MCP_TIMEFRAME_MAP:
            log.error(
                f"TVClient | Unknown timeframe '{timeframe}'. "
                f"Valid: {list(MCP_TIMEFRAME_MAP.keys())}"
            )
            return None

        exchange = self._exchange_for(symbol)
        try:
            candle = self._fetch_candle_screener(symbol, exchange, timeframe)
            if candle is None:
                log.error(f"TVClient | No screener data for {symbol} {timeframe}")
                return None

            if store is not None:
                history = self._fetch_history_screener(
                    symbol, exchange, timeframe, latest=candle
                )
                if history:
                    store.replace_all(symbol, timeframe, history)
                else:
                    store.push(
                        MarketSnapshot(
                            symbol=symbol,
                            timeframe=timeframe,
                            timestamp=datetime.now(timezone.utc),
                            candle=candle,
                            indicators={},
                            summary={},
                            oscillators={},
                            moving_avgs={},
                        )
                    )

            mcp_tf = MCP_TIMEFRAME_MAP[timeframe]
            mcp_data = await self._fetch_mcp_analysis(
                symbol, exchange, mcp_tf, mcp_session=mcp_session
            )
            indicators, summary, oscillators, moving_avgs = self._normalize_analysis(
                candle, mcp_data
            )

            snapshot = MarketSnapshot(
                symbol=symbol,
                timeframe=timeframe,
                timestamp=datetime.now(timezone.utc),
                candle=candle,
                indicators=indicators,
                summary=summary,
                oscillators=oscillators,
                moving_avgs=moving_avgs,
            )
            log.debug(
                f"TVClient | {symbol} {timeframe} → "
                f"close={snapshot.candle.close:.4f} TV={snapshot.tv_recommendation}"
            )
            return snapshot

        except Exception as exc:
            log.error(f"TVClient | Failed to fetch {symbol} {timeframe}: {exc}")
            return None

    def _fetch_candle_screener(
        self, symbol: str, exchange: str, timeframe: str
    ) -> OHLCVCandle | None:
        fields = SCREENER_FIELDS.get(timeframe)
        if fields is None:
            log.warning(
                f"TVClient | No screener field map for {timeframe} — using daily columns"
            )
            fields = SCREENER_FIELDS["1d"]

        open_k, high_k, low_k, close_k, vol_k = fields
        select_cols = ["name", open_k, high_k, low_k, close_k, vol_k]

        query = (
            Query()
            .select(*select_cols)
            .set_markets(self._screener)
            .where(col("name") == symbol.upper(), col("exchange") == exchange.upper())
        )
        _, df = _get_scanner_data(query)
        if df is None or df.empty:
            return None

        row = df.iloc[0]

        def _f(key: str) -> float | None:
            val = row.get(key)
            if val is None or (isinstance(val, float) and pd.isna(val)):
                return None
            return float(val)

        close = _f(close_k)
        if close is None:
            return None

        return OHLCVCandle(
            open=_f(open_k) or close,
            high=_f(high_k) or close,
            low=_f(low_k) or close,
            close=close,
            volume=_f(vol_k) or 0.0,
        )

    def _fetch_history_chart(self, symbol: str, timeframe: str) -> list[OHLCVCandle]:
        """Fetch OHLCV history from the public chart API (~2Y daily / ~5Y weekly)."""
        spec = _CHART_SPECS.get(timeframe)
        if spec is None:
            return []

        interval, range_, default_max = spec
        max_bars = settings.tv_history_days if timeframe == "1d" else default_max
        url = f"{_CHART_API}/{symbol.upper()}?interval={interval}&range={range_}"
        req = urllib.request.Request(url, headers={"User-Agent": _CHART_UA})

        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            log.warning(
                f"TVClient | Chart history failed for {symbol} {timeframe}: {exc}"
            )
            return []

        try:
            result = payload["chart"]["result"][0]
            timestamps = result["timestamp"]
            quote = result["indicators"]["quote"][0]
            adjclose = result["indicators"].get("adjclose", [{}])[0].get("adjclose", [])
        except (KeyError, IndexError, TypeError) as exc:
            log.warning(
                f"TVClient | Chart history parse failed for {symbol} {timeframe}: {exc}"
            )
            return []

        candles: list[OHLCVCandle] = []
        for i, ts in enumerate(timestamps):
            o = quote["open"][i]
            h = quote["high"][i]
            l = quote["low"][i]
            c = quote["close"][i]
            v = quote["volume"][i]
            if None in (o, h, l, c):
                continue
            adj = adjclose[i] if i < len(adjclose) else None
            if adj is not None and c:
                factor = float(adj) / float(c)
                o, h, l, c = (
                    float(o) * factor,
                    float(h) * factor,
                    float(l) * factor,
                    float(adj),
                )
            else:
                o, h, l, c = float(o), float(h), float(l), float(c)
            candles.append(
                OHLCVCandle(
                    open=o,
                    high=h,
                    low=l,
                    close=c,
                    volume=float(v or 0.0),
                    timestamp=datetime.fromtimestamp(ts, tz=timezone.utc),
                )
            )

        if len(candles) > max_bars:
            candles = candles[-max_bars:]
        log.debug(
            f"TVClient | Chart history {symbol} {timeframe} → {len(candles)} bars"
        )
        return candles

    def _fetch_history_screener(
        self,
        symbol: str,
        exchange: str,
        timeframe: str,
        latest: OHLCVCandle | None = None,
    ) -> list[OHLCVCandle]:
        """Fetch OHLCV history — chart API first, screener offsets as fallback.

        Pass ``latest`` (already fetched candle) to avoid a second screener
        POST when overlaying the live bar onto Yahoo chart history.
        """
        chart_history = self._fetch_history_chart(symbol, timeframe)
        if chart_history:
            overlay = latest or self._fetch_candle_screener(symbol, exchange, timeframe)
            if overlay is not None:
                last_ts = chart_history[-1].timestamp
                chart_history[-1] = OHLCVCandle(
                    open=overlay.open,
                    high=overlay.high,
                    low=overlay.low,
                    close=overlay.close,
                    volume=overlay.volume,
                    timestamp=last_ts,
                )
            return chart_history

        max_offsets = self._max_history_offsets(timeframe)
        if max_offsets <= 1 and timeframe != "1d":
            candle = latest or self._fetch_candle_screener(symbol, exchange, timeframe)
            return [candle] if candle else []

        select_cols = ["name"]
        for i in range(max_offsets):
            select_cols.extend(
                [f"open[{i}]", f"high[{i}]", f"low[{i}]", f"close[{i}]", f"volume[{i}]"]
            )

        query = (
            Query()
            .select(*select_cols)
            .set_markets(self._screener)
            .where(col("name") == symbol.upper(), col("exchange") == exchange.upper())
        )
        try:
            _, df = _get_scanner_data(query)
        except Exception as exc:
            log.warning(
                f"TVClient | History query failed for {symbol} {timeframe}: {exc}"
            )
            return []

        if df is None or df.empty:
            return []

        row = df.iloc[0]
        candles: list[OHLCVCandle] = []

        for i in range(max_offsets):
            close = _row_float(row, f"close[{i}]")
            if close is None:
                break
            candles.append(
                OHLCVCandle(
                    open=_row_float(row, f"open[{i}]") or close,
                    high=_row_float(row, f"high[{i}]") or close,
                    low=_row_float(row, f"low[{i}]") or close,
                    close=close,
                    volume=_row_float(row, f"volume[{i}]") or 0.0,
                )
            )

        candles.reverse()  # oldest first
        return candles

    async def _fetch_mcp_analysis(
        self,
        symbol: str,
        exchange: str,
        timeframe: str,
        mcp_session: ClientSession | None = None,
    ) -> dict[str, Any]:
        if mcp_session is not None:
            return await self._call_coin_analysis(
                mcp_session, symbol, exchange, timeframe
            )

        server_params = StdioServerParameters(command=MCP_COMMAND, args=[])
        try:
            async with stdio_client(server_params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    return await self._call_coin_analysis(
                        session, symbol, exchange, timeframe
                    )
        except Exception as exc:
            log.warning(f"TVClient | MCP analysis failed for {symbol}: {exc}")
            return {}

    @staticmethod
    async def _call_coin_analysis(
        session: ClientSession,
        symbol: str,
        exchange: str,
        timeframe: str,
    ) -> dict[str, Any]:
        try:
            result = await session.call_tool(
                "coin_analysis",
                arguments={
                    "symbol": symbol,
                    "exchange": exchange,
                    "timeframe": timeframe,
                },
            )
            texts = [b.text for b in result.content if hasattr(b, "text")]
            raw = "\n".join(texts)
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                return {}
        except Exception as exc:
            log.warning(f"TVClient | coin_analysis failed for {symbol}: {exc}")
            return {}

    def _normalize_analysis(
        self,
        candle: OHLCVCandle,
        mcp_data: dict[str, Any],
    ) -> tuple[dict, dict, dict, dict]:
        if mcp_data and "error" not in mcp_data:
            return _from_mcp_payload(candle, mcp_data)

        log.debug("TVClient | MCP unavailable — screener OHLCV only")
        ind = {
            "open": candle.open,
            "high": candle.high,
            "low": candle.low,
            "close": candle.close,
            "volume": candle.volume,
        }
        return ind, {"RECOMMENDATION": "NEUTRAL"}, {}, {}


def _from_mcp_payload(
    candle: OHLCVCandle,
    mcp_data: dict[str, Any],
) -> tuple[dict, dict, dict, dict]:
    indicators: dict[str, Any] = {
        "open": candle.open,
        "high": candle.high,
        "low": candle.low,
        "close": candle.close,
        "volume": candle.volume,
    }

    price_data = mcp_data.get("price_data") or {}
    for src, dst in (
        ("open", "open"),
        ("high", "high"),
        ("low", "low"),
        ("close", "close"),
        ("current_price", "close"),
        ("volume", "volume"),
    ):
        val = price_data.get(src)
        if val is not None:
            indicators[dst] = val

    rsi = mcp_data.get("rsi") or {}
    if isinstance(rsi, dict) and rsi.get("value") is not None:
        indicators["RSI"] = rsi["value"]

    ema = mcp_data.get("ema") or {}
    if isinstance(ema, dict):
        for period in (5, 10, 20, 30, 50, 100, 200):
            key = f"EMA{period}"
            if ema.get(key) is not None:
                indicators[key] = ema[key]

    sma = mcp_data.get("sma") or {}
    if isinstance(sma, dict):
        for period in (5, 10, 20, 30, 50, 100, 200):
            key = f"SMA{period}"
            if sma.get(key) is not None:
                indicators[key] = sma[key]

    macd = mcp_data.get("macd") or {}
    if isinstance(macd, dict):
        if macd.get("macd") is not None:
            indicators["MACD.macd"] = macd["macd"]
        if macd.get("signal") is not None:
            indicators["MACD.signal"] = macd["signal"]

    adx = mcp_data.get("adx") or {}
    if isinstance(adx, dict) and adx.get("value") is not None:
        indicators["ADX"] = adx["value"]

    sentiment = mcp_data.get("market_sentiment") or {}
    signal = sentiment.get("buy_sell_signal") or sentiment.get("overall_rating")
    rec = _map_recommendation(signal)

    grade = mcp_data.get("grade")
    if grade and rec == "NEUTRAL":
        rec = _map_recommendation(str(grade))

    summary = {"RECOMMENDATION": rec}
    oscillators = mcp_data.get("stochastic") or {}
    moving_avgs = ema if isinstance(ema, dict) else {}
    return indicators, summary, oscillators, moving_avgs


def _row_float(row: pd.Series, key: str) -> float | None:
    val = row.get(key)
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    return float(val)


def _map_recommendation(raw: Any) -> str:
    if raw is None:
        return "NEUTRAL"
    rec = str(raw).upper()
    mapping = {
        "BUY": "BUY",
        "STRONG_BUY": "STRONG_BUY",
        "SELL": "SELL",
        "STRONG_SELL": "STRONG_SELL",
        "NEUTRAL": "NEUTRAL",
        "HOLD": "NEUTRAL",
        "BULLISH": "BUY",
        "BEARISH": "SELL",
        "A": "BUY",
        "B": "BUY",
        "C": "NEUTRAL",
        "D": "SELL",
        "F": "STRONG_SELL",
    }
    return mapping.get(rec, "NEUTRAL")
