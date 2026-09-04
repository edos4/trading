"""
data/universes/_buckets.py — sub-universe partitions used by multi-bucket summaries.

backtest_uc_v14.cjs splits its trade log three ways: NASDAQ-60 mega/large caps,
"Dow/NYSE" (everything else in the sweep), and combined. `BacktestResult.summarize`
takes an optional `buckets=` mapping of {name: set[str]} to reproduce that.
"""

from __future__ import annotations

# NASDAQ-60 set, verbatim from backtest_uc_v14.cjs.
NAS60: frozenset[str] = frozenset({
    "AAPL", "MSFT", "NVDA", "META", "GOOGL", "AMZN", "TSLA", "AVGO",
    "NFLX", "AMD", "ADBE", "CSCO", "QCOM", "TXN", "MU", "AMAT", "LRCX", "KLAC",
    "ADI", "MCHP", "NXPI", "ON", "MRVL",
    "PANW", "CRWD", "SNPS", "CDNS", "FTNT", "WDAY", "ANSS", "CHKP", "CTSH",
    "INTU", "TEAM",
    "AMGN", "GILD", "VRTX", "REGN", "ISRG", "IDXX", "DXCM", "BIIB",
    "SBUX", "MNST", "PAYX", "ROST", "PCAR", "ODFL", "FAST", "DLTR",
    "PYPL", "INTC", "VRSK", "ZM", "ILMN", "ALGN", "ABNB", "DDOG", "SNOW", "PDD",
})


def uc_buckets(symbols: list[str]) -> dict[str, frozenset[str]]:
    """{'nas60': …, 'dow': …} — 'dow' is every swept symbol not in NAS60."""
    universe = {s.upper() for s in symbols}
    return {
        "nas60": frozenset(universe & NAS60),
        "dow": frozenset(universe - NAS60),
    }
