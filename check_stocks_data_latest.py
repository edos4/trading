"""
check_stocks_data_latest.py — print the last 5 bars per stock from the
stock-history database into latest.txt.

Run:  python check_stocks_data_latest.py
"""

from __future__ import annotations

from data import db

_OUTPUT = "latest.txt"

_QUERY = """
SELECT symbol, bar_date, open, high, low, close, volume
FROM (
    SELECT symbol, bar_date, open, high, low, close, volume,
           ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY ts DESC) AS rn
    FROM daily_bars
) t
WHERE rn <= 5
ORDER BY symbol, bar_date DESC
"""


def _fmt_price(v: float) -> str:
    s = f"{v:.4f}".rstrip("0").rstrip(".")
    return s or "0"


def main() -> None:
    conn = db.get_conn()
    with conn.cursor() as cur:
        cur.execute(_QUERY)
        rows = cur.fetchall()
    conn.close()

    per_symbol: dict[str, list] = {}
    for symbol, bar_date, o, h, l, c, vol in rows:
        per_symbol.setdefault(symbol, []).append(
            (str(bar_date), _fmt_price(o), _fmt_price(h), _fmt_price(l),
             _fmt_price(c), int(vol))
        )

    with open(_OUTPUT, "w") as f:
        f.write(f"Last 5 bars per stock (stocks_history, "
                f"{len(per_symbol)} symbols)\n")
        f.write("=" * 78 + "\n")
        for symbol in sorted(per_symbol):
            f.write(f"\n{symbol}\n")
            f.write(f"  {'date':<12}{'open':>10}{'high':>10}"
                    f"{'low':>10}{'close':>10}{'volume':>12}\n")
            for bar_date, o, h, l, c, vol in per_symbol[symbol]:
                f.write(f"  {bar_date:<12}{o:>10}{h:>10}{l:>10}"
                        f"{c:>10}{vol:>12}\n")

    print(f"wrote {_OUTPUT}: {len(per_symbol)} symbols, "
          f"{len(rows)} bars")


if __name__ == "__main__":
    main()