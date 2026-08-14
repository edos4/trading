"""
data/pse_edge.py — Daily OHLCV from the PSE Edge chart endpoint.

Yahoo Finance still accepts BDO.PS-style tickers, but as of 2026 the v8 chart
API returns a YHD stub with no timestamps. Edge is the same backend the
portal's own stock-data page uses (POST /common/DisclosureCht.ax).

Ticker identity stays BDO in the bot; this module maps BDO → (cmpy_id,
security_id) via the company directory, then pulls daily OHLC. `volume` is
share count approximated as peso VALUE / CLOSE (Edge chart has no share volume).
"""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from utils.logger import log

_DIR_URL = "https://edge.pse.com.ph/companyDirectory/search.ax"
_CHART_URL = "https://edge.pse.com.ph/common/DisclosureCht.ax"
_DIR_REFERER = "https://edge.pse.com.ph/companyDirectory/form.do"
_CHART_REFERER = "https://edge.pse.com.ph/companyPage/stockData.do"
_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
_MANILA = ZoneInfo("Asia/Manila")
_DIR_CACHE = Path("data/cache/pse_edge_directory.json")
_DIR_TTL_SECONDS = 7 * 24 * 3600
_HIST_CACHE_DIR = Path("data/cache/pse_edge_ohlcv")
_HIST_TTL_SECONDS = 6 * 3600
_MIN_INTERVAL_SECONDS = 0.4

# Symbol cell: <td class="alignC"><a ... onclick="cmDetail('260','468');...">BDO</a>
_SYMBOL_RE = re.compile(
    r"cmDetail\('(\d+)',\s*'(\d+)'\);return false;\">\s*"
    r"([A-Z0-9][A-Z0-9.+-]*)\s*</a>",
    re.IGNORECASE,
)

_http_lock = threading.Lock()
_next_allowed = 0.0
_dir_lock = threading.Lock()
_dir_mem: dict[str, tuple[str, str]] | None = None


def reset_for_tests() -> None:
    global _next_allowed, _dir_mem
    with _http_lock:
        _next_allowed = 0.0
    with _dir_lock:
        _dir_mem = None


def _throttle() -> None:
    global _next_allowed
    with _http_lock:
        now = time.monotonic()
        wait = _next_allowed - now
        _next_allowed = max(now, _next_allowed) + _MIN_INTERVAL_SECONDS
    if wait > 0:
        time.sleep(wait)


def _request(
    url: str,
    *,
    data: bytes | None = None,
    content_type: str,
    referer: str,
    timeout: int = 30,
) -> bytes:
    _throttle()
    headers = {
        "User-Agent": _UA,
        "Accept": "application/json, text/html, */*; q=0.01",
        "Content-Type": content_type,
        "Referer": referer,
        "Origin": "https://edge.pse.com.ph",
        "X-Requested-With": "XMLHttpRequest",
    }
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _norm_symbol(symbol: str) -> str:
    s = (symbol or "").strip().upper()
    if ":" in s:
        s = s.split(":")[-1]
    if s.endswith(".PS"):
        s = s[: -len(".PS")]
    return s


def _load_dir_cache() -> dict[str, tuple[str, str]]:
    global _dir_mem
    with _dir_lock:
        if _dir_mem is not None:
            return _dir_mem
        if _DIR_CACHE.exists():
            try:
                raw = json.loads(_DIR_CACHE.read_text(encoding="utf-8"))
                ts = float(raw.get("fetched_at") or 0)
                age = time.time() - ts
                rows = raw.get("symbols") or {}
                if age <= _DIR_TTL_SECONDS and isinstance(rows, dict):
                    _dir_mem = {
                        str(k).upper(): (str(v["company_id"]), str(v["security_id"]))
                        for k, v in rows.items()
                        if isinstance(v, dict) and v.get("company_id") and v.get("security_id")
                    }
                    return _dir_mem
            except (OSError, json.JSONDecodeError, TypeError, ValueError, KeyError):
                pass
        _dir_mem = {}
        return _dir_mem


def _save_dir_cache(mapping: dict[str, tuple[str, str]]) -> None:
    global _dir_mem
    with _dir_lock:
        _dir_mem = dict(mapping)
        _DIR_CACHE.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "fetched_at": time.time(),
            "symbols": {
                k: {"company_id": cid, "security_id": sid}
                for k, (cid, sid) in sorted(_dir_mem.items())
            },
        }
        _DIR_CACHE.write_text(json.dumps(payload), encoding="utf-8")


def _parse_directory_html(html: str) -> dict[str, tuple[str, str]]:
    out: dict[str, tuple[str, str]] = {}
    for company_id, security_id, ticker in _SYMBOL_RE.findall(html):
        out[ticker.upper()] = (company_id, security_id)
    return out


def resolve_ids(symbol: str) -> tuple[str, str] | None:
    """Map bot ticker BDO → Edge (company_id, security_id). Cached to disk."""
    sym = _norm_symbol(symbol)
    if not sym:
        return None
    mapping = _load_dir_cache()
    hit = mapping.get(sym)
    if hit:
        return hit
    form = urllib.parse.urlencode(
        {
            "pageNo": "1",
            "companyId": "",
            "keyword": sym,
            "sortType": "",
            "dateSortType": "DESC",
            "cmpySortType": "ASC",
            "symbolSortType": "ASC",
            "sector": "ALL",
            "subsector": "ALL",
        }
    ).encode()
    try:
        html = _request(
            _DIR_URL,
            data=form,
            content_type="application/x-www-form-urlencoded",
            referer=_DIR_REFERER,
        ).decode("utf-8", errors="replace")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        log.warning(f"PSE Edge | directory lookup failed for {sym}: {exc}")
        return None
    found = _parse_directory_html(html)
    exact = found.get(sym)
    if exact is None:
        log.warning(f"PSE Edge | {sym} not in directory keyword results")
        return None
    mapping[sym] = exact
    try:
        _save_dir_cache(mapping)
    except OSError as exc:
        log.debug(f"PSE Edge | directory cache write skipped: {exc}")
    return exact


def _parse_chart_date(raw: str) -> datetime | None:
    text = (raw or "").strip()
    for fmt in ("%b %d, %Y %H:%M:%S", "%b %d, %Y"):
        try:
            naive = datetime.strptime(text, fmt)
            return naive.replace(tzinfo=_MANILA)
        except ValueError:
            continue
    return None


def _f(value) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _hist_cache_path(symbol: str) -> Path:
    return _HIST_CACHE_DIR / f"{_norm_symbol(symbol)}_1d.json"


def _load_daily_cache(symbol: str):
    p = _hist_cache_path(symbol)
    if not p.exists():
        return None
    try:
        if time.time() - p.stat().st_mtime > _HIST_TTL_SECONDS:
            return None
        from data.tv_client import OHLCVCandle

        raw = json.loads(p.read_text(encoding="utf-8"))
        candles = []
        for c in raw:
            ts = c.get("t")
            candles.append(
                OHLCVCandle(
                    open=c["o"],
                    high=c["h"],
                    low=c["l"],
                    close=c["c"],
                    volume=c.get("v", 0.0),
                    timestamp=datetime.fromisoformat(ts) if ts else None,
                )
            )
        return candles
    except (OSError, json.JSONDecodeError, TypeError, KeyError, ValueError):
        return None


def _save_daily_cache(symbol: str, candles) -> None:
    try:
        _HIST_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        payload = [
            {
                "o": c.open,
                "h": c.high,
                "l": c.low,
                "c": c.close,
                "v": c.volume,
                "t": c.timestamp.isoformat() if c.timestamp else None,
            }
            for c in candles
        ]
        _hist_cache_path(symbol).write_text(json.dumps(payload), encoding="utf-8")
    except OSError as exc:
        log.debug(f"PSE Edge | history cache write skipped: {exc}")


def fetch_daily(symbol: str, *, start: date | None = None, end: date | None = None):
    """Daily OHLCV from Edge. Returns list[OHLCVCandle]; empty on failure."""
    from data.tv_client import OHLCVCandle

    custom_range = start is not None or end is not None
    if not custom_range:
        cached = _load_daily_cache(symbol)
        if cached:
            return cached

    ids = resolve_ids(symbol)
    if ids is None:
        return []
    company_id, security_id = ids
    end = end or date.today()
    start = start or (end - timedelta(days=800))
    payload = json.dumps(
        {
            "cmpy_id": str(company_id),
            "security_id": str(security_id),
            "startDate": start.strftime("%m-%d-%Y"),
            "endDate": end.strftime("%m-%d-%Y"),
        }
    ).encode()
    referer = f"{_CHART_REFERER}?cmpy_id={company_id}&security_id={security_id}"
    try:
        body = _request(
            _CHART_URL,
            data=payload,
            content_type="application/json",
            referer=referer,
        )
        data = json.loads(body.decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        log.warning(f"PSE Edge | chart failed for {symbol}: {exc}")
        return []

    rows = data.get("chartData") or []
    candles = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        ts = _parse_chart_date(str(row.get("CHART_DATE") or ""))
        o, h, l, c = _f(row.get("OPEN")), _f(row.get("HIGH")), _f(row.get("LOW")), _f(row.get("CLOSE"))
        if ts is None or None in (o, h, l, c) or c == 0:
            continue
        peso_value = _f(row.get("VALUE")) or 0.0
        volume = peso_value / c if c else 0.0
        candles.append(
            OHLCVCandle(open=o, high=h, low=l, close=c, volume=volume, timestamp=ts)
        )
    candles.sort(key=lambda x: x.timestamp or datetime.min.replace(tzinfo=_MANILA))
    if candles and not custom_range:
        _save_daily_cache(symbol, candles)
    return candles


def _to_weekly(daily):
    if len(daily) < 5:
        return []
    from data.tv_client import OHLCVCandle
    import pandas as pd

    df = pd.DataFrame(
        [
            {
                "timestamp": c.timestamp,
                "open": c.open,
                "high": c.high,
                "low": c.low,
                "close": c.close,
                "volume": c.volume,
            }
            for c in daily
            if c.timestamp is not None
        ]
    )
    if df.empty:
        return []
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    df = df.set_index("timestamp").sort_index()
    weekly = df.resample("W").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    ).dropna()
    return [
        OHLCVCandle(
            open=row.open,
            high=row.high,
            low=row.low,
            close=row.close,
            volume=row.volume,
            timestamp=idx.to_pydatetime(),
        )
        for idx, row in weekly.iterrows()
    ]


def fetch_history(symbol: str, timeframe: str, max_bars: int):
    """PH history for 1d / 1W. Weekly is resampled from Edge daily bars."""
    daily = fetch_daily(symbol)
    if timeframe == "1W":
        candles = _to_weekly(daily)
    else:
        candles = daily
    if max_bars and len(candles) > max_bars:
        candles = candles[-max_bars:]
    log.debug(f"PSE Edge | {symbol} {timeframe} → {len(candles)} bars")
    return candles
