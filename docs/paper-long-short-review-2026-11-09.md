# Paper long/short exit review — 9 November 2026

Input: workspace `pt.json`, exported **9 November 2026, 21:30 Asia/Manila**
(`2026-11-09T13:30:45.150152+00:00`). SHA-256:
`93a6f6da407e7943082e95104fad39e526c2042270e16c42e1213092cb0f15fe`.
Calculations and individual verified trades are saved in
[paper-long-short-review-2026-11-09.json](paper-long-short-review-2026-11-09.json).
The input export and runtime account ledgers were not changed.

## Results and evidence

This is the 1,926-symbol US Patternsv2 historical replay from November 10, 2025
until November 6, 2026. It completed 250 sessions and ended with `end_of_tape`,
37 fills, 36 closed trades and one open short. DII was Shadow, volume confirmation
was off, and Kronos was Confirm for entries and Exit for held positions. The
PH/PHP book is idle with no trades; its capital is not combined with USD.

Every saved fill, fee, prior order, exit reason, holding-bar count, open mark
and total account P&L reproduces from the saved OHLC evidence under the revised
engine. There are no inconsistent OHLC ranges, ambiguous stop/target bars or
unresolved execution intervals recorded in this export. This is an arithmetic
and execution-rule verification, not independent certification of source prices,
corporate actions, borrow availability or real fills.

| Measure | US result |
| --- | ---: |
| Closed / wins / losses | 36 / 7 / 29 |
| Realized net P&L | -$4,712.13 |
| Open marked P&L / total marked P&L | -$95.49 / -$4,807.62 |
| Open hypothetical liquidation P&L, including remaining costs | -$110.58 |
| Profit factor / win rate | 0.387 / 19.44% |
| Average winner / loser | +$425.44 / -$265.18 |
| Expectancy / average net R | -$130.89 / -0.480R |
| Break-even win rate at observed payoffs | 38.40% |
| Closed gross P&L before fees / closed fees | -$4,000.66 / $711.47 |
| Fees already paid, including open entry fee | $721.44 |
| Maximum drawdown from saved daily marks | $5,053.40 |
| Median / maximum holding sessions | 2 / 15 |
| Maximum overlapping positions, including entry sessions | 3 |

The observed win rate is far below the break-even rate. Its descriptive Wilson
95% interval is 9.75%–35.03%; overlapping positions are not independent trials.
The loss remains substantial even before fees, so reducing commissions alone
cannot repair this sample. An extra 0.05% execution cost per leg, holding fills
fixed, changes realized net P&L to -$5,067.86.

## Longs, shorts and entries

| Side | Closes | Wins | Net P&L | Profit factor | Mean net R |
| --- | ---: | ---: | ---: | ---: | ---: |
| Long | 22 | 3 | $-3,447.36 | 0.304 | -0.574 |
| Short | 14 | 4 | $-1,264.77 | 0.538 | -0.334 |

Both sides lose in this replay. Short positions have the correct accounting:
they receive sale proceeds, retain an offsetting liability, buy to cover, and
pay adverse upward exit slippage plus both fees. Long positions sell to close
with adverse downward slippage. The funded account cash reconciles to these
results. Disabling shorts would remove losing trades from this observed sample
but would not establish a profitable long strategy; both directions remain enabled.

| Pattern | Closes | Wins | Net P&L | Profit factor |
| --- | ---: | ---: | ---: | ---: |
| ascending_channel | 6 | 2 | $-203.73 | 0.805 |
| ascending_triangle | 1 | 0 | $-264.51 | 0.000 |
| descending_channel | 2 | 0 | $-772.85 | 0.000 |
| descending_triangle | 1 | 0 | $-295.54 | 0.000 |
| falling_wedge | 16 | 2 | $-2,488.71 | 0.308 |
| symmetrical_triangle | 10 | 3 | $-686.80 | 0.600 |

Twelve positions, six longs and six shorts, stop in their entry session and
lose $3,566.15 together. The median entry-session range is 1.34 times the
initial gross stop risk. APLD has a 7.20R entry-session range. This is a poor
fit between these breakout entries and a strict 3% budget, rather than evidence
that the short stop is backwards. Widening the stop conflicts with the requested
budget. Muting falling wedges based on sixteen closes or selecting a volatility
cutoff from these observed outcomes would fit this inspected replay. No such
P&L-selected filter is enabled.

The journal contains 990 rows, all within the saved scan window, with matching
fill IDs for all 37 trades. It records 514 distinct entry decisions: 333 longs
and 181 shorts. The cumulative funnel has 477 rejections (92.80%), including
475 attributed to Kronos, one structural opening-gap rejection and one symbol
cap. Kronos rejections include opening-price rechecks; queues and fills are
stages of a decision, not extra opportunities. The export lacks the complete
later paths of rejected entries, so it cannot establish that removing the model
gate or restoring the DII veto would improve expectancy.

All 37 entry forecasts have verified file identity but explicitly have
`out_of_sample_eligible=false`. Their checkpoint selection boundary is November
6 and declared eligibility begins November 7, after the replay ends. This is
not an untouched test of the selected model. Three forecast samples do not
provide three independent models. Future entry-policy evaluation needs a frozen
checkpoint and later, previously uninspected sessions with the same costs and
funding rules.

## Stops, trailing exits and gap losses

| Executed exit | Closes | Wins | Net P&L |
| --- | ---: | ---: | ---: |
| stop_loss | 20 | 0 | $-5,982.71 |
| trailing_stop | 10 | 3 | $-392.26 |
| kronos_exit | 6 | 4 | $1,662.83 |

The export already has independent `net-loss-trail-v1` protection on every
position. Its initial budget is **3% of executed entry notional, net of both
fees and configured adverse exit slippage**. It is not a 3% account-equity
risk allocation. The nearer structural or pattern stop may exit earlier.
For a $100 executed entry, 0.1% fees per side and 0.05% exit slippage, the raw
safety triggers are about $97.25 long and $102.75 short; ordinary fills at
those triggers lose exactly 3% net.

Nineteen of the twenty hard-stop exits meet the 3% net budget. Seven of the ten
`trailing_stop` exits lose money because a peak below +3% still permits a
negative floor after three percentage points of giveback. For example, CVX
peaks at +2.334% net and exits at -0.666%; LNG peaks at +0.218% and exits at
-2.782%. This is the saved allowance working in the correct direction. A
breakeven floor after a smaller gain would be an additional strategy change.

Two realized losses exceed 3%:

* **NVMI long:** the resting sell stop was $366.624927, but the next session
  opened at $365.510010. After slippage, the sale was $365.327255 and the net
  loss was $323.01 (-3.2953%).
* **MTZ short:** the prior surviving close reached +6.9893% net, raising the
  intended floor to +3.9893% and the buy stop to $296.700538. An opposing model
  vote also queued a next-open market exit. The next open was $323.880005 and
  the actual cover was $324.041945, losing $475.55 (-4.7967%). Both orders faced
  the same gap. Replacing the recorded model reason with a stop reason cannot
  recover a fill at an unavailable price.

ACLS also gaps through a profitable short trail: its best surviving net close
is +11.1017%, its intended floor is +8.1017%, and its opening cover realizes
+4.2093%. Thus a gap can exceed the giveback allowance even when the final
position remains profitable. The audit now records opening gaps through the
paper stop independently of the executed exit reason, so a queued Kronos order
no longer hides the gap diagnosis for MTZ.

A stop is a trigger, not a guaranteed fill price, as described in the
[SEC investor bulletin on stop and trailing orders](https://www.investor.gov/introduction-investing/general-resources/news-alerts/alerts-bulletins/investor-bulletins-15).
Historical bars and live polling cannot guarantee a 3% maximum across overnight
gaps or missed observations. The engine retains the actual observed fill and
costs instead of clipping a loss to 3%.

## Exit repairs implemented

1. **Live protection now trails favorable observed prices.** Previously a
   favorable intraday quote left the independent stop at its previous daily
   close level. A long could rise from $100 to $110 and fall back without
   retaining that gain; the mirrored short had the same problem. Each quote
   now checks existing orders first, then tightens the independent stop from
   a surviving observed net return. With zero costs, those $110 long / $90
   short observations install $107 sell / $93 buy stops. A subsequent observed
   reversal executes with the correct direction, fees and slippage. A move
   between polls can still cross the trigger.
2. **Current ranges cannot fabricate earlier fills.** A live entry is itself
   recorded as a price observation. The day’s earlier high/low cannot stop it
   before it existed. After a live quote, the same completed bar uses the
   observed close to execute resting orders, while retaining its complete
   OHLC for pattern close decisions and future references. The next bar can
   use its full range because that order existed before it opened. No forming
   high/low is used to ratchet a live trail.
3. **Time and session guards protect both sides.** Delayed quotes, quotes before
   entry, older completed bars and wrong-timeframe observations cannot execute
   a new stop. Daily/weekly period identity handles timestamp changes within
   the same session; a later print is not a new bar that can execute a queued
   next-open exit.
4. **Saved open positions and runtime recovery retain protection.** Existing
   positions gain observed-price trailing at their next valid live quote.
   The last quote and each stop ratchet persist in the account and export.
   An incompatible pattern runtime still permits the independent safety exit.
   Long sells and short buybacks use the same settlement function once.
5. **Evidence states the observation used.** `live_quote` exits retain their
   point observations. `live_bar_close` retains both full `ohlc` and point
   `resting_ohlc`. The daily-only review explicitly refuses to certify a
   quote-managed trade, including one that eventually closes on a later bar.

The old export contains no live quote sequence. All of its historical exits
and P&L remain exactly reproducible. There is no claim that the live repair
would have made this replay profitable. Both long and short entries remain
allowed in the US book; PSE retains its existing market policy.

## Remaining AME short

AME sold **44 shares at $226.726576** on November 6. Its last observed close is
**$228.669998**. The saved marked loss is **$95.49**; a hypothetical immediate
cover at that last price, including adverse slippage and remaining fees, would
lose **$110.58 (-1.1085%)**. This is an illustration at the saved mark, not an
executed close or a current quote.

The independent buy-to-cover stop is **$232.952102**, nearer than the
structural/ATR hard stop of $239.797363; its measured target is $183.1163.
There is no pending exit and no saved price breach. Reaching the end of a
replay does not itself justify closing the ledger at a fabricated later price.
On resumed management, the stop remains active and favorable observed live
prices can tighten it. The export and runtime ledger were not force-closed.

## Target diagnostic and further improvement

The median measured target is **11.81 initial R**, while the median trade lasts
only two sessions. No measured target executes. A bounded 2R target-cap
diagnostic on the same saved entries changes realized net P&L from -$4,712.13
into **-$2,748.05**, with profit factor 0.610 and nine winners. Long P&L becomes
-$2,693.62 and short P&L -$54.43. Eight exits change; ADNT loses $326.88 of its
original gain, while MTZ improves by $996.02. The revised strategy still loses.

This comparison holds entries, quantities, external model decisions and
observed coverage fixed. It is not a rescan or a funded portfolio replay with
reallocated capital, and it was selected for inspection after seeing outcomes.
Other repository reviews show a 2R cap hurting earlier samples. Therefore the
existing optional target cap remains available, with its default unchanged.
The next useful experiment is to freeze the current exits and model, collect
future decisions and complete later paths, then compare entry quality and
one predeclared target policy on that new data. Stops alone cannot create a
positive expectancy from this export.

## Verification

261 focused regression tests pass; 23 tests skip their optional fixtures and
five slow tests are deselected. Coverage includes mirrored long/short net
budgets, observed-price reversals, restart persistence, stale observations,
changing daily timestamps, legacy and causal exits, runtime fallback, entry
protection, accounting, stream/backtest parity and export evidence. The
broader suite was attempted but stalled in existing asynchronous integration
paths (web TestClient and the Kronos exit test); it was interrupted and is
not reported as passing.

Reproduce the saved-trade audit with:

```bash
.venv/bin/python -m analysis.paper_trade_review pt.json --output /tmp/paper-review.json
```

The committed JSON is a compact projection of that audit, omitting the repeated
symbol-coverage map and unchanged diagnostic rows. The original export remains
necessary to re-verify the recorded observations.

## Individual trades

All results include the configured execution costs. Open AME is a hypothetical
liquidation value and is excluded from closed win/loss statistics.

| Symbol | Side | Entry session | Exit/latest session | Sessions | Net P&L | Net return | Exit |
| --- | --- | --- | --- | ---: | ---: | ---: | --- |
| CVX | Short | 2025-11-20 | 2025-12-01 | 7 | $-66.01 | -0.666% | trailing_stop |
| FLO | Long | 2025-12-12 | 2025-12-23 | 8 | $-137.41 | -1.375% | trailing_stop |
| BWIN | Long | 2026-01-09 | 2026-01-12 | 2 | $-299.64 | -3.000% | stop_loss |
| CRC | Long | 2026-01-09 | 2026-01-12 | 2 | $-299.49 | -3.000% | stop_loss |
| NTES | Short | 2026-01-16 | 2026-01-28 | 8 | $136.61 | +1.376% | trailing_stop |
| APLD | Short | 2026-02-06 | 2026-02-06 | 1 | $-299.99 | -3.000% | stop_loss |
| BXSL | Long | 2026-03-05 | 2026-03-06 | 2 | $-299.77 | -3.000% | stop_loss |
| CSTL | Long | 2026-03-17 | 2026-03-18 | 2 | $-299.46 | -3.000% | stop_loss |
| AFG | Long | 2026-04-10 | 2026-04-15 | 4 | $-297.31 | -3.000% | stop_loss |
| ADNT | Long | 2026-04-09 | 2026-04-21 | 9 | $851.42 | +8.522% | kronos_exit |
| JKHY | Long | 2026-05-04 | 2026-05-05 | 2 | $-298.40 | -3.000% | stop_loss |
| IOT | Long | 2026-05-20 | 2026-05-20 | 1 | $-299.44 | -3.000% | stop_loss |
| MORN | Long | 2026-06-01 | 2026-06-02 | 2 | $399.83 | +4.009% | kronos_exit |
| KKR | Short | 2026-06-04 | 2026-06-04 | 1 | $-299.19 | -3.000% | stop_loss |
| PRGO | Long | 2026-06-09 | 2026-06-11 | 3 | $-166.56 | -1.667% | trailing_stop |
| SCHW | Long | 2026-06-11 | 2026-06-18 | 6 | $254.77 | +2.570% | trailing_stop |
| LNG | Long | 2026-06-29 | 2026-07-01 | 3 | $-276.95 | -2.782% | trailing_stop |
| ACLS | Short | 2026-07-06 | 2026-07-09 | 4 | $418.40 | +4.209% | trailing_stop |
| APPF | Long | 2026-07-30 | 2026-07-30 | 1 | $-295.54 | -3.000% | stop_loss |
| MTZ | Short | 2026-07-29 | 2026-07-30 | 2 | $-475.55 | -4.797% | kronos_exit |
| HELE | Short | 2026-08-21 | 2026-08-21 | 1 | $-299.33 | -3.000% | stop_loss |
| CWEN | Long | 2026-09-09 | 2026-09-10 | 2 | $-299.47 | -3.000% | stop_loss |
| NVMI | Long | 2026-09-09 | 2026-09-10 | 2 | $-323.01 | -3.295% | stop_loss |
| SUPN | Short | 2026-08-25 | 2026-09-15 | 15 | $423.32 | +4.236% | kronos_exit |
| ASND | Short | 2026-09-16 | 2026-09-16 | 1 | $-294.68 | -3.000% | stop_loss |
| LITE | Short | 2026-09-15 | 2026-09-16 | 2 | $-264.51 | -2.855% | trailing_stop |
| VNET | Long | 2026-09-22 | 2026-09-22 | 1 | $-299.99 | -3.000% | stop_loss |
| ESI | Long | 2026-09-23 | 2026-09-24 | 2 | $-143.66 | -1.437% | trailing_stop |
| MDB | Short | 2026-09-29 | 2026-09-29 | 1 | $-291.43 | -3.000% | stop_loss |
| EG | Short | 2026-09-30 | 2026-10-08 | 7 | $-146.94 | -1.475% | trailing_stop |
| NRG | Long | 2026-10-08 | 2026-10-08 | 1 | $-296.85 | -3.000% | stop_loss |
| MMS | Long | 2026-10-09 | 2026-10-12 | 2 | $-29.90 | -0.300% | kronos_exit |
| PWR | Short | 2026-10-16 | 2026-10-21 | 4 | $493.71 | +5.150% | kronos_exit |
| HP | Short | 2026-10-23 | 2026-10-23 | 1 | $-299.18 | -3.000% | stop_loss |
| SAIA | Long | 2026-10-23 | 2026-10-23 | 1 | $-295.30 | -3.000% | stop_loss |
| URI | Long | 2026-10-23 | 2026-10-23 | 1 | $-295.22 | -3.000% | stop_loss |
| AME | Short | 2026-11-06 | 2026-11-06 | 1 | $-110.58 | -1.109% | open |
