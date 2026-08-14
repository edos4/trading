"""
data/edgar_client.py — SEC EDGAR 8-K item 2.02 (Results of Operations) lookup.

Used by the v9 earnings-blackout filter: a pattern must skip a trade when an
earnings 8-K (item 2.02) filing date falls inside the trade's holding window.

Source: SEC EDGAR submissions API
  https://data.sec.gov/submissions/CIK{XXXXXXXX}.json
plus the ticker → CIK map:
  https://www.sec.gov/files/company_tickers.json

The SEC requires a descriptive User-Agent header on every request. Set
EDGAR_USER_AGENT in the environment (or pass one to the constructor); the
default is a generic bot identity that SEC accepts but rate-limits.

Design:
  - One in-memory cache per (symbol) of the full set of 8-K item 2.02 filing
    dates, so a backtest that replays thousands of bars only hits the network
    once per symbol.
  - All network / parsing errors are caught and surfaced via the return value
    so callers can degrade gracefully (e.g. treat a fetch failure as "no
    blackout" rather than blocking every trade).
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from datetime import date, datetime
from typing import Iterable

from utils.logger import log

_TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"
_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
_DEFAULT_UA = "trading-bot-v2/2.0 (swing research)"
_REQUEST_TIMEOUT = 15  # seconds


def _resolve_user_agent(ua: str | None) -> str:
    if ua:
        return ua
    return os.environ.get("EDGAR_USER_AGENT") or _DEFAULT_UA


class EdgarClient:
    """Fetch 8-K item 2.02 filing dates per symbol, with in-memory caching."""

    def __init__(self, user_agent: str | None = None) -> None:
        self._ua = _resolve_user_agent(user_agent)
        self._ticker_to_cik: dict[str, int] | None = None
        # {symbol: sorted list of filing dates} cache
        self._earnings_cache: dict[str, list[date]] = {}

    # ── Public API ────────────────────────────────────────────────────────────
    def earnings_dates(self, symbol: str) -> list[date]:
        """All 8-K item 2.02 filing dates known for `symbol` (sorted ascending).

        Returns an empty list if the symbol is unknown or the request fails;
        failures are logged once and not retried on every call.
        """
        sym = symbol.upper().strip()
        if sym in self._earnings_cache:
            return self._earnings_cache[sym]

        try:
            cik = self._cik_for_ticker(sym)
            if cik is None:
                log.debug(f"[EdgarClient] {sym}: no CIK mapping, no earnings data")
                self._earnings_cache[sym] = []
                return []

            dates = self._fetch_earnings_8k_dates(cik)
            self._earnings_cache[sym] = dates
            return dates
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            log.warning(
                f"[EdgarClient] {sym}: EDGAR lookup failed ({exc!r}); "
                f"treating as no-known-earnings"
            )
            self._earnings_cache[sym] = []
            return []

    def has_earnings_in(
        self, symbol: str, start: date, end: date
    ) -> bool:
        """True if any 8-K item 2.02 filing date falls in [start, end]."""
        if skip_edgar_enabled():
            return False
        filings = self.earnings_dates(symbol)
        for fd in filings:
            if start <= fd <= end:
                return True
        return False

    # ── Internal helpers ──────────────────────────────────────────────────────
    def _cik_for_ticker(self, ticker: str) -> int | None:
        if self._ticker_to_cik is None:
            self._ticker_to_cik = self._fetch_ticker_map()
        return self._ticker_to_cik.get(ticker)

    def _fetch_ticker_map(self) -> dict[str, int]:
        payload = self._get_json(_TICKER_MAP_URL)
        out: dict[str, int] = {}
        # payload maps an integer key → {"cik_str": ..., "ticker": ...}
        for row in payload.values():
            tk = str(row.get("ticker", "")).upper()
            cik = row.get("cik_str")
            if tk and cik:
                out[tk] = int(cik)
        return out

    def _fetch_earnings_8k_dates(self, cik: int) -> list[date]:
        payload = self._get_json(_SUBMISSIONS_URL.format(cik=cik))
        recent = (payload.get("filings") or {}).get("recent") or {}
        forms = recent.get("form") or []
        dates = recent.get("filingDate") or []
        items = recent.get("items") or []
        out: list[date] = []
        for i, form in enumerate(forms):
            if form != "8-K":
                continue
            # `items[i]` is a single comma-separated string for this filing
            # (e.g. "2.02,9.01"), not a list of item codes. Iterating it
            # directly (as before) walks individual characters, so the
            # "2.02" substring check would essentially never match and the
            # earnings-blackout filter would never fire. Split it first.
            row_items = (items[i] if i < len(items) else "") or ""
            if not any("2.02" in it for it in row_items.split(",")):
                continue
            ds = dates[i] if i < len(dates) else None
            if not ds:
                continue
            try:
                out.append(datetime.strptime(ds, "%Y-%m-%d").date())
            except ValueError:
                continue
        out.sort()
        return out

    def _get_json(self, url: str) -> dict:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": self._ua,
                "Accept": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=_REQUEST_TIMEOUT) as resp:  # noqa: S310
            data = resp.read()
        return json.loads(data)


# Module-level convenience instance so patterns can share the cache.
_DEFAULT_CLIENT: EdgarClient | None = None
_SKIP_EDGAR = False


def set_skip_edgar(skip: bool) -> None:
    """PH / non-US books must not query SEC EDGAR (ticker collisions like SM)."""
    global _SKIP_EDGAR
    _SKIP_EDGAR = bool(skip)


def skip_edgar_enabled() -> bool:
    return _SKIP_EDGAR


def default_client() -> EdgarClient:
    """Shared EdgarClient (lazy singleton) so all patterns share one cache."""
    global _DEFAULT_CLIENT
    if _DEFAULT_CLIENT is None:
        _DEFAULT_CLIENT = EdgarClient()
    return _DEFAULT_CLIENT


def to_date(value: "date | datetime | str") -> date:
    """Coerce a pandas Timestamp / datetime / str to a python date."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return datetime.strptime(str(value), "%Y-%m-%d").date()


def any_earnings_in(
    symbol: str, window: Iterable["date | datetime | str"]
) -> bool:
    """Helper: true if any earnings filing date falls on any day in `window`."""
    if skip_edgar_enabled():
        return False
    dates = default_client().earnings_dates(symbol)
    if not dates:
        return False
    window_days = {to_date(d) for d in window}
    return any(fd in window_days for fd in dates)
