"""
data/alpaca_client.py — Alpaca Markets historical OHLCV bars client.

The free Basic plan allows 200 requests/min and serves SIP bars since 2016,
with only the latest 15 minutes restricted. The bars endpoint accepts many
symbols per request and returns at most 10 000 data points per page, so
callers batch symbols and follow `next_page_token` until the batch is done.

Unknown tickers are omitted from the response rather than failing the batch.

API docs: https://docs.alpaca.markets/reference/stockbars
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Sequence

from utils.logger import log

_API_BASE = "https://data.alpaca.markets"
_TIMEFRAME = "1Hour"
_PAGE_LIMIT = 10_000


class AlpacaAuthError(RuntimeError):
    """Raised when Alpaca rejects the API key pair (HTTP 401/403)."""


class AlpacaBadRequest(RuntimeError):
    """Raised when Alpaca rejects a symbol list (HTTP 400)."""


def _parse_ts(value: str) -> int:
    """RFC-3339 bar timestamp (bar open) → Unix seconds."""
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1]
    if "." in text:
        head, frac = text.split(".", 1)
        text = f"{head}.{frac[:6]}"
    return int(datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp())


def _retry_after(headers, default: float) -> float:
    try:
        return max(1.0, float(headers.get("Retry-After")))
    except (TypeError, ValueError):
        return default


class AlpacaClient:
    """Batched historical bars client with a process-wide rate limiter."""

    def __init__(
        self,
        api_key: str,
        secret_key: str,
        *,
        feed: str = "sip",
        paper: bool = True,
        requests_per_min: int = 170,
        timeout: float = 30.0,
        max_retries: int = 5,
    ):
        self.api_key = api_key
        self.secret_key = secret_key
        self.feed = feed or "sip"
        self._trading_base = (
            "https://paper-api.alpaca.markets" if paper
            else "https://api.alpaca.markets"
        )
        self._timeout = timeout
        self._max_retries = max(1, max_retries)
        self._interval = 60.0 / max(1, requests_per_min)
        self._lock = threading.Lock()
        self._next_request_at = 0.0

    def _throttle(self) -> None:
        """Reserve the next global request slot, then wait for it."""
        with self._lock:
            slot = max(time.monotonic(), self._next_request_at)
            self._next_request_at = slot + self._interval
        delay = slot - time.monotonic()
        if delay > 0:
            time.sleep(delay)

    def _request(self, path: str, params: dict[str, str]) -> dict | None:
        url = f"{_API_BASE}{path}?{urllib.parse.urlencode(params)}"
        return self._get(url)

    def _get(self, url: str):
        headers = {
            "APCA-API-KEY-ID": self.api_key,
            "APCA-API-SECRET-KEY": self.secret_key,
            "User-Agent": "trading-bot/2.0 alpaca-client",
        }
        delay = 2.0
        for attempt in range(self._max_retries):
            self._throttle()
            req = urllib.request.Request(url, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                if exc.code == 401:
                    raise AlpacaAuthError(
                        f"Alpaca rejected the API key pair (HTTP {exc.code})"
                    ) from exc
                if exc.code == 403:
                    # Plan scope, not credentials — e.g. "subscription does not
                    # permit querying recent SIP data" or OTC content. Skip the
                    # batch instead of aborting every other worker's work.
                    body = exc.read().decode("utf-8", "replace").strip()
                    log.warning(
                        f"Alpaca | HTTP 403: {body or exc.reason} — batch skipped"
                    )
                    return None
                if exc.code == 400:
                    body = exc.read().decode("utf-8", "replace").strip()
                    raise AlpacaBadRequest(body or "HTTP 400") from exc
                if exc.code == 429:
                    wait = _retry_after(exc.headers, delay)
                    log.warning(
                        f"Alpaca | 429 (attempt {attempt + 1}), retrying in {wait:.0f}s"
                    )
                elif exc.code >= 500:
                    wait = delay
                    log.warning(
                        f"Alpaca | HTTP {exc.code} (attempt {attempt + 1}), "
                        f"retrying in {wait:.0f}s"
                    )
                else:
                    log.warning(f"Alpaca | HTTP {exc.code}: {exc.reason}")
                    return None
            except Exception as exc:
                wait = delay
                log.warning(
                    f"Alpaca | request failed (attempt {attempt + 1}): {exc}"
                )
            if attempt == self._max_retries - 1:
                break
            time.sleep(wait)
            delay = min(delay * 2, 60)

        log.warning("Alpaca | request failed after retries")
        return None

    def fetch_assets(self) -> dict[str, str]:
        """symbol -> exchange for every US equity Alpaca knows (active + inactive)."""
        payload = self._get(
            f"{self._trading_base}/v2/assets"
            f"?{urllib.parse.urlencode({'status': 'all', 'asset_class': 'us_equity'})}"
        )
        if not isinstance(payload, list):
            return {}
        return {
            str(a["symbol"]).strip(): str(a.get("exchange") or "").upper()
            for a in payload
            if a.get("symbol")
        }

    def fetch_bars(
        self,
        symbols: Sequence[str],
        *,
        start: str,
        end: str,
        adjustment: str = "raw",
    ) -> dict[str, list[tuple[int, float, float, float, float, int]]]:
        """Fetch 1-hour bars for `symbols` in [start, end].

        Returns ``symbol -> sorted (ts, open, high, low, close, volume) rows``.
        A failed page ends the batch with whatever pages already arrived; the
        caller's resume point (newest stored bar) picks up the rest later.
        Raises AlpacaBadRequest if the API rejects the symbol list, so the
        caller can retry smaller groups without the offending ticker.
        """
        if not symbols:
            return {}

        params = {
            "symbols": ",".join(symbols),
            "timeframe": _TIMEFRAME,
            "start": start,
            "end": end,
            "limit": str(_PAGE_LIMIT),
            "adjustment": adjustment,
            "feed": self.feed,
        }
        collected: dict[str, dict[int, tuple]] = {}
        pages = 0

        while True:
            payload = self._request("/v2/stocks/bars", params)
            if payload is None:
                break
            pages += 1
            for symbol, bars in (payload.get("bars") or {}).items():
                bucket = collected.setdefault(symbol, {})
                for bar in bars:
                    try:
                        ts = _parse_ts(bar["t"])
                        o = float(bar["o"])
                        h = float(bar["h"])
                        lo = float(bar["l"])
                        c = float(bar["c"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    bucket[ts] = (ts, o, h, lo, c, int(bar.get("v") or 0))

            token = payload.get("next_page_token")
            if not token:
                break
            params["page_token"] = token

        rows = {
            symbol: [bucket[ts] for ts in sorted(bucket)]
            for symbol, bucket in collected.items()
        }
        log.debug(
            f"Alpaca | {len(symbols)} symbol(s) → "
            f"{sum(len(v) for v in rows.values())} bars, {pages} page(s)"
        )
        return rows
