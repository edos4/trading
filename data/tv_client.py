"""
data/tv_client.py — Market data via TradingView MCP + TradingView screener.

OHLCV comes from tradingview-screener (TradingView API). Indicators and
recommendations come from tradingview-mcp-server (coin_analysis). History
builds in OHLCVStore across scan cycles — no Yahoo Finance.
"""

from __future__ import annotations

import json
import logging
import shutil
import sys
import threading
import time
import urllib.error
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


def _is_benign_mcp_gone_process(message: str) -> bool:
    """True when MCP stdio teardown races a child that already exited.

    Closing stdin often reaps tradingview-mcp before the SDK's killpg();
    ESRCH is success, not a scan failure.
    """
    return (
        "Process group termination failed" in message
        and "No such process" in message
    ) or (
        "Process termination failed" in message
        and "attempting force kill" in message
    )


class _McpGoneProcessFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            return True
        return not _is_benign_mcp_gone_process(message)


def _install_mcp_stdio_teardown_fix() -> None:
    logger = logging.getLogger("mcp.os.posix.utilities")
    if any(isinstance(f, _McpGoneProcessFilter) for f in logger.filters):
        return
    logger.addFilter(_McpGoneProcessFilter())


_install_mcp_stdio_teardown_fix()


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


def _yahoo_chart_payload(
    chart_symbol: str, interval: str, range_: str, *, timeout: int = 20
) -> dict | None:
    """GET Yahoo v8 chart JSON. Retry on HTTP 429 with exponential backoff."""
    url = f"{_CHART_API}/{chart_symbol}?interval={interval}&range={range_}"
    delay = 2.0
    last_exc: Exception | None = None
    for attempt in range(7):
        req = urllib.request.Request(url, headers={"User-Agent": _CHART_UA})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            last_exc = exc
            if exc.code == 429 and attempt < 6:
                time.sleep(delay)
                delay = min(delay * 2, 60)
                continue
            log.warning(
                f"TVClient | Chart history failed for {chart_symbol} "
                f"{interval}/{range_}: {exc}"
            )
            return None
        except Exception as exc:
            last_exc = exc
            log.warning(
                f"TVClient | Chart history failed for {chart_symbol} "
                f"{interval}/{range_}: {exc}"
            )
            return None
    log.warning(
        f"TVClient | Chart history failed for {chart_symbol} after retries: {last_exc}"
    )
    return None

# interval, range, max bars to keep
# Daily uses 2y so Kronos gate can take LOOKBACK=400 (1y ≈ 252 bars is too short).
_CHART_SPECS: dict[str, tuple[str, str, int]] = {
    "1d": ("1d", "2y", 512),
    "1W": ("1wk", "5y", 65),
}


def _candles_from_yahoo_payload(
    payload: dict | None, chart_symbol: str, timeframe: str
) -> list[OHLCVCandle]:
    """Parse Yahoo v8 chart JSON. Empty/YHD stubs return [] with no parse error."""
    if not payload:
        return []
    try:
        result = (payload.get("chart") or {}).get("result") or []
        if not result:
            err = (payload.get("chart") or {}).get("error")
            log.warning(
                f"TVClient | Chart history empty for {chart_symbol} {timeframe}"
                + (f": {err}" if err else "")
            )
            return []
        result0 = result[0]
        timestamps = result0.get("timestamp") or []
        if not timestamps:
            meta = result0.get("meta") or {}
            log.info(
                f"TVClient | Yahoo {chart_symbol} {timeframe} has no bars "
                f"(exchange={meta.get('exchangeName') or meta.get('exchange')})"
            )
            return []
        quote = result0["indicators"]["quote"][0]
    except (KeyError, IndexError, TypeError) as exc:
        log.warning(
            f"TVClient | Chart history parse failed for {chart_symbol} {timeframe}: {exc}"
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
        # Keep OHLC on the raw trading-price basis. The live TradingView
        # screener candle is raw as well; mixing adjusted Yahoo history with
        # raw live prices creates artificial dividend/split discontinuities
        # at the history/live boundary and can fabricate chart patterns.
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
    return candles


def fetch_yahoo_daily_max(symbol: str) -> list[OHLCVCandle]:
    """Full Yahoo daily series (range=max), not truncated to tv_history_days."""
    from core.market import yahoo_chart_symbol

    chart_symbol = yahoo_chart_symbol(symbol, screener="america")
    payload = _yahoo_chart_payload(chart_symbol, "1d", "max", timeout=30)
    candles = _candles_from_yahoo_payload(payload, chart_symbol, "1d")
    if candles:
        log.debug(f"TVClient | max-history {symbol} → {len(candles)} bars")
    return candles


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

# TradingView's `value` column is None for every PSE row. Peso turnover lives
# in `Value.Traded`; 10-day ADV ≈ close * average_volume_10d_calc.
_PH_TRADED_COL = "Value.Traded"
_PH_AVG_VOL_COL = "average_volume_10d_calc"


def _numeric_col(df: pd.DataFrame, name: str) -> pd.Series:
    if name not in df.columns:
        return pd.Series(float("nan"), index=df.index, dtype="float64")
    return pd.to_numeric(df[name], errors="coerce")


def _ph_peso_adv(df: pd.DataFrame) -> pd.Series:
    """Peso ADV: 10d share volume * close, else today's Value.Traded, else close*volume."""
    close = _numeric_col(df, "close")
    adv10 = close * _numeric_col(df, _PH_AVG_VOL_COL)
    traded = _numeric_col(df, _PH_TRADED_COL)
    session = close * _numeric_col(df, "volume")
    return adv10.fillna(traded).fillna(session).fillna(0.0)


def _rows_from_screener_df(df: pd.DataFrame, want_n: int) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    seen: set[str] = set()
    for _, row in df.iterrows():
        if not row.get("name") or not row.get("exchange"):
            continue
        sym = str(row["name"]).upper()
        if sym in seen:
            continue
        seen.add(sym)
        rows.append((sym, str(row["exchange"]).upper()))
        if len(rows) >= want_n:
            break
    return rows


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
        """Return top N symbols from TradingView screener."""
        return [s for s, _ex in TVClient.fetch_top_symbols_with_exchanges(n, screener)]

    @staticmethod
    def fetch_top_symbols_with_exchanges(
        n: int = 100,
        screener: str = "america",
        *,
        order_by: str | None = None,
        min_value: float | None = None,
        exchange: str | None = None,
    ) -> list[tuple[str, str]]:
        """Return top N (symbol, exchange) pairs.

        US default: dollar volume (`value`) with a $20M ADV floor. PH: peso turnover (`Value.Traded` / 10d ADV),
        never share-volume (that ranks penny names). Optional ADV floor in pesos.
        """
        from core.market import get_market

        profile = None
        if screener == "philippines":
            profile = get_market("ph")
        rank = order_by or (profile.universe_order if profile else "market_cap_basic")
        want_n = max(n, 1)

        if screener == "philippines":
            return TVClient._fetch_ph_universe(
                want_n,
                min_value=min_value if min_value is not None else (
                    profile.min_adv if profile else None
                ),
                exchange=exchange,
            )

        fetch_n = want_n
        if min_value or exchange:
            fetch_n = min(max(want_n * 4, want_n), 500)

        select_cols = ["name", "exchange"]
        if rank in ("value", "volume") or min_value:
            select_cols.extend(["value", "volume"])

        def _query(rank_col: str) -> pd.DataFrame | None:
            q = (
                Query()
                .select(*select_cols)
                .set_markets(screener)
                .order_by(rank_col, ascending=False)
                .limit(fetch_n)
            )
            _, df = _get_scanner_data(q)
            return df

        df = None
        tried = []
        for rank_col in (rank, "volume", "market_cap_basic"):
            if rank_col in tried:
                continue
            tried.append(rank_col)
            try:
                df = _query(rank_col)
            except Exception as exc:
                log.warning(f"TVClient | universe order_by={rank_col} failed: {exc}")
                df = None
            if df is not None and not df.empty:
                break
        if df is None or df.empty:
            return []

        if exchange:
            exch = exchange.upper()
            if "exchange" in df.columns:
                df = df[df["exchange"].astype(str).str.upper() == exch]
        if min_value and "value" in df.columns:
            vals = pd.to_numeric(df["value"], errors="coerce").fillna(0.0)
            filtered = df[vals >= float(min_value)]
            if filtered.empty:
                log.warning(
                    f"TVClient | ADV floor {min_value:.0f} removed every row — "
                    f"keeping unfiltered top {want_n}"
                )
            else:
                df = filtered

        return _rows_from_screener_df(df, want_n)

    @staticmethod
    def _fetch_ph_universe(
        want_n: int,
        *,
        min_value: float | None,
        exchange: str | None,
    ) -> list[tuple[str, str]]:
        """Top PSE names by peso ADV. TV `value` is always None on this market."""
        fetch_n = min(max(want_n * 5, 120), 280)
        select_cols = [
            "name", "exchange", "close", "volume",
            _PH_TRADED_COL, _PH_AVG_VOL_COL,
        ]

        df = None
        for rank_col in (_PH_TRADED_COL, "volume"):
            try:
                q = (
                    Query()
                    .select(*select_cols)
                    .set_markets("philippines")
                    .order_by(rank_col, ascending=False)
                    .limit(fetch_n)
                )
                _, df = _get_scanner_data(q)
            except Exception as exc:
                log.warning(f"TVClient | PH universe order_by={rank_col} failed: {exc}")
                df = None
            if df is not None and not df.empty:
                break
        if df is None or df.empty:
            return []

        if exchange and "exchange" in df.columns:
            df = df[df["exchange"].astype(str).str.upper() == exchange.upper()]
        if df.empty:
            return []

        df = df.copy()
        df["_peso_adv"] = _ph_peso_adv(df)
        df = df.sort_values("_peso_adv", ascending=False)
        if min_value:
            filtered = df[df["_peso_adv"] >= float(min_value)]
            if filtered.empty:
                log.warning(
                    f"TVClient | PH ADV floor ₱{min_value:,.0f} removed every row — "
                    f"keeping top {want_n} by peso turnover"
                )
            else:
                df = filtered
        return _rows_from_screener_df(df, want_n)

    @staticmethod
    def fetch_universe(n: int, market: str | None = None) -> list[tuple[str, str]]:
        """Market-aware universe: US by dollar volume + ADV floor, PH by peso volume + ADV floor."""
        from core.market import get_market

        profile = get_market(market)
        return TVClient.fetch_top_symbols_with_exchanges(
            n if n else profile.default_n_symbols,
            profile.tv_screener,
            order_by=profile.universe_order,
            min_value=profile.min_adv,
            exchange=profile.tv_exchange if profile.id == "ph" else None,
        )

    @staticmethod
    def fetch_top_symbols_with_exchanges_cached(
        n: int = 100,
        screener: str = "america",
        ttl_seconds: int = _SYMBOLS_CACHE_TTL_SECONDS,
        *,
        order_by: str | None = None,
        min_value: float | None = None,
        exchange: str | None = None,
        market: str | None = None,
    ) -> list[tuple[str, str]]:
        """Same as fetch_top_symbols_with_exchanges, cached to data/cache/.

        Backtests (CLI and UI) re-run the same top-N screener query on every
        invocation; this reads the last fetch from disk instead when it's
        still fresh, and re-fetches + saves when it's missing or stale.
        """
        if market is not None:
            from core.market import get_market, ohlcv_cache_key  # noqa: F401

            return TVClient.fetch_universe_cached(n, market, ttl_seconds=ttl_seconds)

        rank = order_by or ("value" if screener == "philippines" else "market_cap_basic")
        tag = f"{screener}_{rank}_{n}"
        if exchange:
            tag += f"_{exchange}"
        if min_value:
            tag += f"_adv{int(min_value)}"
        path = _SYMBOLS_CACHE_DIR / f"symbols_{tag}.json"
        if path.exists():
            age = time.time() - path.stat().st_mtime
            if age < ttl_seconds:
                try:
                    rows = json.loads(path.read_text(encoding="utf-8"))
                    return [(s, e) for s, e in rows]
                except Exception:
                    pass
        rows = TVClient.fetch_top_symbols_with_exchanges(
            n, screener, order_by=order_by, min_value=min_value, exchange=exchange,
        )
        if rows:
            _SYMBOLS_CACHE_DIR.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(rows), encoding="utf-8")
        return rows

    @staticmethod
    def fetch_universe_cached(
        n: int,
        market: str | None = None,
        ttl_seconds: int = _SYMBOLS_CACHE_TTL_SECONDS,
        extra_symbols: str | list[str] | None = None,
    ) -> list[tuple[str, str]]:
        from core.market import get_market, merge_extra_symbols

        profile = get_market(market)
        rows = TVClient.fetch_top_symbols_with_exchanges_cached(
            n if n else profile.default_n_symbols,
            profile.tv_screener,
            ttl_seconds,
            order_by=profile.universe_order,
            min_value=profile.min_adv,
            exchange=profile.tv_exchange if profile.id == "ph" else None,
        )
        return merge_extra_symbols(rows, extra_symbols, profile.id)

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
                    # The history source supplies the stable session timestamp
                    # that TradingView screener's OHLC response omits. Use it
                    # for the current candle so bar_identity() can distinguish
                    # one completed daily/weekly bar from the next.
                    candle = OHLCVCandle(
                        open=candle.open, high=candle.high, low=candle.low,
                        close=candle.close, volume=candle.volume,
                        timestamp=history[-1].timestamp,
                    )
                    history[-1] = candle
                    store.replace_all(symbol, timeframe, history)
                else:
                    # Screener responses do not reliably expose a bar
                    # timestamp. For swing timeframes, a wall-clock fallback
                    # is still stable at the session-date level because
                    # bar_identity()/is_closed_session_bar() deliberately map
                    # forming bars to the last closed session. Intraday bars
                    # remain timestamp-less rather than pretending every scan
                    # is a distinct completed bar.
                    if candle.timestamp is None and timeframe in {"1d", "1W"}:
                        candle = OHLCVCandle(
                            open=candle.open, high=candle.high, low=candle.low,
                            close=candle.close, volume=candle.volume,
                            timestamp=datetime.now(timezone.utc),
                        )
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
        """Fetch OHLCV history. PH uses PSE Edge (Yahoo *.PS is a YHD stub).

        Local --ui/--web read 33ai.edos.uk (or VPS Postgres) and never Yahoo.
        """
        spec = _CHART_SPECS.get(timeframe)
        if spec is None:
            return []

        interval, range_, default_max = spec
        max_bars = settings.tv_history_days if timeframe == "1d" else default_max

        from data.history import load_daily_candles, resample_weekly, ui_web_history_enabled

        if ui_web_history_enabled():
            mkt = "ph" if self._screener == "philippines" else None
            daily = load_daily_candles(symbol, market=mkt)
            if not daily:
                return []
            if timeframe.upper() in ("1W", "W", "1WK", "WEEKLY"):
                weekly = resample_weekly(daily)
                if weekly and len(weekly) > default_max:
                    return weekly[-default_max:]
                return weekly
            if len(daily) > max_bars:
                return daily[-max_bars:]
            return daily

        if self._screener == "philippines":
            from data.pse_edge import fetch_history

            edge = fetch_history(symbol, timeframe, max_bars)
            if edge:
                log.debug(
                    f"TVClient | PSE Edge {symbol} {timeframe} → {len(edge)} bars"
                )
            return edge

        from core.market import yahoo_chart_symbol

        chart_symbol = yahoo_chart_symbol(symbol, screener=self._screener)
        payload = _yahoo_chart_payload(chart_symbol, interval, range_, timeout=20)

        candles = _candles_from_yahoo_payload(payload, chart_symbol, timeframe)
        if candles and len(candles) > max_bars:
            candles = candles[-max_bars:]
        if candles:
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
    moving_avgs = sma if isinstance(sma, dict) else {}
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
