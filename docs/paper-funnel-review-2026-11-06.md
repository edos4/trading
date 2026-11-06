# Paper entry funnel and exit review — November 6, 2026

Input: `pt.json`, exported **November 6, 2026 at 09:45:55 Manila**
(`2026-11-06T01:45:55.465068+00:00`). SHA-256:
`b386c26ae8bdcc25b073034782cebee045d710a2396c8cde06c0b21754db9d5f`.
This is the **one-closed, zero-open** export. Earlier reports in this
repository analyze different files also named `pt.json`.

The companion JSON reproduces every saved order, executed fill, fee and
account total from the export's own OHLC evidence. Its US baseline is
verified with no malformed saved trade candles. The PSE book is empty;
USD and PHP are never combined. No account or historical tape was modified.

## The entry funnel is the main problem in this run

The US run requested 2,000 symbols, Patternsv2, September 1 onward, with
volume confirmation off, Kronos entry confirmation on, and Kronos exits on.
The final simulated session is November 5. The log contains 63 events but
only **62 distinct entry decisions**: CGON's queue and fill count once.

| Observed outcome | Long decisions | Short decisions | Total |
| --- | ---: | ---: | ---: |
| DII rejected | 20 | 29 | 49 |
| Kronos no directional majority | 2 | 3 | 5 |
| Kronos reported date mismatch | 3 | 2 | 5 |
| Kronos opposed entry | 2 | 0 | 2 |
| Filled | 0 | 1 | 1 |
| Total | 27 | 35 | 62 |

**98.39%** of observed decisions were rejected. DII eliminated **79.03%**
before Kronos; only 13 candidates reached confirmation and one passed.
The most recent scan's three DII rejections are not the whole-run funnel.
The side setting is already enabled: shorts are not being rejected by a
long-only US policy. All recorded patterns are double-bottom/double-top.

DII accepts long bulls only within 17–40% and short bears only within
26–42%. These are narrow context bands; balanced 50/50 passes neither.
This export contains no subsequent prices or model votes for the 49
DII-rejected entries. It cannot establish that these bands improve results,
that the rejected trades would win, or that simply accepting all entries
would increase profit. Detector and confidence thresholds are not fitted
to this single realized trade.

**Change:** paper accounts now record DII in shadow mode by default. The
vote remains in Logs, while candidates proceed to the remaining gates.
`PAPER_DII_GATE_MODE=filter` explicitly restores the paper veto. The existing
`DII_GATE_SHADOW=true` override remains supported. Non-paper scanners keep
their existing gate. The run header and exported settings identify DII mode.
This makes 49 observed candidates eligible for further checks; it does
not imply 49 additional fills or any profit counterfactual. Kronos confirm
still requires a valid agreeing majority, including at next-open fills.

## The five date errors actually conceal invalid context

A separate read-only check of the current historical database found that
all five histories reach the requested date. Each fails the confirmation
worker's OHLC validation in its last 400 bars:

| Symbol | Decision date | Invalid OHLC ranges | Consecutive valid bars at end |
| --- | --- | ---: | ---: |
| TLYS | September 11 | 2 | 6 |
| NBR | September 21 | 5 | 1 |
| WT | October 14 | 5 | 1 |
| CTRI | November 3 | 4 | 8 |
| RRX | November 3 | 3 | 20 |

These are current-database diagnostics, distinct from immutable export
evidence; no historical model run was repeated. They reproduce the worker's
input-validation failure without loading weights. Dropping history to the
valid suffix would leave fewer than its required 60 bars for every symbol.

The worker returned a per-symbol `error` with empty returns. The caller
discarded that field, built a forecast with an empty anchor date, and then
called it a date mismatch. **Fixed:** preserve the symbol's actual worker
error, distinguish invalid OHLC context from insufficient history, and show
expected/received dates for real anchor mismatches. Errors cannot vote,
approve a trade, request a model exit, or populate the cache. Healthy symbols
in the batch still work. Invalid ranges remain blocked; no prices are
silently repaired. Source-data remediation remains necessary for these names.

## CGON's short exit is correct, including its small net loss

CGON was sold short at the September 25 open and bought back September 28.
It held for two observed bars, three calendar days, with zero configured
slippage and 0.1% commission per side.

| Measure | Value |
| --- | ---: |
| Quantity / executed entry | 144 shares / $69.3200 |
| Entry notional | $9,982.08 |
| Structural stop / effective ATR stop | $80.9700 / $74.2923 |
| Independent initial protective stop | $71.2590 |
| Best surviving close | $67.1500 |
| Best net liquidation return | +2.93354% |
| Next bar's protective trail | $69.227524 |
| Executed gross price profit | +$13.3165 |
| Entry plus exit fees | $19.9508 |
| Net result | **−$6.6343 (−0.06646%, −0.02376R)** |

The independent stop already enforces a **3% net loss budget per trade's
entry notional**, including both commissions and configured exit slippage.
Its trail gives back three percentage points of that notional from the best
positive net close. Here +2.93354% minus 3% permits −0.06646%. A buy-to-cover
below entry is therefore a small gross winner and still a net loser. This
is not a reversed short P&L calculation. The prior close's marked P&L was
+$302.50 after entry fees; net liquidation value was +$292.83 after both fees.

The pattern's separate 14.345% activation ladder never armed. The paper
protective trail did. The audit now reports these separately and counts
either as an armed trail; previously it misleadingly counted only the
pattern trail. Baseline drawdown across the two saved marks is $309.13.
An additional 5 basis points of friction per side would make this same
fixed-fill trade −$16.61; a 10-basis-point stress gives −$26.59. These are
accounting stresses, not alternative order simulations.

With only one loss and no longs, neither the reported 0% win rate nor zero
profit factor estimates an edge. The bounded target/trail comparisons do
not improve this trade. Keep both US directions and the existing cost-aware
3% policy; do not choose a breakeven threshold simply to turn CGON green.

## Fixed a separate live exit gap

The scanner distinguished forming bars from completed bars, but paper
management returned before checking any orders on forming updates. A daily
position could cross its protective stop during the session and remain
open until the daily candle completed. A queued completed-close exit could
also wait until the next bar completed.

Live paper scans now check a forming candle's **observed last price** for
existing resting safety, structural, trailing and target orders. Long
closes sell; short closes buy back; slippage and both commissions settle
through the same accounting as completed-bar exits. Queued close decisions
execute at the first observed price of the following bar. Positions removed
by an exit cannot settle again; cooldown records the loss immediately.

The range before observation is not a known execution path. Forming-bar
highs/lows cannot manufacture earlier fills, arm a close-based trail, or
advance completed-bar timers. A crossed stop fills at the observed price,
including gaps between polls. The saved trail still tightens only from a
surviving completed close. Replay continues its causal OHLC order model.
Both old open positions and saved trails receive the quote protection.

Exports identify these fills as `exit_check.observation_kind=live_quote`,
with a point-price OHLC and the source bar timestamp. The offline review
explicitly refuses to certify them as completed-candle replays. The existing
export's fills remain unchanged and reproduce exactly. There are no open
positions in this input to close or rewrite.

A 3% stop is a trigger, not a guaranteed maximum realized loss through
polling gaps, market gaps or slippage. See the
[SEC stop-order bulletin](https://www.investor.gov/introduction-investing/general-resources/news-alerts/alerts-bulletins/investor-bulletins-15).
These are simulated orders managed by the scanner, not broker-hosted stops.
The research ledger omits short borrow availability, borrow fees and margin.

## Reproduction and validation

```sh
.venv/bin/python -m analysis.paper_trade_review pt.json \
  --output /tmp/paper-funnel-review.json
```

Regression tests exercise both directions, ordinary and gapped quote exits,
fees/slippage and cash reconciliation, restart protection, double-settlement
prevention, wrong timeframes, pre-entry observations, close-confirmed orders,
next-bar market exits, scanner integration, explicit DII filtering, per-symbol
worker errors, and evidence that a completed-bar replay cannot certify.

Validation: **167 focused tests passed**. The exact proposed source also
passed the full non-slow suite in a temporary HEAD checkout without the
workspace's pre-existing detector/audit edits: **2,403 passed, 154 skipped,
10 deselected**. Only synthetic market settings and a dummy UI password
were supplied; no private `.env` or runtime data was copied. The mixed
workspace's earlier full run reported 2,492 passes and one unrelated Laya
cancellation race (`loading` instead of `cancelled`); unchanged HEAD's
isolated test and the proposed commit's isolated full suite both passed it.
Laya code is unchanged. Async scanner/HTTP tests required local socket access.

A fresh paper replay can now collect DII-shadow outcomes under the same
Kronos and protection rules. Compare a separate period with explicit DII
filter mode before deciding whether to restore its veto. Rejected-candidate
profits and improved strategy expectancy cannot be inferred from this file.
