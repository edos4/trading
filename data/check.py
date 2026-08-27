"""
data/check.py — "check history" report for the stock-history database.

`python main.py --check-db` prints global + per-symbol statistics and
freshness. Exit code is non-zero when the dataset is stale, so it can
gate a cron job.
"""

from __future__ import annotations

import sys
from collections import Counter

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


def run_check(
    *,
    stale_days: int = 7,
    top_n: int = 20,
) -> int:
    conn = db.get_conn()
    try:
        db.ensure_schema(conn)
        stats = db.global_stats(conn)
        ranges = db.per_symbol_range(conn)
        today = db.today_date()

        print("=" * 78)
        print("  STOCK-HISTORY DATABASE CHECK")
        print("=" * 78)
        print(f"  Database:        {stats['db_name']}")
        print(f"  Symbols:         {stats['n_symbols']:,}")
        print(f"  Bars:            {stats['n_bars']:,}")
        print(f"  Bar date range:  {stats['min_date']} .. {stats['max_date']}")
        print(f"  DB size:         {stats['db_size_bytes'] / 1e9:.2f} GB")
        print(f"  Today:           {today}")

        if stats["max_date"] is None:
            print("\n  daily_bars is empty.")
            return 1

        global_age = (today - stats["max_date"]).days
        stale_global = global_age > stale_days
        print(f"  Global freshness: last bar {stats['max_date']} "
              f"({global_age}d ago) {'— STALE' if stale_global else '— OK'}")
        print()

        ages = Counter()
        for _sym, (_min_d, max_d, _n) in ranges.items():
            ages[_bucket((today - max_d).days)] += 1
        print("  Last-bar age histogram (per symbol):")
        for upper, label in _BUCKETS:
            if ages.get(label):
                print(f"    {label:>6s} : {ages[label]:>7,}")
        print()

        by_age = sorted(
            ranges.items(), key=lambda kv: kv[1][1], reverse=False
        )[:top_n]
        print(f"  {top_n} stalest symbols (by last bar date):")
        print(f"  {'symbol':8s} {'last_bar':>12s} {'age_d':>6s} {'rows':>8s}")
        print("  " + "-" * 40)
        for sym, (_min_d, max_d, n) in by_age:
            print(f"  {sym:8s} {str(max_d):>12s} {(today - max_d).days:>6d} {n:>8,d}")
        print("=" * 78)

        if stale_global:
            log.warning(f"check | dataset is stale: global last bar {stats['max_date']} "
                        f"is {global_age}d old (threshold {stale_days}d)")
            log.info(f"check | problems found: stale={stale_global}")
            return 1
        log.info("check | OK — database is current")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(run_check())
