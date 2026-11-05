# Paper trade review — 2026-11-05, 21:27 Manila export

The current `pt.json` shows a marginally profitable US strategy whose long
trades subsidize consistently poor shorts. Preserve the uncapped pattern
targets and working trails, make pattern shorts an explicit research opt-in,
and enforce that permission at the paper ledger as well as the scanner.
This is a risk-policy decision from a small sample, not proof of future edge.

## Input and verification

Export time: **2026-11-05 13:27:42 UTC / 21:27:42 Asia/Manila**.
SHA-256: `d11f66d2ace0d32f3b7c0b170f91244b7c77753be13445690a5fcd8b63c71e63`.
Exit engine: `6c9e5cb88fc36896f885a55b18a80617a1cc46ddeb4712dff23bfbcbade04d7e`.
The book was running and mid-scan at export. Its last saved position mark is
**2026-09-14**, so this is an unfinished historical replay, not a completed
November-to-November test. The PSE/PHP book contains no trades; all money below
is USD and no PSE performance conclusion follows.

There are **59 closed trades, one open short (IRON), and no pending entries**.
The earlier `paper-exit-audit-2026-11-05.md` describes a different, 12:25 UTC
export with 65 closed trades. Its zero-trailing-exit finding does not describe
this input. Its results must not be pooled with these results as independent
samples.

`analysis/paper_trade_review.py` independently reconstructs all 60 positions
from their saved resolved rules and the OHLC in `daily_marks[].exit_check`.
It verifies every pre-bar order and exit decision, plus closed exit dates,
fills, reasons, holding-bar counts, net P&L and fees. The open position's marked
P&L and the book's realized, unrealized and fee totals also reconcile.
Fee and slippage rates come from the actual saved fills and entry fees:
**0.10% per side and zero slippage**, respectively. It does not assume today's
configuration described the historical run. Full-precision saved rules and
fills avoid share-count amplification of rounded display prices.

No database, market-data download, mutable detector rescan or model inference
is needed. Runtime drift, missing OHLC evidence and inconsistent baselines
raise errors before comparisons are returned. Older exports without this
evidence cannot be replayed by this tool. External model decisions cannot be
recreated from a final vote alone; incompatible pending-exit state also fails
verification instead of being invented.

Reproduce against the source with the hash above:

```bash
.venv/bin/python -m analysis.paper_trade_review pt.json \
  --output /tmp/paper-trade-review.json
```

The checked-in `paper-trade-review-2026-11-05.json` contains the verified
per-position results and comparisons. The operator's source export is retained
in the working tree without including its pre-existing edit in this commit.

## Performance and fragility

| Closed cohort | Trades | Wins | Net P&L | Profit factor | Mean net R |
|---|---:|---:|---:|---:|---:|
| All | 59 | 20 (33.9%) | +$1,683.17 | 1.134 | +0.106 |
| BUY | 37 | 18 (48.6%) | +$6,703.35 | 2.026 | +0.638 |
| SELL | 22 | 2 (9.1%) | −$5,020.18 | 0.169 | −0.788 |
| Double bottom | 36 | 17 (47.2%) | +$6,496.31 | 1.994 | +0.636 |
| Double top | 22 | 2 (9.1%) | −$5,020.18 | 0.169 | −0.788 |
| Falling wedge | 1 | 1 | +$207.05 | undefined (no losses) | +0.704 |

Closed gross P&L is **+$2,872.07**, less **$1,188.90** in entry/exit fees.
IRON adds **+$151.93** marked P&L after its entry fee, reconciling to the desk's
**+$1,835.10** total. Hypothetically liquidating IRON at its last saved close
and charging the exit fee reduces its contribution to **+$142.14**. It is
still an open trade, not a realized winner. Total paid fees, including IRON's
entry, are **$1,198.85**.

The average winner is $712.85 and average loser is −$322.41. This observed
payoff requires about **31.14% wins** to break even versus 33.90% observed:
only 2.76 percentage points of margin. Expectancy is just **+$28.53 per trade**.
Daily marked drawdown over the recorded position histories reaches
**$6,119.94**, exceeding the final profit by more than three times. The peak
concurrent count, including positions before same-session exits, is five.
This remains an independent fixed-notional research book; it does not model
broker margin, locate availability, borrow fees or portfolio capacity.

Profit is concentrated: remove AIRO, the best trade (+$2,357.49), and closed
P&L becomes **−$674.32**. Remove the best three and it becomes **−$3,429.84**.
These sensitivity calculations are not reasons to remove winners; they show
why the aggregate profit alone is insufficient validation.

The logged selection path contains 75 queued entries: 60 filled and 15 failed
opening-gap checks. Another 219 logged rejections are DII decisions. These
are event counts: queued and later filled/rejected rows describe the same
candidate, so 369 log rows are not 369 independent opportunities. Rejected
signals have no corresponding realized trade paths here; the export cannot
establish the DII gate's incremental benefit or calibrate new DII thresholds.

Holding fills and quantities fixed, extra friction of 5, 10 and 20 basis
points per side reduces closed P&L to **+$1,088.72**, **+$494.27** and
**−$694.63**, respectively. These are accounting stresses, not fresh order
simulations: slippage can also change sizing, entry eligibility and exit timing.

## Why pattern shorts become opt-in

| Entry quarter | BUY trades | BUY net | SELL trades | SELL net |
|---|---:|---:|---:|---:|
| 2025 Q4 | 4 | −$1,407.66 | 1 | −$314.29 |
| 2026 Q1 | 13 | −$159.67 | 12 | −$2,817.87 |
| 2026 Q2 | 9 | +$1,552.88 | 4 | −$1,391.47 |
| 2026 Q3, partial | 11 | +$6,717.81 | 5 | −$496.55 |

The shorts lose in every observed quarter and produce 20 hard stops against
two trailing winners. Their average payoff needs approximately **37.19% wins**,
far above the observed 9.09%. Even a descriptive 95% Wilson interval for that
win rate is only **2.53%–27.81%**. Trades may be dependent and the policy was
selected after observing outcomes; this interval is not a formal independent
validation or a forecast. The corresponding long interval is wide too:
**33.45%–64.11%**.

All these shorts are double tops. This sample cannot distinguish a family
problem from a broader short-side or market-regime problem. The existing
boolean short switch is the smallest reversible restriction and avoids
inventing a fitted indicator threshold or assuming the mirrored pattern has
the same edge as a double bottom.

The shipped default is `SHORT_ENTRIES_ENABLED=false`. An explicit true value
continues to allow short-side research. The same check now applies at scanner
selection, direct paper entry, queue creation and eventual pending-order fill,
including orders restored after restart. Turning it off does not close an
existing short or block its protective stop/target/trailing exits. The existing
indicators-only exception remains. Market long-only rules still apply.

Retaining the original 37 longs and removing the original shorts yields
+$6,703.35. That is a **fixed-entry subset comparison**, not a new scanner
backtest or an expected return. A full rescan can produce different candidates,
cooldowns and fills. Validate the policy on untouched later periods and
multiple market regimes before treating it as an enduring improvement.
Freeze the rules before that validation. The export does not establish a
point-in-time universe or validate corporate-action adjustments and delisted
coverage; those assumptions also need explicit treatment in a broader test.

There is no evidence for a universal new price floor:

| Entry price | BUY trades / net | SELL trades / net |
|---|---:|---:|
| Below $5 | 10 / −$546.74 | 2 / −$640.50 |
| $5–$10 | 6 / +$3,488.04 | 2 / −$640.59 |
| $10–$50 | 11 / −$599.92 | 11 / −$2,190.73 |
| $50 and above | 10 / +$4,361.98 | 7 / −$1,548.36 |

A $10 minimum would discard a profitable long cohort, while shorts lose in
every price band. A sub-$5 ban selected from ten long trades would be another
fitted rule. The existing optional price floor stays opt-in.

## Exit policy: keep room for the winners

There are **39 hard stops, 16 trailing stops and four targets**. Trails contribute
**+$8,460.08** and targets **+$5,796.94**, against hard stops of −$12,573.85.
Eighteen closed positions armed their trail. Entry checks use the next observed
open, including valid protective orders on that entry session. Intrabar
ambiguity favors the stop; an already marketable opening order executes before
later intrabar touches. A surviving close can arm or ratchet tomorrow's trail,
never an order retroactively applied to the earlier part of today's bar.

The 1R trail / 1.75R activation is working in this export. Restoring a 2R cap to
the **same saved entries, stops, quantities and bars**, with actual fees, gives:

| Target policy | Wins | Net closed P&L | Profit factor | Trailing exits |
|---|---:|---:|---:|---:|
| Saved measured targets | 20/59 | +$1,683.17 | 1.134 | 16 |
| Cap at 2R | 21/59 | −$1,037.58 | 0.915 | 0 |

The cap improves win rate while reducing profit by **$2,720.75**, and again
preempts trail activation. Every capped closed trade resolves within its
original saved OHLC coverage; no post-exit price path is invented. An open
remainder is valued separately with liquidation costs. The earlier uncommitted
`TARGET_MAX_R=0` default is included because this new replay independently
supports preserving the measured targets. A positive cap remains configurable.

**21/59 close on entry day**, including 19 losses, for −$3,020.05. The median
entry-day range among closed trades is **2.10R**, suggesting that a 3% fixed
stop is often narrow relative to daily movement. Effective entry risk ranges
from **1.56% to 3%**, because signal-anchored stops can be tightened by an
adverse entry gap. The existing guard rejected six entries with less than half
their planned stop distance, alongside nine gaps through the structural stop.
Those protections are retained.

There are two trades with ambiguous stop/target touches and eight gap fills.
VITL loses **−3.66 net R** after a gap; a nominal 3% stop is not a guaranteed
loss ceiling. Skipping entry-day exits or forcing stop-price fills through gaps
would make results look better by making execution less credible. Widening
stops, changing trail activation, or adding time exits needs a separate causal
replay with later bars, consistent risk sizing and holdout validation; this
sample does not calibrate a replacement width.

Ten last-saved Kronos exit votes oppose the position, yet no trade closes via
Kronos. All ten anchors are favorable to the entry. The current handler defers
an opposing vote until an adverse-move floor is exceeded, which is consistent
with these observations. The historical floor and a full vote timeline are
absent, so the model's causal effect cannot be quantified from this export.
Automatically treating the final opposing vote as an earlier executable exit
would be unsupported.

## Export and regression improvements

Future UI and CLI exports include `execution_context`: actual account fees,
slippage, simulated clock, initial capital, sizing and current entry/stop/trail
settings. It is explicitly labeled current snapshot context, not a historical
rule snapshot. Older snapshots keep a missing value instead of receiving
today's defaults. Per-trade rule snapshots remain the historical authority.

The related paper-exit evidence changes are included: separate effective hard
and trailing stops, pre-bar orders and raw OHLC, fees and net R, deep-copied
export evidence, and simulation-time holding summaries. Entry-policy
regressions exercise scanner/direct/queued/restarted paths, explicit short
opt-in, the indicator exception and continued management of existing shorts.
Portable audit regressions cover both directions, nonzero slippage, costs,
first-session targets, open liquidation, currency separation and rejection of
corrupted evidence. No detector thresholds or protected runtime files changed.

Validation: the broader working-tree suite passed **824 tests**, with 144
skips. The commit candidate, tested over a clean copy of HEAD without the
unrelated detector/audit work, passed **808 tests**, with 144 skips. Skips
include PostgreSQL integration checks without a configured disposable test
database. The final audit tests passed again after adding price/log breakdowns,
and the generated report reproduces byte-for-byte. Existing loopback tests
require execution outside the network-restricted sandbox; they pass there.
