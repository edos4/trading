# Paper export review and short execution corrections

Reviewed November 6, 2026 (Asia/Manila). Input: `pt.json`, exported
November 5 at 20:51:24 UTC (November 6 at 04:51:24 Manila).
SHA-256: `aabb4eb564831e41d39e2d45554e5934e6fb2756c0ab9dd7cc536f5adec03e77`.
Reproduction:

```sh
.venv/bin/python -m analysis.paper_trade_review pt.json \
  --output /tmp/paper-review.json
```

The companion `paper-trade-review-2026-11-06.json` contains the verified
numbers, separate books, observed signal funnel and bounded counterfactual.
This is a different, much smaller export than the earlier November 5 review.
Its short trades were disabled; earlier exports' short results do not describe
this run.

## What the export establishes

The US replay covers 25 simulated sessions from October 1 through November 4,
with 2,000 symbols configured, Patternsv2 enabled, volume confirmation off,
DII enforced, and fixed $10,000 entries. There is **one closed trade and one
open trade**, both double-bottom longs. The PHP book is empty. Currency
results below are USD only.

| Measure | Result |
| --- | ---: |
| Realized P&L | -$255.58 |
| Open marked P&L, after entry fee | +$404.05 |
| Total marked P&L | +$148.47 (+0.1485% of initial capital) |
| Fees paid so far | $29.65 |
| Total P&L if the open trade closed at its saved last close, with exit fee | +$138.10 |
| Closed win rate / profit factor | 0/1 / 0.00 |
| Closed average net R | -1.0835R |

The saved OHLC, decision timestamps, entry fills, quantities, order ladders,
exit fills and fees reproduce under the audited causal exit engine. There
was no stop/target ambiguity and no opening-gap exit in the closed trade.
The descriptive 95% Wilson interval on a 0/1 win rate spans 0%–79.35%; one
loss cannot identify expectancy. The open winner is not a closed win.

### The signal funnel is the main finding

The export has 32 log rows but only **28 observed decisions**. Four queued
signals have a second row recording their execution outcome; counting these
rows as separate signals inflates activity.

| Side | Decisions | Rejected before queue | Queued | Filled | Rejected at open |
| --- | ---: | ---: | ---: | ---: | ---: |
| Long | 12 | 8 by DII | 4 | 2 | 2 by gap checks |
| Short | 16 | 16 by the disabled-short switch | 0 | 0 | 0 |

All short candidates were double tops. Four were below $5 and seven below
$10. These are candidate counts, not performance cohorts. None reached DII,
so it would be incorrect to promise 16 new shorts after enabling the switch.
There is no rejected-candidate OHLC history or subsequent return series in
this export to determine whether those shorts would have won.

DII rejected 8/12 long decisions (66.7%). Half of the four queued longs failed
the opening checks. Removing either filter to increase activity would be an
untested strategy change. The analysis now distinguishes disabled shorts,
DII and opening gaps instead of grouping short rejections under `other`.

### Trade-level diagnosis

**CSGP:** decision October 22 at $35.45; filled October 23 at
$35.220001, 283 shares. The saved stop was $34.3865, only 2.3666% from the
fill ($235.88 initial risk). The entry candle ranged from $33.79 to $35.59,
or **2.16R**, and touched that stop. The $43.38 target was **9.79R** away.
Gross loss was $235.88; $19.70 of round-trip fees made the net loss $255.58,
or -1.0835R. The engine correctly honored protection on the entry session.
Skipping entry-day stops would conceal this loss using information unavailable
when the position opened. No later CSGP bars are saved, so widening the stop
cannot be scored as a recovery or a win.

**GSHD:** decision November 3 at $52.23; filled November 4 at $54.10,
184 shares. The 3.58% higher open was a worse long entry. The effective stop
was $52.477, 3% from the fill ($298.63 risk), while the target was $61.015,
or **4.26R**. The entry session's $3.59 range was **2.21R**. Last close
$56.35 gives +4.159% gross price movement, +$404.05 after its entry fee,
and +$393.68 on a hypothetical close with the second fee. Its best close was
below the 5.25% trail activation, so an unarmed trail is expected.

GSHD also has a Kronos `oppose` exit vote despite entry confirmation being
off. Those modes are separate. The saved position is profitable and remains
inside the configured adverse-move deferral, so the vote did not queue an
exit. A vote alone must not be mistaken for an executed close.

A saved-bar experiment capping targets at 2R realizes GSHD on its entry bar,
leaving CSGP unchanged: total liquidation P&L becomes +$321.18 versus
+$138.10, a +$183.08 difference. This rests on **one favorable candle** and
changes the open/closed sample. It does not validate a 2R cap; measured
targets remain the default. Future-bar outcomes and rescanned signals were
not invented for this comparison.

## Corrections made

1. **Enable US short entries by default.** The existing switch can still
   disable new pattern shorts. PSE remains long-only. Disabling entries
   continues to leave every existing position's protection active.

2. **Honor Patternsv2's causal structural stop and ATR cap.** Its detector
   supplies a cap of `clamp(1.5 × ATR14 / signal price, 3%, 8%)` and its own
   trail. Previously the scanner replaced those with a 3% stop and a
   1R giveback / 1.75R activation. That overwrite made the existing volatility
   policy ineffective. It now applies to legacy/indicator entries only,
   and exports explicitly state this scope. This repairs policy propagation;
   this tiny export cannot validate the ATR multiplier or trail parameters.

3. **Resolve the saved signal's trail at the next open.** The fill step no
   longer replaces every strategy's trail with current legacy settings or
   invents a trail when the signal has none. Patternsv2's activation and
   giveback coefficients scale from its ATR cap to the actual initial stop
   distance, including a nearer structural stop. Stop and target prices stay
   fixed; invalid gaps are rejected. The trail remains a percentage of the
   best close, so its dollar giveback changes with that reference; the
   coefficient is not an exact fixed-dollar R distance.

4. **Reject inverted entry protection consistently.** Scanner decisions,
   paper queues, direct entries and next-open fills validate direction,
   positive finite price levels and a valid stop cap. Shorts must have their
   supplied stop above entry and target below it. A direct entry can no
   longer silently discard a crossed short stop/target and open the trade.
   Existing gap-risk, slippage and fee treatment remains in force.

5. **Preserve evidence before entry gates reject a signal.** Saved decision
   rules now include rejected shorts' stop, cap, target and trail. Previously
   all 16 rejected shorts had null requested/resolved rule snapshots.
   Rejection statistics now identify DII, disabled shorts, opening gaps and
   invalid entry geometry.

6. **Keep old open positions manageable.** Only the specifically audited
   previous engine hash is accepted for the unchanged exit ladder. Their
   saved protection is retained, including after restart; unknown runtimes
   remain rejected. Old pending orders still require the current entry
   runtime and are explicitly rejected after runtime changes.

The actual mirrored double-top detector is exercised on causal prefixes,
through conversion, scanner gates, queue persistence, next-open fill,
position persistence, gap stops, gap targets and armed trailing exits.
Matching long cases check directional symmetry. Both sides include adverse
slippage and fees, and cash/equity must reconcile to net trade P&L.
No detector quality threshold was fitted to this export.

## Limits and next evaluation

These corrections make the intended execution policy operate consistently;
they do not demonstrate a profitable short strategy. The account remains
an independent-trade research ledger, with no enforced portfolio capital or
gross-exposure limits. Increased short activity can therefore produce
unfinanceable aggregate exposure; its equity curve is not a broker margin
simulation.

Paper shorts also do not model stock availability, locate/borrow charges,
recalls, dividends owed or short-sale price restrictions. Actual US short
sales require a locate or applicable exception, and short losses can exceed
the original proceeds. See the [SEC's Regulation SHO overview](https://www.sec.gov/investor/pubs/regsho.htm).
Gap stops deliberately fill at the observed open rather than promising the
stop price; [FINRA explains why stop prices are not guaranteed fills](https://syndication.finra.org/content/understanding-order-types-can-save-time-and-money).

Before claiming improved expectancy, run an isolated, broader replay with
identical tapes, dates, universe, costs and settings, changing only the entry
policy. Report US and PHP separately; split long/short, pattern, quarter,
price, gap rejection, net R, fee drag, hold time and maximum gross exposure.
Use a held-out time window and explicitly report unresolved positions. Test
DII in shadow against enforced DII rather than selecting a new band from
these 12 long decisions. Include borrow/slippage stress for shorts. The
current export supports no claim that the disabled shorts were bad trades
or that removing DII would improve returns.

Honoring a wider ATR cap can also increase dollar risk at unchanged notional:
an 8% cap on $10,000 permits approximately $800 of initial stop risk before
fees and gaps, versus $300 at 3%. A nearer structural stop reduces that
amount. The existing optional risk-sizing mode can address varying dollar
risk, but it is not enabled or calibrated from this two-trade export.

## Validation

`.venv/bin/pytest -q -m 'not slow'`: **2,355 passed, 154 skipped,
10 deselected**. This run used the checkout including its pre-existing
detector changes. HTTP TestClient and threaded async tests stalled inside the
filesystem/network sandbox; the isolated checks and full suite completed
outside it. The saved-export analysis reproduced both trades and totals
after the entry changes, using the audited unchanged exit runtime.
