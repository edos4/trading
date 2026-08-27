"""
data/check.py — "check history" report for the stock-history database.

`python main.py --check-db` prints global + per-market statistics and
freshness. Exit code is non-zero when any selected book is stale.
"""

from __future__ import annotations

import sys
from collections import Counter

from core.market import last_closed_session_date
from data import db
from utils.logger import log

_BUCKETS = [
    (0, "today"),
    (1, "1d"),
    (3, "2-3d"),
    (7, "4-7d"),
    (14, "1-2w"),
    (30, "2-4w"),
    (60, "1-2mo"),
    (90, "2-3mo"),
    (365000, "3mo+"),
]


def _bucket(age_days: int) -> str:
    for upper, label in _BUCKETS:
        if age_days <= upper:
            return label
    return "3mo+"


def _print_book(
    label: str,
    stats: dict,
    ranges: dict,
    *,
    market: str,
    stale_days: int,
    top_n: int,
) -> bool:
    today = db.today_date(market)
    last_closed = last_closed_session_date(market)
    print("-" * 78)
    print(f"  {label}")
    print(f"  Symbols:         {stats['n_symbols']:,}")
    print(f"  Bars:            {stats['n_bars']:,}")
    print(f"  Bar date range:  {stats['min_date']} .. {stats['max_date']}")
    print(f"  Today ({market}): {today}  last closed session: {last_closed}")

    if stats["max_date"] is None or not ranges:
        print("  (empty)")
        return False

    age = (last_closed - stats["max_date"]).days
    stale = age > stale_days
    print(
        f"  Freshness: last bar {stats['max_date']} "
        f"({age}d vs last session) {'— STALE' if stale else '— OK'}"
    )

    ages = Counter()
    for _sym, (_min_d, max_d, _n, _mkt) in ranges.items():
        ages[_bucket((last_closed - max_d).days)] += 1
    print("  Last-bar age histogram (vs last closed session):")
    for upper, b in _BUCKETS:
        if ages.get(b):
            print(f"    {b:>6s} : {ages[b]:>7,}")

    by_age = sorted(ranges.items(), key=lambda kv: kv[1][1])[:top_n]
    print(f"  {top_n} stalest symbols:")
    print(f"  {'symbol':12s} {'last_bar':>12s} {'age_d':>6s} {'rows':>8s}")
    print("  " + "-" * 44)
    for sym, (_min_d, max_d, n, _mkt) in by_age:
        print(f"  {sym:12s} {str(max_d):>12s} {(last_closed - max_d).days:>6d} {n:>8,d}")
    return stale


def run_check(
    *,
    stale_days: int = 7,
    top_n: int = 20,
    market: str | None = None,
) -> int:
    conn = db.get_conn()
    try:
        db.ensure_schema(conn)
        global_stats = db.global_stats(conn)
        print("=" * 78)
        print("  STOCK-HISTORY DATABASE CHECK")
        print("=" * 78)
        print(f"  Database:        {global_stats['db_name']}")
        print(f"  Symbols (all):   {global_stats['n_symbols']:,}")
        print(f"  Bars (all):      {global_stats['n_bars']:,}")
        print(f"  Bar date range:  {global_stats['min_date']} .. {global_stats['max_date']}")
        print(f"  DB size:         {global_stats['db_size_bytes'] / 1e9:.2f} GB")

        if global_stats["max_date"] is None:
            print("\n  daily_bars is empty.")
            return 1

        books = [market] if market in ("us", "ph") else ["us", "ph"]
        stale_any = False
        any_book = False
        for mid in books:
            stats = db.global_stats(conn, market=mid)
            ranges = db.per_symbol_range(conn, market=mid)
            if stats["n_symbols"] == 0 and not ranges:
                if market:
                    print(f"\n  No {mid} symbols.")
                    return 1
                continue
            any_book = True
            stale_any = _print_book(
                f"MARKET {mid.upper()}",
                stats,
                ranges,
                market=mid,
                stale_days=stale_days,
                top_n=top_n,
            ) or stale_any

        print("=" * 78)
        if not any_book:
            return 1
        if stale_any:
            log.warning("check | one or more markets are stale")
            return 1
        log.info("check | OK — selected market(s) current")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(run_check())
