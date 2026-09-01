"""
core/market.py — US vs Philippine (PSE) market profiles.

One codebase, two books. Select MARKET=us|ph (or --market / UI dropdown).
Do not mix PHP prices into the USD paper ledger.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

MARKET_US = "us"
MARKET_PH = "ph"
MarketId = Literal["us", "ph"]

# PSETradeX target: board lot becomes 1 share for all names.
_PSETRADEX_LOT1_ON = date(2026, 11, 23)

# Starting PH sleeve: drop names below this peso turnover (ADV proxy).
PH_MIN_ADV_PHP = 5_000_000.0
# US paper 2026-08-17: 77 tickers off a huge tape, noise W-patterns on
# names like CDE/NIO. Floor ~$20M dollar volume (TV `value`).
US_MIN_ADV_USD = 20_000_000.0
# US paper 2026-08-18: sub-$5 names (TGLO −35%, QMCI, AHMA, IBIO) dominated
# stop-loss churn despite ADV filter — gap risk and OTC-style wicks.
# 2026-09-01: $6/$6 names (LICN, WEST) gapped straight through a 10% stop
# overnight; sub-$10 still carries outsized gap/wick risk even with an ADV
# filter, so raise the floor to $10.
US_MIN_SHARE_PRICE = 10.0


@dataclass(frozen=True)
class MarketProfile:
    id: str
    label: str
    tv_screener: str
    tv_exchange: str
    yahoo_suffix: str
    currency: str
    currency_symbol: str
    session_tz: str
    paper_account_path: Path
    paper_initial_capital: float
    max_daily_loss: float
    txn_cost_pct: float
    # Engine breakeven is per book: a “scratch” must clear round-trip costs.
    # US 0.10% RT → 3% trigger / 0.15% buffer. PH ~0.70% RT → 5% / 0.80%.
    breakeven_trigger_pct: float | None
    breakeven_buffer_pct: float
    long_only: bool
    kronos_gate_default: bool
    kronos_rank_default: bool
    scan_interval_seconds: int
    universe_order: str
    default_n_symbols: int
    min_adv: float | None
    min_share_price: float | None
    skip_edgar: bool
    lot_round: bool


US = MarketProfile(
    id=MARKET_US,
    label="US (NASDAQ/NYSE)",
    tv_screener="america",
    tv_exchange="NASDAQ",
    yahoo_suffix="",
    currency="USD",
    currency_symbol="$",
    session_tz="America/New_York",
    paper_account_path=Path("data/cache/paper_account.json"),
    paper_initial_capital=100_000.0,
    max_daily_loss=1_500.0,
    txn_cost_pct=0.001,
    breakeven_trigger_pct=0.06,
    breakeven_buffer_pct=0.0015,
    long_only=False,
    kronos_gate_default=True,
    kronos_rank_default=False,
    scan_interval_seconds=3600,
    universe_order="value",
    default_n_symbols=50,
    min_adv=US_MIN_ADV_USD,
    min_share_price=US_MIN_SHARE_PRICE,
    skip_edgar=False,
    lot_round=False,
)

PH = MarketProfile(
    id=MARKET_PH,
    label="Philippines (PSE)",
    tv_screener="philippines",
    tv_exchange="PSE",
    yahoo_suffix=".PS",
    currency="PHP",
    currency_symbol="₱",
    session_tz="Asia/Manila",
    paper_account_path=Path("data/cache/paper_account_ph.json"),
    paper_initial_capital=1_000_000.0,
    max_daily_loss=15_000.0,
    txn_cost_pct=0.0035,  # ~0.70% round trip (buy~0.295% + sell~0.395%)
    breakeven_trigger_pct=0.05,  # skip +3% flicker (PH paper: PNB 003 in 3 bars)
    breakeven_buffer_pct=0.008,  # ≥ RT cost so BE is a ₱ scratch, not −0.6%
    long_only=True,
    kronos_gate_default=False,
    kronos_rank_default=False,
    scan_interval_seconds=300,
    universe_order="Value.Traded",
    default_n_symbols=30,
    min_adv=PH_MIN_ADV_PHP,
    min_share_price=None,
    skip_edgar=True,
    lot_round=True,
)

PROFILES: dict[str, MarketProfile] = {US.id: US, PH.id: PH}

# (inclusive_lo, inclusive_hi, tick, lot) — pre-PSETradeX board-lot table.
# hi=None means unbounded. Spec example: ₱5.00–9.99 → lot 100, tick ₱0.01.
_PSE_BANDS: tuple[tuple[float, float | None, float, int], ...] = (
    (0.0001, 0.0099, 0.0001, 1_000_000),
    (0.01, 0.049, 0.001, 100_000),
    (0.05, 0.249, 0.001, 10_000),
    (0.25, 0.495, 0.005, 10_000),
    (0.50, 4.99, 0.01, 1_000),
    (5.00, 9.99, 0.01, 100),
    (10.00, 19.98, 0.02, 100),
    (20.00, 49.95, 0.05, 100),
    (50.00, 99.95, 0.05, 10),
    (100.00, 199.90, 0.10, 10),
    (200.00, 499.80, 0.20, 10),
    (500.00, 999.50, 0.50, 10),
    (1_000.00, 1_999.00, 1.00, 5),
    (2_000.00, 4_998.00, 2.00, 5),
    (5_000.00, None, 5.00, 5),
)

# Regular + common special non-working days. Eid dates are proclamations —
# keep the known 2026 set; unknown years still honor weekends + fixed dates.
_PH_HOLIDAYS: dict[int, frozenset[date]] = {
    2026: frozenset({
        date(2026, 1, 1),
        date(2026, 2, 17),  # Chinese New Year
        date(2026, 2, 25),  # EDSA
        date(2026, 4, 2),   # Maundy Thursday
        date(2026, 4, 3),   # Good Friday
        date(2026, 4, 9),   # Araw ng Kagitingan
        date(2026, 5, 1),   # Labor Day
        date(2026, 6, 12),  # Independence Day
        date(2026, 8, 21),  # Ninoy Aquino
        date(2026, 8, 31),  # National Heroes Day (last Monday of August)
        date(2026, 11, 1),  # All Saints
        date(2026, 11, 30), # Bonifacio Day
        date(2026, 12, 8),  # Immaculate Conception
        date(2026, 12, 24),
        date(2026, 12, 25),
        date(2026, 12, 30), # Rizal Day
        date(2026, 12, 31),
    }),
}

SessionWindow = Literal[
    "preopen",
    "preopen_no_cancel",
    "am",
    "recess",
    "pm",
    "preclose",
    "preclose_no_cancel",
    "tal",
    "closed",
]


def resolve_market_id(raw: str | None = None) -> str:
    """Normalize us|ph|US|PH|philippines. Unknown → us."""
    if raw is None or not str(raw).strip():
        from config import settings

        raw = getattr(settings, "market", MARKET_US)
    key = str(raw).strip().lower()
    if key in ("ph", "pse", "philippines", "philippine"):
        return MARKET_PH
    if key in ("us", "america", "nasdaq", "nyse"):
        return MARKET_US
    if key in PROFILES:
        return key
    return MARKET_US


def get_market(raw: str | None = None) -> MarketProfile:
    return PROFILES[resolve_market_id(raw)]


_EXTRA_SPLIT = re.compile(r"[\s,;]+")


def parse_extra_symbols(raw: str | list[str] | tuple[str, ...] | None) -> list[str]:
    """Comma / space / semicolon tickers. Deduped, uppercased, no exchange prefix."""
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        chunks: list[str] = []
        for item in raw:
            chunks.extend(parse_extra_symbols(item if isinstance(item, str) else str(item)))
        seen: set[str] = set()
        out: list[str] = []
        for sym in chunks:
            if sym in seen:
                continue
            seen.add(sym)
            out.append(sym)
        return out
    text = str(raw).strip()
    if not text:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for tok in _EXTRA_SPLIT.split(text):
        sym = tok.strip().upper()
        if not sym:
            continue
        if ":" in sym:
            sym = sym.split(":")[-1]
        if sym.endswith(".PS"):
            sym = sym[:-3]
        if not sym or sym in seen:
            continue
        seen.add(sym)
        out.append(sym)
    return out


def merge_extra_symbols(
    rows: list[tuple[str, str]],
    extra: str | list[str] | tuple[str, ...] | None,
    market: str | None = None,
) -> list[tuple[str, str]]:
    """Append operator extras after screener rows. Skip names already present."""
    profile = get_market(market)
    wanted = parse_extra_symbols(extra)
    if not wanted:
        return rows
    seen = {str(sym).upper() for sym, _ex in rows}
    out = list(rows)
    for sym in wanted:
        if sym in seen:
            continue
        out.append((sym, profile.tv_exchange))
        seen.add(sym)
    return out


def default_market() -> MarketProfile:
    return get_market(None)


def ph_history_symbol(symbol: str) -> str:
    """Bot ticker BDO → stocks_history key BDO.PS. Idempotent on .PS."""
    s = (symbol or "").strip().upper()
    if ":" in s:
        s = s.split(":")[-1]
    if s.endswith(".PS"):
        return s
    return f"{s}.PS" if s else s


def is_ph_history_symbol(symbol: str) -> bool:
    return (symbol or "").upper().strip().endswith(".PS")


def yahoo_chart_symbol(symbol: str, market: str | None = None, *, screener: str | None = None) -> str:
    """Bot ticker → Yahoo chart ticker. Never send PSE:BDO to Yahoo."""
    s = (symbol or "").strip().upper()
    if ":" in s:
        s = s.split(":")[-1]
    if s.endswith(".PS"):
        return s
    profile = get_market(market) if market or screener is None else None
    use_ps = False
    if screener is not None:
        use_ps = screener.strip().lower() == "philippines"
    elif profile is not None:
        use_ps = profile.yahoo_suffix == ".PS"
    if use_ps and s:
        return f"{s}.PS"
    return s


def ohlcv_cache_key(symbol: str, timeframe: str, market: str | None = None) -> str:
    chart = yahoo_chart_symbol(symbol, market)
    return f"{chart}_{timeframe}".replace(":", "_")


def format_money(amount: float, market: str | None = None, *, signed: bool = False) -> str:
    profile = get_market(market)
    body = f"{abs(amount):,.2f}"
    sign = ""
    if signed:
        sign = "+" if amount >= 0 else "-"
    elif amount < 0:
        sign = "-"
    return f"{sign}{profile.currency_symbol}{body}"


def _pse_band(price: float) -> tuple[float, int]:
    px = abs(float(price))
    if px <= 0:
        return 0.01, 100
    for lo, hi, tick, lot in _PSE_BANDS:
        if px < lo:
            continue
        if hi is None or px <= hi + 1e-12:
            return tick, lot
    return _PSE_BANDS[-1][2], _PSE_BANDS[-1][3]


def pse_tick_size(price: float) -> float:
    return _pse_band(price)[0]


def pse_board_lot(price: float, *, as_of: date | None = None) -> int:
    day = as_of or datetime.now(ZoneInfo("Asia/Manila")).date()
    if day >= _PSETRADEX_LOT1_ON:
        return 1
    return _pse_band(price)[1]


def round_price_to_tick(price: float) -> float:
    tick = pse_tick_size(price)
    if tick <= 0:
        return price
    n = round(price / tick)
    return round(n * tick, 10)


def round_qty_to_lot(qty: float, price: float, *, as_of: date | None = None) -> int:
    lot = pse_board_lot(price, as_of=as_of)
    q = int(qty)
    if lot <= 1:
        return max(0, q)
    return max(0, (q // lot) * lot)


def apply_lot_rounding(signal, *, as_of: date | None = None) -> bool:
    """Round qty down to the board lot and stops/TPs to the tick.

    Returns False when the rounded size is below one lot (caller should skip).
    """
    px = float(getattr(signal, "price", 0.0) or 0.0)
    if px <= 0:
        return False
    rounded = round_qty_to_lot(signal.qty, px, as_of=as_of)
    if rounded < pse_board_lot(px, as_of=as_of):
        signal.qty = 0
        return False
    signal.qty = rounded
    if getattr(signal, "stop_loss", None) is not None:
        signal.stop_loss = round_price_to_tick(float(signal.stop_loss))
    if getattr(signal, "take_profit", None) is not None:
        signal.take_profit = round_price_to_tick(float(signal.take_profit))
    return True


def _last_monday_of_august(year: int) -> date:
    d = date(year, 8, 31)
    while d.weekday() != 0:
        d -= timedelta(days=1)
    return d


def is_ph_holiday(day: date) -> bool:
    known = _PH_HOLIDAYS.get(day.year)
    if known is not None:
        return day in known
    if day.month == 8 and day == _last_monday_of_august(day.year):
        return True
    return (day.month, day.day) in {
        (1, 1), (4, 9), (5, 1), (6, 12), (8, 21),
        (11, 1), (11, 30), (12, 8), (12, 25), (12, 30), (12, 31),
    }


def _manila_now(now: datetime | None = None) -> datetime:
    tz = ZoneInfo("Asia/Manila")
    if now is None:
        return datetime.now(tz)
    if now.tzinfo is None:
        return now.replace(tzinfo=tz)
    return now.astimezone(tz)


def session_window(market: str | None = None, now: datetime | None = None) -> SessionWindow:
    """PSE session clock. US profile is always 'am' (paper already runs 24/7)."""
    profile = get_market(market)
    if profile.id != MARKET_PH:
        return "am"
    local = _manila_now(now)
    if local.weekday() >= 5 or is_ph_holiday(local.date()):
        return "closed"
    t = local.time()
    if time(9, 0) <= t < time(9, 15):
        return "preopen"
    if time(9, 15) <= t < time(9, 30):
        return "preopen_no_cancel"
    if time(9, 30) <= t < time(12, 0):
        return "am"
    if time(12, 0) <= t < time(13, 0):
        return "recess"
    if time(13, 0) <= t < time(14, 45):
        return "pm"
    if time(14, 45) <= t < time(14, 48):
        return "preclose"
    if time(14, 48) <= t < time(14, 50):
        return "preclose_no_cancel"
    if time(14, 50) <= t < time(15, 0):
        return "tal"
    return "closed"


def may_assume_fill(market: str | None = None, now: datetime | None = None) -> bool:
    """Paper/live assumed fills only in continuous AM/PM matching."""
    if get_market(market).id != MARKET_PH:
        return True
    return session_window(market, now) in ("am", "pm")


def _session_now(market: str | None = None, now: datetime | None = None) -> datetime:
    profile = get_market(market)
    tz = ZoneInfo(profile.session_tz)
    if now is None:
        return datetime.now(tz)
    if now.tzinfo is None:
        return now.replace(tzinfo=tz)
    return now.astimezone(tz)


def is_swing_timeframe(timeframe: str) -> bool:
    """Daily/weekly bars whose identity is a session date, not last-print time."""
    t = (timeframe or "").strip().lower().replace(" ", "")
    return t in {"1d", "d", "day", "daily", "1w", "w", "wk", "1wk", "week", "weekly"}


def is_weekly_timeframe(timeframe: str) -> bool:
    t = (timeframe or "").strip().lower().replace(" ", "")
    return t in {"1w", "w", "wk", "1wk", "week", "weekly"}


def cash_session_closed(market: str | None = None, now: datetime | None = None) -> bool:
    """True after the listing's cash session (US 16:00 ET, PSE after TAL / weekend)."""
    profile = get_market(market)
    if profile.id == MARKET_PH:
        return session_window(market, now) == "closed"
    local = _session_now(market, now)
    if local.weekday() >= 5:
        return True
    return local.time() >= time(16, 0)


def last_closed_session_date(
    market: str | None = None, now: datetime | None = None,
) -> date:
    """Most recent fully closed cash session (skips weekends; PH holidays)."""
    local = _session_now(market, now)
    d = local.date()
    if not cash_session_closed(market, now):
        d -= timedelta(days=1)
    profile = get_market(market)
    for _ in range(14):
        if d.weekday() >= 5:
            d -= timedelta(days=1)
            continue
        if profile.id == MARKET_PH and is_ph_holiday(d):
            d -= timedelta(days=1)
            continue
        return d
    return d


def is_closed_session_bar(
    timeframe: str,
    ts: datetime | None,
    *,
    market: str | None = None,
    now: datetime | None = None,
) -> bool:
    """Whether this snapshot is a completed swing bar (not a forming RTH print).

    Historical bars (session date before today) are always closed — paper
    stream replay uses wall-clock 'now' years after the history bar.
    """
    if not is_swing_timeframe(timeframe):
        return True
    if ts is None:
        return True
    local_now = _session_now(market, now)
    ts_local = ts if ts.tzinfo is not None else ts.replace(tzinfo=local_now.tzinfo)
    bar_date = ts_local.astimezone(local_now.tzinfo).date()
    if bar_date < local_now.date():
        return True
    return cash_session_closed(market, now)


def bar_identity(
    timeframe: str,
    ts: datetime | None,
    *,
    market: str | None = None,
    now: datetime | None = None,
):
    """Stable key for 'is this a new bar?' across hourly scans of 1d/1w.

    Intraday: the candle timestamp. Swing: session-local date (or ISO week),
    and while today's cash session is still open the key stays on the last
    *closed* session so last-print timestamp churn is not a new daily bar.
    """
    if ts is None:
        return None
    if not is_swing_timeframe(timeframe):
        return ts
    local_now = _session_now(market, now)
    closed = is_closed_session_bar(timeframe, ts, market=market, now=now)
    if closed:
        ts_local = ts if ts.tzinfo is not None else ts.replace(tzinfo=local_now.tzinfo)
        d = ts_local.astimezone(local_now.tzinfo).date()
    else:
        d = last_closed_session_date(market, now)
    if is_weekly_timeframe(timeframe):
        iso = d.isocalendar()
        return (iso.year, iso.week)
    return d


def session_label(market: str | None = None, now: datetime | None = None) -> str:
    w = session_window(market, now)
    return {
        "preopen": "pre-open",
        "preopen_no_cancel": "pre-open (no cancel)",
        "am": "AM",
        "recess": "recess",
        "pm": "PM",
        "preclose": "pre-close",
        "preclose_no_cancel": "pre-close (no cancel)",
        "tal": "TAL",
        "closed": "closed",
    }.get(w, w)


def clock_payload(market: str | None = None, now: datetime | None = None) -> dict:
    """Session strip / dual-book clocks. US paper is always 'open'."""
    profile = get_market(market)
    local = _session_now(profile.id, now)
    window = session_window(profile.id, now)
    tz_name = "PHT" if profile.id == MARKET_PH else "ET"
    session_open = window in ("am", "pm") if profile.id == MARKET_PH else True
    return {
        "market": profile.id,
        "local_time": local.strftime("%H:%M"),
        "tz_name": tz_name,
        "session": session_label(profile.id, now),
        "session_open": session_open,
        "running": False,
    }


def markets_payload() -> list[dict]:
    """JSON for web/UI dropdowns."""
    out = []
    for p in (US, PH):
        out.append({
            "id": p.id,
            "label": p.label,
            "currency": p.currency,
            "currency_symbol": p.currency_symbol,
            "tv_screener": p.tv_screener,
            "tv_exchange": p.tv_exchange,
            "default_n_symbols": p.default_n_symbols,
            "txn_cost_pct": p.txn_cost_pct,
            "breakeven_trigger_pct": p.breakeven_trigger_pct,
            "breakeven_buffer_pct": p.breakeven_buffer_pct,
            "account_value": p.paper_initial_capital,
            "kronos_gate": p.kronos_gate_default,
            "kronos_rank": p.kronos_rank_default,
            "kronos_batch": False,
            "long_only": p.long_only,
            "scan_interval_seconds": p.scan_interval_seconds,
        })
    return out
