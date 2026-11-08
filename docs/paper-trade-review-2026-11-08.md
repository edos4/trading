# Paper export review — 8 November 2026

Input: the workspace `pt.json`, exported at 17:30:53 Asia/Manila.
SHA-256: `ab66ceead403b46614c5c2daabc34d86c27ff17000e24ef52b63c7326c13c883`.
The input is left unchanged and is not included in this commit. The machine-readable
analysis is [paper-trade-review-2026-11-08.json](paper-trade-review-2026-11-08.json).

## What this export establishes

| Book | Closed / open | Net realized P&L | Evidence |
| --- | ---: | ---: | --- |
| US, USD | 1 / 0 | +$984.29 | One SHOP short; five saved daily bars |
| PSE, PHP | 0 / 0 | ₱0 | Idle; no trades or signal log |

The US book started with $100,000 and ended with $100,984.29. This is a
0.98429% account gain; the individual trade returned 9.94899% on its $9,893.35
entry notional. There are no long trades to evaluate and no open positions in
this input to close. A 100% win rate from one short does not establish an edge;
profit factor is undefined because there are no losses. Removing the best
trade removes the entire sample. USD and PHP results are never combined.

The offline review reproduced the entry, all five pre-bar order states, the
exit trigger, both fees and the realized P&L from saved OHLC. It did not
download prices, rescan detectors, run Kronos, or change the paper ledgers.

## The rejection funnel is the principal trading bottleneck

The cumulative counters cover 250 replay sessions, 1,931 selected symbols,
478,517 timeframe requests and 466,651 pattern evaluations. There were
469,351 successful snapshots (98.08% of requests); the final scan had 1,834
symbols with snapshots and 17 dead symbols. Final-cycle counters are zero;
the meaningful figures are under `scan_stats.run_totals`:

| Outcome | Count | Share of 26 detections |
| --- | ---: | ---: |
| DII rejected | 20 | 76.92% |
| Kronos rejected after DII | 5 | 19.23% |
| Filled | 1 | 3.85% |

Overall rejection is 96.15%. Kronos rejected five of the six candidates that
survived DII. The run used DII **Filter**, Kronos entries **Confirm**, and
Kronos exits **Exit**; the volume gate was off. This is approximately one
filled trade per trading year, despite a large scanned universe.

The four exported journal rows cannot explain these 25 rejected signals.
They contain AAA/BBB decisions at 17:26:32, after the saved scan finished at
17:14:57. They belong to two different run IDs; neither includes SHOP's
filled identity. Their prices, patterns and insufficient-cash rejection match
synthetic funding-test inputs. Before this change, tests without their own
log override could append to the operator's real journal. The original
rejected-candidate details are absent: their side, timestamps, model votes,
and subsequent returns cannot be reconstructed from aggregate counters.

Changes: the Paper tab now exposes **DII entries**, defaulting to **Shadow**.
It records the vote while allowing the candidate to reach later checks.
**Filter** remains an explicit per-run choice. The selected mode overrides
server defaults, is frozen when the book starts, and is saved in its settings.
API/MCP callers can pass `dii_gate_mode`; omitted values retain configured
defaults. Geometry, net target economics, cash, exposure, collateral and
Kronos checks remain active. This removes the observed DII veto when Shadow
is selected; it does not imply all 26 candidates would have filled or won.

Tests now use temporary signal journals and explicit shipped DII defaults.
The reviewer groups decisions by run ID as well as signal ID, so replaying
the same signal in two runs no longer merges their outcomes. It also reports
scan totals separately from journal coverage and warns about mismatches.
Existing logs are preserved; missing production evidence is not invented.

## SHOP closed correctly as a short

The short was opened on January 15 and covered on January 22, after five
observed bars and seven calendar days. SELL opens the short; its exit buys
back shares, incurs adverse upward slippage and pays the exit fee.

| Measure | Value |
| --- | ---: |
| Quantity / executed entry | 62 shares / $159.570169 |
| Structural stop / capped pattern stop | $170.673000 / $169.003957 |
| Independent initial protective stop | $163.951694 |
| Initial net loss budget | $296.80, or 3% of entry notional |
| Best surviving net close | +12.94899%, or $1,281.09 |
| Next-session protective buy stop | $143.319926 |
| Executed cover after 0.05% slippage | $143.391586 |
| Gross profit / both 0.1% fees | $1,003.07 / $18.78 |
| Net profit / net R | $984.29 / +3.62331R |

| Completed session | Net liquidation return at close | Following protective stop |
| --- | ---: | ---: |
| Jan 15 | +0.74170% | $162.769938 |
| Jan 16 | +2.10992% | $160.589930 |
| Jan 20 | +9.20835% | $149.279933 |
| Jan 21 | +12.94899% | $143.319926 |
| Jan 22 | Closed at +9.94899% | Buy stop touched |

The paper trail gave back exactly three percentage points, or $296.80, from
the best surviving net close. The separate pattern trail had armed but was
wider ($157.409166). On January 22, price opened at $141.789993 and reached
$144.190002, crossing the protective buy stop. The later $137.639999 close
would have looked better, but holding through an already-triggered stop would
use hindsight. January 21's range cannot trigger a stop first established at
that day's close either.

Kronos confirmed the short at entry with three down votes and still supported
it on January 21 with two down votes and one flat vote. No Kronos exit was
executed. Its saved checkpoint provenance is unverified and its historical
holdout status unknown, so these votes do not independently validate the model.

The bounded 2R target comparison exits earlier for $564.41, $419.88 less than
the actual trade. Entry-based trailing and the existing paper protection
reproduce the actual $984.29. There is no evidence here to disable shorts,
force smaller profit targets, tune a breakeven threshold, or widen stops.
An extra five basis points of friction per side reduces the fixed-fill gain
to $974.90; ten basis points reduces it to $965.50. These are accounting
stresses, not new execution simulations.

## Closing positions reliably

The existing `net-loss-trail-v1` policy already applies a 3% **net** loss
budget to new paper entries and adopted open positions. It includes both
commissions and configured exit slippage. It tightens from positive completed
net closes without waiting for the pattern's R activation. Its floor is
`max(-3%, best_net_close - 3 percentage points)`; a +1% peak can still finish
at -2%. The budget is per trade's entry notional, not 3% of account equity.
The policy and saved bar-exit semantics remain compatible with this export.

Two execution defects have been fixed for both sides:

1. Scanning only selected entry symbols and enabled detectors' timeframes
   could orphan a position after changing the universe or muting its detector.
   Every held position and queued order now retains its own symbol/timeframe
   feed. Held symbols are retried even if previously marked dead. Extra feed
   requests manage existing orders; they do not expand the entry universe.
2. A funded live scan buffered snapshots until every fetch finished, delaying
   a crossed stop behind unrelated slow requests. Forming live quotes now
   check resting exits immediately on arrival and settle through the same
   long/short accounting. Replay retains the opening-fills-first rule that
   prevents future intrabar proceeds from financing another entry.

Ordinary touches stop at the 3% net budget or tighter protection; gaps and
movement between polls still fill at observed executable prices and may
exceed it. This is consistent with the
[SEC's explanation of stop-order execution](https://www.investor.gov/introduction-investing/general-resources/news-alerts/alerts-bulletins/investor-bulletins-15).
Paper stops are scanner-managed simulated orders. The export also omits
borrow fees and verified borrow availability, spread dynamics and independently
verified corporate actions, so it cannot certify live short profitability.

## Validation and next run

Regression coverage exercises both directions, ordinary and gapped stops,
costs and cash reconciliation, saved trails after restart, detector/universe
changes with both single and batch feeds, queued fills, and live quote exits
before the remaining scan finishes. Existing tests continue to cover adoption
of older positions, no retroactive trail fills, and no double settlement.
The full non-slow suite passed: **2,643 passed, 154 skipped, 10 deselected**
(plus five subtests). API/async tests were run with local socket access.
`node --check web/static/app.js` and `git diff --check` also passed.

Reproduce the analysis without touching trading state:

```sh
.venv/bin/python -m analysis.paper_trade_review pt.json \
  --output /tmp/paper-trade-review-2026-11-08.json
```

For the next paper run, use the visible DII Shadow setting and keep both US
directions enabled. Collect a complete journal before evaluating rejection
quality by side and pattern. Compare later, separate periods under identical
costs and protection before promoting either gate to a veto. A larger sample
must establish expectancy and drawdowns; this one winner cannot.
