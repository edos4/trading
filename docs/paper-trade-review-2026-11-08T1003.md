# Follow-up paper review — 8 November 2026, 18:03 Asia/Manila

Input: `paper_trades_all_2026-11-08T1003.json`.
SHA-256: `706f3b0e089337497e568d225c39812a1da267fe33dfbb6cdad4d976c881ab70`.
Neither this input nor the earlier `pt.json` is changed or committed.
The reproducible results are saved in
[paper-trade-review-2026-11-08T1003.json](paper-trade-review-2026-11-08T1003.json).

## Comparison with the earlier run

Both runs scan the same 1,931 US symbols over 250 replay sessions, ending
November 6. Both have 466,651 pattern evaluations, 26 detections, identical
execution costs and the same exit engine hash. The sole difference in their
exported run settings is DII Filter becoming Shadow. This is another replay
of the same period, not a new independent sample of three additional trades.

| US measure | Earlier DII Filter | New DII Shadow |
| --- | ---: | ---: |
| Detections | 26 | 26 |
| Rejected | 25 (96.15%) | 23 (88.46%) |
| Filled / open | 1 / 0 | 3 / 0 |
| Long / short fills | 0 / 1 | 1 / 2 |
| Realized net P&L | +$984.29 | +$620.78 |
| Return on initial $100,000 | +0.98429% | +0.62078% |
| Win rate | 100% of 1 | 33.33% of 3 |
| Profit factor | Undefined; no losses | 2.708 |
| Maximum drawdown from saved daily marks | $309.69 | $673.20 |
| Journal rows within the saved scan window | 0 | 49 |

Opportunity flow and auditability improved. Profit did not improve in this
replay: SHOP's result is unchanged, and the two additional trades lost a
combined $363.50. The earlier one-trade win rate was not a useful benchmark.
The PSE book remains idle with no trades; PHP is not combined with USD.

## All three closes reproduce, and both losses respect the budget

| Trade | Side | Entry / exit sessions | Net P&L | Net return | Exit |
| --- | --- | --- | ---: | ---: | --- |
| SHOP | Short | Jan 15 / Jan 22 | +$984.29 | +9.94899% | Paper protective trail |
| BXSL | Long | Mar 5 / Mar 6 | -$299.77 | -3.00000% | Paper protective stop |
| SBET | Short | May 13 / May 14 | -$63.74 | -0.63749% | Paper protective trail |

The offline review verified every saved pre-bar order, OHLC exit decision,
fill, fee, holding-bar count and total cash reconciliation. There are no
open positions, ambiguous stop/target bars, missing saved OHLC bars, or gap
exits among these trades. These are historical simulations, not observed
live quote fills.

**BXSL:** 411 shares filled at $24.312149, or $9,992.29 notional. The
cost-aware stop was $23.642549; 0.05% exit slippage produced a $23.630728
sale. Both fees totaled $19.70. Net loss was $299.7688, exactly 3% of entry
notional. The wider pattern stop was $23.252140, so the independent paper
order closed the trade earlier. Its -1.08925 net R is not a stop violation:
the R denominator is gross entry-to-trigger price risk, while net P&L also
includes fees and exit slippage.

**SBET:** 1,399 shares sold short at $7.146425, or $9,997.85 notional. The
May 13 close at $6.96 represented +2.36251% after hypothetical cover costs.
The three-percentage-point giveback therefore allowed a floor of -0.63749%,
placing the next buy stop at $7.174072. May 14 opened at $6.94, then its high
of $7.48 crossed that stop. The $7.177659 executed cover and $20.04 total
fees yield -$63.7351. This is a correctly tightened short stop, not reversed
short accounting. A positive close smaller than the giveback allowance can
still end in a small loss. Moving it to breakeven solely to turn this sample
green would be fitting a rule to one outcome.

**SHOP:** exactly reproduces the earlier +$984.29 result and its three-point
giveback from a +12.94899% best surviving net close. All three exits came
from independent paper protection; none was a Kronos exit. Long exits sell,
and short exits buy back with adverse upward slippage.

The 3% budget is net of configured transaction costs, per entry notional.
It remains an order-trigger budget; this sample contains no gap stress that
could demonstrate fills beyond that budget.

## Kronos now controls almost all opportunity flow

The journal has 49 rows: 20 DII shadow votes, 23 rejections, three queues and
three fills. These correspond to 26 distinct entry decisions in one run.
Every journal timestamp falls within that run's scan window. Shadow votes
and queue/fill events must not be counted as additional signals.

| Side | Detected | Kronos opposed | Kronos no opinion | Filled |
| --- | ---: | ---: | ---: | ---: |
| Long | 18 | 9 | 8 | 1 |
| Short | 8 | 2 | 4 | 2 |
| Total | 26 | 11 | 12 | 3 |

There are no worker failures or capacity rejections in these 23 cases. Each
has saved model-vote evidence and a 60-bar decision context. Of the 12
no-opinion cases, eight have a majority inside the sideways band and four
split up/down/flat. Confirm mode requires a directional majority, so its
rejection of a no-opinion case matches the selected policy. This is a policy
bottleneck, not evidence that the worker malfunctioned.

The model's saved checkpoint provenance still says identity and historical
holdout status are unverified. Three stochastic forecasts are not three
independent models. Neither these votes nor three realized outcomes validate
the gate's predictive edge. Rejected signals mostly contain a historical
prefix ending at the decision; they do not provide complete subsequent
execution paths or future Kronos exit votes. Their full alternative P&L
cannot be recovered from this export.

## What to retain and what to test next

Retain symmetric long/short protection and the cost-aware 3% stop/trail.
There is no evidence here to disable longs after one losing long, disable
shorts after one losing short, or widen stops. Shorts netted +$920.55 across
two trades; the sole long lost $299.77. Those sample sizes cannot establish
a reliable side preference.

The saved-bar 2R target comparison lowers total P&L to $200.91 because it
cuts SHOP's gain. Keeping entry-based trailing and existing paper protection
reproduces $620.78. Removing the best trade leaves -$363.50. The apparent
profit factor and average net R (+0.7673) therefore depend on one winner.
An extra 5 basis points of cost per side reduces fixed-fill P&L to $591.52;
10 basis points reduces it to $562.26. These are accounting stresses, not
alternative execution paths.

The useful next paper experiment is a separately recorded comparison of
Kronos entry Confirm against Shadow, holding DII Shadow, the universe,
execution costs and 3% protection fixed. Keep entry and exit mode changes
separate so their effects can be measured. Evaluate another period as well;
the same replay should not become the optimization target. Do not reduce the
vote threshold or sideways band merely to increase the count. No trading
thresholds were changed as part of this follow-up.

## Audit defect found and fixed

SHOP has a filled log row but no `signal_id` on its ledger trade. Its first
accepted signal passed DII, so there was no preceding shadow/rejection log
to assign an ID. The order queue copied the signal before the queued log
assigned that ID. BXSL and SBET already had IDs from earlier DII shadow rows,
which explains why they linked correctly. The reviewer appropriately flags
missing identity linkage; here that does not mean the log rows are absent.

Decision IDs are now assigned before gates or pending queues copy a signal.
An older queued decision without an ID receives one before the execution
copy is made. Filled journal events also include the actual trade ID,
executed price and quantity, resolved rules, fees and opening-check evidence.
The original queued event remains an immutable pre-fill snapshot. The export
also retains entry-universe and execution-watchlist counts that were being
removed by its field whitelist. Existing export history is not rewritten.

Regression tests cover longs and shorts with a fresh first accepted signal,
queue persistence/reload, migration of an older unidentified queue, matching
decision/trade IDs, and exact fill economics. Existing accounting and exit
tests remain in the validation suite.
Validation: **2,647 passed, 154 skipped, 10 deselected**, plus five subtests,
in the full non-slow suite. The focused paper/audit suite passed 130 tests.
`git diff --check` and input checksum verification also passed.

```sh
.venv/bin/python -m analysis.paper_trade_review \
  paper_trades_all_2026-11-08T1003.json \
  --output /tmp/paper-review-2026-11-08T1003.json
```
