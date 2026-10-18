# Backtest and paper replay smoke — 2026-10-17

Passed both runs using actual cached daily OHLCV and five repository detectors (double top, double bottom, rounding bottom, rounding top, head and shoulders). No application source changes were needed.

Eight symbols: CDNS, NET, ON, TXN, SWKS, QCOM, PCAR, PYPL. Input: the last 260 bars from each committed fixture cache, 2025-06-20 through 2026-07-02. Thirty warmup bars precede the 230 decision sessions, 2025-08-04 through 2026-07-02. Each run uses $100,000 initial capital, $10,000 independent fixed notional, 0.1% commission per leg, 0.1% slippage per fill, causal next-observed-open entries and keep-open at the boundary.

| Run | Closed | Open | Realized net | Unrealized net | Total net | Fees | Final equity |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| baseline | 11 | 1 | $-419.56 | $642.83 | $223.28 | $225.89 | $100223.28 |
| gated | 1 | 0 | $-329.65 | $0.00 | $-329.65 | $20.25 | $99670.35 |

Assertions passed for equal closed/open fills, symbols, patterns, directions, prices, quantities, entry/exit timestamps and exit reasons; equal fees; and every session's equity within $0.000001. Each run required actual fills and checked for signal-handler failures. The baseline leaves one last-session order unfilled, as expected without a following opening bar. The gated run enables top-four selection and the real RVOL/OBV volume gate; eleven volume vetoes were recorded. This small sample does not establish that the top-four cutoff became binding; the earlier multi-symbol regression covers that case.

The paper side drives the scanner's bar handling, analysis, signal processing, selection and PaperAccount execution in memory. Input arrives one bar at a time; future bars are not loaded into its candle store. Paper books were never loaded, saved or reset. No broker orders or external data requests were made. The version database/API and feed transport/scheduler are bypassed by this offline runner, which imports the repository detectors directly. Other patterns, Laya, live connectivity and current production account state were not checked. These inspected fixtures are development data, not an untouched holdout or a profitability finding.

Reproduce from the repository root:

```sh
PYTHONPATH=. MPLCONFIGDIR=/tmp/mpl-backtest-smoke .venv/bin/python docs/verification/backtest-paper-smoke-2026-10-17/run.py
```

The runner contains the comparison assertions. JSON artifacts include trades, signal events, daily equity, costs and input-file SHA256 identities. Paper trade wall-clock timestamps differ by design; comparisons use their simulated timestamps. Unclosed trades have no economic exit, so internal exit-date placeholders are excluded until a trade closes.
