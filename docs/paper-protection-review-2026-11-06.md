# Paper long/short protection review — November 6, 2026

Input: `pt.json`, exported **November 6, 07:38:54 Manila**
(`2026-11-05T23:38:54.481352+00:00`). SHA-256:
`d2879e0741257db850df7c47c5d39570e24bc6aef02051984c65f4027b92dad1`.
This is the seven-closed/two-open export, distinct from earlier files with
the same name. Its last simulated session is November 4. The companion
JSON contains the reproducible calculations and each trade's comparison.

## What is causing the losses

The US book uses independent approximately $10,000 entries, permits longs
and shorts, charges 0.1% per side and assumes zero slippage. The PHP book
is empty; no currencies are combined.

| Measure | Observed result |
| --- | ---: |
| Closed / open | 7 / 2 |
| Realized net P&L | −$1,206.58 |
| Open marked P&L, after entry fees | +$403.21 |
| Total marked P&L | −$803.37 |
| Total if both opens liquidated at their last close, including exit fees | −$823.71 |
| Closed profit factor / win rate | 0.644 / 28.6% |
| Average winner / loser | +$1,093.62 / −$678.76 |
| Closed longs | 0/3 wins; −$1,881.58 |
| Closed shorts | 2/4 wins; +$675.00 |
| Fees already paid, including open entry fees | $157.25 |
| Maximum daily marked drawdown | $2,856.79 |
| Peak overlapping positions, including entry sessions | 5 |

The loss arithmetic and short-side direction reproduce from the saved
rules and bars. The central defect relative to the requested risk budget is
**policy scope**: `stop_loss_pct=0.03` applies only to legacy/indicator
scanner entries. Patternsv2 bypasses it and uses structural stops combined
with ATR caps of approximately 5%–8% in this export. The five hard-stop
losses therefore realize **5.23%–8.21% net**, including commissions. None
is an opening-gap exit. These losses were permitted by the saved rules;
they were not caused by a reversed short P&L calculation.

The trails are also too distant for the requested protection. Every saved
Patternsv2 trade requires approximately 2 initial R to activate, equivalent
to **10.08%–16%** favorable price movement here, and gives back about 2R.
No trail activates in this run. MRT reaches +0.67R and STVN +0.48R on a
surviving close, then loses $820.71 and $691.53. Even the profitable GLXY
and SPCE shorts exit through recorded next-open Kronos orders before
their trails activate. Removing shorts would worsen this sample.

The three closed longs never establish a profitable surviving close. Their
entry quality remains a concern, but there is no rejected-candidate tape
or post-exit path here to justify changing detector thresholds, removing
DII, widening stops, or assuming a later recovery. The observed entry
funnel has 28 decisions: 12 longs (8 DII rejections, 4 fills) and 16 shorts
(10 DII rejections, 5 fills, one unresolved queue). Queue/fill pairs count
once; the two `exited` log events are closes, not new short candidates.

## Implemented policy

Every new paper entry receives an independent persisted
`net-loss-trail-v1` protective order. Pattern stops, targets and trails
remain available to exit sooner. This applies to both sides, indicators,
direct entries and queued next-open fills. PSE remains long-only.

* **Initial net loss budget: 3% of the trade's executed entry notional.**
  The trigger includes both commission legs and configured adverse exit
  slippage. It is not 3% of total account equity. With $100 entry and 0.1%
  fees per side, zero-slippage raw triggers are approximately $97.1972 for
  a long and $102.7972 for a short: either ordinary stop realizes −3% net.
* **Continuous close-based trail: give back at most another 3% of entry
  notional from the best positive net liquidation return.** There is no
  separate 10%–16% activation hurdle. A +1% net close tightens the loss
  floor to −2%; +3% raises it to breakeven; +8% raises it to +5%.
  The entry-notional distance gives both sides the same dollar budget.
* **Causal execution.** A surviving completed close tightens the next bar's
  order. The current high/low cannot arm an order retroactively on the same
  bar. The order is resting intrabar even when the pattern waits for close
  confirmation. An opening target or queued market exit executes before a
  later intrabar stop; ambiguous intrabar stop/target touches favor protection.
* **Existing open positions are adopted on their next management pass.**
  Only already completed marks can seed their trail. A position already
  through its new stop closes at the next observed open; no earlier fill is
  invented. Existing historical rules and closed trades are not rewritten.
  Legacy and causal positions are covered; disabling new short entries does
  not disable protection of existing shorts.
* **Persistence and evidence.** The policy, costs, adoption timestamp, peak
  net return and active stop survive reloads and Export Trades. Each exit
  check records the prior protective order separately from the new state.
  New positions' initial R includes their actual protection; adopted
  positions retain their historical R denominator.
* **Replay parity.** Historical-stream backtests install the same protection
  with their own fees/slippage. The offline legacy research walk continues
  to reproduce its original pattern ladders; it is not a simulation of this
  additional paper policy. Unknown runtimes remain rejected; the export's
  audited pre-change runtime is explicitly compatible when adding protection.

Entries whose known entry/exit execution costs already exhaust the budget
are rejected before charging fees or changing cash. Old pending entry
orders with an incompatible runtime must be regenerated; they are explicitly
rejected rather than filled under an unrecorded execution-policy change.

## Saved-entry comparison

Entries, quantities, commissions, slippage, structural orders and observed
external Kronos orders are held fixed. Only the additional protective order
changes. Bars end at each original exit or last open mark. No subsequent
prices, detector rescans or new model predictions are used.

| Symbol | Side | Original net P&L | With protection | New exit / bars |
| --- | --- | ---: | ---: | --- |
| STRR | Long | −$764.30 | −$299.86 | Stop / 1 |
| GLXY | Short | +$1,457.48 | +$1,457.48 | Kronos / 6 |
| SPCE | Short | +$729.75 | +$729.75 | Kronos / 5 |
| MRT | Short | −$820.71 | +$54.70 | Trail / 4 |
| CSGP | Long | −$521.68 | −$299.02 | Stop / 1 |
| STVN | Short | −$691.53 | −$201.41 | Trail / 2 |
| CTRI | Long | −$595.60 | −$299.89 | Stop / 1 |
| GSHD | Long, still open | +$393.68* | +$393.68* | Open / 1 |
| ZBRA | Short, still open | −$10.81* | −$10.81* | Open / 1 |

\*Hypothetical liquidation P&L at the last observed close, with exit fees;
these are not executed closes or counted wins.

Closed P&L changes from **−$1,206.58 to +$1,141.76**, an improvement of
**$2,348.34** on these saved entries. Profit factor becomes 2.038, with
three wins and four losses. Including hypothetical open liquidation changes
the total from **−$823.71 to +$1,524.63**. All seven revised exits resolve
within the available saved bars; there are no censored revised closes.

At GSHD's last close the new resting stop is about **$54.7254**, versus the
old ATR hard stop of $49.772. This protects roughly +0.95% net, subject to
gaps. ZBRA's cost-aware initial stop is about **$342.0525**, versus the old
ATR cap around $357.4257. These are calculated policy states, not edits to
the actual running ledger.

This is a risk-budget correction requested for both sides, not a fitted
entry strategy or proof of profitability. Seven closes are a small,
dependent sample: the original descriptive 95% win-rate interval spans
8.2%–64.1%. Earlier exits can change later position eligibility and signals
in a full run. A fresh isolated historical-stream replay and an independent
time window are needed before claiming improved expectancy. Targets are
not tuned to this sample.

## Evidence limits and validation

Three saved candles have opens below their reported lows: MRT on October
15 and 19, STVN on October 29. The audit's explicit opt-in reproduces raw
orders/fills and flags those anomalies; `baseline_reproduced=true` and
`baseline_verified=false`. It does not repair the tape. Strict validation
still rejects those ranges by default. The revised MRT/STVN exits precede
the malformed bars, but that does not certify the underlying dataset.

A stop cannot guarantee a maximum realized loss through a gap, halt or
unmodeled liquidity/slippage. The simulator fills through-stop opens at the
observed open on both sides, as described in the
[SEC's stop-order bulletin](https://www.investor.gov/introduction-investing/general-resources/news-alerts/alerts-bulletins/investor-bulletins-15).
The policy executes when the scanner/replay manages an observed bar; it is
not a broker-hosted live order. The research ledger also omits short borrow
costs/availability and portfolio financing limits.

Reproduce without modifying an account or downloading tape:

```sh
.venv/bin/python -m analysis.paper_trade_review pt.json \
  --allow-inconsistent-ohlc \
  --output /tmp/paper-protection-review.json
```

Validation: full non-slow suite **2,409 passed, 154 skipped, 10 deselected**.
Two further adoption cases and stronger close-only trailing assertions
passed in the **54-test** focused protection/audit run. Tests cover both
sides, fees/slippage, entry-session stops, gaps, tighter structural stops,
opening targets, pending exits, legacy adoption, future-mark exclusion,
restart/export reproduction, unchanged inputs and historical-stream parity.
Socket-based tests required local socket access outside the sandbox.
The exact proposed commit was also tested in a temporary HEAD checkout,
excluding pre-existing detector edits and untracked audit tests, with synthetic
market settings and a dummy UI password: **2,319 passed, 154 skipped,
10 deselected**. No private `.env` was copied.
