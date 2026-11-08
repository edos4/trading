# Paper trades and Patternsv2 risk review — 8 November 2026

Input: workspace `pt.json`, exported at 20:22 Asia/Manila on 8 November 2026.
SHA-256: `e94e9d3ca94f5b7c69a0160173b237964193077e441b6321b6b07ea8bd72f753`.
Machine-readable results: [paper-patternsv2-risk-review-2026-11-08.json](paper-patternsv2-risk-review-2026-11-08.json).

## Verified results and sample boundaries

This is the 1,931-symbol US Patternsv2 replay with DII **Shadow**, Kronos entry
**Confirm**, three forecast samples, Kronos exits **Exit**, and volume confirmation
off. It reached 217 simulated sessions and stopped on September 22, 2026;
it did not finish the requested tape. The scan reports `missing_execution_bars`
after three failed attempts to obtain a September 22 candle for held MMS.
There are 35 fills, 34 closes, and one open short. The PHP book has no trades;
its capital and results are not combined with USD.

The offline review reproduces every closed fill, fee, pre-bar order, exit
reason, holding-bar count, and the remaining position's marked P&L from saved
OHLC evidence. All saved ranges are internally consistent. There are no
stop/target ambiguity bars. This verifies the saved simulation's arithmetic
and order rules; it does not certify the source's corporate-action adjustments,
stock borrow, spreads, or live execution.

| Measure | US result |
| --- | ---: |
| Closed trades / wins / losses | 34 / 13 / 21 |
| Net realized P&L | +$111.96 |
| Open marked P&L / total account P&L | -$16.72 / +$95.24 |
| Profit factor / win rate | 1.0204 / 38.24% |
| Average winner / loser | +$430.45 / -$261.14 |
| Expectancy / average net R | +$3.29 / +0.0116R |
| Break-even win rate at observed payoffs | 37.76% |
| Median / maximum holding sessions | 2 / 10 |
| Maximum drawdown from saved daily marks | $2,701.77 |

The win rate's descriptive Wilson interval spans 23.90%–54.96%, and overlapping
trades are not independent trials. Removing the best trade changes net P&L to
-$1,005.03 and profit factor to 0.817; removing the best three changes it to
-$2,521.81 and 0.540. An additional 0.05% execution cost per leg, holding all
fills fixed, changes closed net P&L to -$226.41. This run has very little
margin above its assumed execution costs.

## Longs and shorts

| Side | Closes | Wins | Net P&L | Profit factor | Mean net R |
| --- | ---: | ---: | ---: | ---: | ---: |
| Long | 11 | 6 | +$1,532.19 | 2.152 | +0.512 |
| Short | 23 | 7 | -$1,420.24 | 0.658 | -0.228 |

Shorts have 11 stop-loss, ten trailing-stop, and two Kronos exits. Longs have
four stop-loss, two trailing-stop, and five Kronos exits. Seven of the eight
same-session stops are shorts; these eight trades together lost $2,388.99.
The median entry-session range is 1.30 times initial gross stop risk, so a
3% budget often sits within an ordinary daily range. Widening the stop would
contradict the requested budget. These observations support investigating
entry quality on new data; they do not establish a profitable short filter.
Shorts were profitable in 2026Q1 (+$749.00), then lost $1,083.00 in Q2 and
$874.79 in Q3. Keeping both directions is appropriate for this repair.

The accounting has the correct direction: longs sell to close, shorts buy to
cover, both pay adverse exit slippage and fees. Short proceeds are liabilities
until covered, not realized profits. This review found no sign inversion in
the saved short fills or funded ledger reconciliation.

| Pattern | Closes | Wins | Net P&L | Profit factor |
| --- | ---: | ---: | ---: | ---: |
| ascending_channel | 3 | 0 | $-898.73 | 0.000 |
| ascending_triangle | 3 | 1 | $-394.64 | 0.316 |
| descending_channel | 1 | 0 | $-297.31 | 0.000 |
| descending_triangle | 4 | 2 | $+1,333.92 | 5.227 |
| falling_wedge | 7 | 3 | $-159.28 | 0.846 |
| symmetrical_triangle | 16 | 7 | $+527.99 | 1.224 |

Per-family samples range from one to sixteen trades. Ascending channels lost
all three short trades, but muting that family based on three outcomes would
fit this replay. Descending triangles include both breakout directions; the
family's name is not an instruction to short. Existing pattern mute settings
and short-entry permissions are preserved.

## The 3% protection and open MMS position

The export already uses `net-loss-trail-v1`: an independent resting order limits
ordinary net loss to 3% of **entry notional**, including both fees and adverse
exit slippage. It tightens from the best surviving completed close with a
three-percentage-point allowance. It never loosens or waits for the pattern's
ATR/R activation. The wider structural/ATR stop remains separate and may close
a trade earlier. This is a price-position budget, not a 3% account-equity bet.
Net R uses gross entry-to-trigger risk as its denominator. An exact -3% net
exit can therefore be about -1.09R once fees and slippage are included; this
alone is not evidence that the budget failed.

All 15 stop-loss exits except UNM meet the 3% budget. UNM's resting short stop
was $75.388005; the next session opened at $75.580002 and the cover including
slippage was $75.617792. Net loss was $325.51, or 3.2621%. Filling at the stop
would conceal a real opening gap. WDAY also gapped through a profitable trail:
its best surviving net close was +9.05195%, the intended floor was +6.05195%,
and the opening-gap fill realized +5.33666%. The [SEC's stop-order bulletin](https://www.investor.gov/introduction-investing/general-resources/news-alerts/alerts-bulletins/investor-bulletins-15)
explains why trigger prices cannot guarantee execution prices.

Seven of twelve trailing exits lost money. That does not mean the long/short
trail is reversed: FLO peaked at +1.625% net and exited at -1.375%; PPTA peaked
at +0.567% and exited at -2.433%. A three-point giveback can end below breakeven
when the peak is below 3%. A forced breakeven rule would be another strategy
change, not an accounting correction. SHOP peaked at +12.949% and retained
+9.949%, demonstrating the same rule on a profitable short.

MMS sold 178 shares at $55.932019 on September 21. Its last valid close is
$55.970001, the independent buy stop is $57.467817, and its measured target is
$44.9668. The saved mark is -$16.72; a hypothetical immediate cover at that mark,
including remaining costs, would be -$31.67. No September 22 execution bar is
available in the export. The repair leaves the stop executable when valid
evidence resumes. It does not book a fabricated close or continue a supposedly
complete replay through an unknown execution interval. Neither runtime ledger
files nor the exported positions were altered.

## Detection and execution repairs

1. **Measured objectives stay at the breakout boundary.** Previously a long
   breakout with boundary $100, pattern height $20, and confirmation at $105
   received a $125 target. The target is now $120. The mirrored short objective
   is $80, not $75. Confirmation overshoot consumes remaining reward. A signal
   on the wrong side of its boundary, or one whose measured objective is already
   exhausted, is rejected. Missing/ambiguous directions cannot default to short.
2. **Choose candidates after validating execution geometry.** The production
   detector can retain same-family candidates until the adapter checks their
   targets. An unusable high-score candidate can no longer suppress a valid
   lower-score candidate. The standalone chart detector keeps its deduplicated
   API, and external detector checkouts without this capability retain their
   existing call signature. Full-history and latest-bar equivalence tests remain
   active. No detection thresholds were selected using trade P&L.
3. **Saved-engine drift cannot disable independent protection.** When a saved
   pattern runtime is incompatible, paper management continues with the cost-aware
   safety stop and any already queued next-open market exit. It does not reinterpret
   incompatible pattern targets or trails. Surviving closes still tighten safety;
   exit evidence records `management_mode=paper_protection_only` and the runtime
   mismatch. Saved provenance and rule snapshots are retained. The previous audited
   engine is explicitly accepted for its unchanged saved ladders.
4. **Old bars cannot reuse a new stop.** A delayed or duplicate completed candle
   is ignored for the held position before changing its mark or bar clock. This
   prevents a newly ratcheted long or short stop from being applied retroactively
   to an older range, including after restart.
5. **Malformed forming ranges do not block observed-price protection.** A valid
   live last-price observation can still execute a resting stop even when the
   surrounding high/low range is malformed. No historical range, forming high,
   or forming low is used to invent a tick fill. Invalid historical bars remain
   unresolved data gaps.

The report generator now records replay termination/coverage, net position
returns, 3% budget breaches, and a bounded comparison of boundary-anchored
objectives. Existing closed trades and open positions retain their saved levels.

## Entry funnel and bounded comparisons

There are 406,106 detector evaluations, 376 entry decisions, 341 rejections, and
35 fills. Of 239 long decisions, eleven filled; of 137 short decisions, 24 filled
including MMS. Kronos opposed 173 decisions and had no opinion on 150. Another
12 queued decisions failed its net opening-price recheck; four hit the symbol
cap and two hit the pattern cap. The 53 queue events and 35 fill events describe
stages of the same decisions, not additional detections. All 743 journal rows
fall within this run's saved scan window and every ledger fill matches its ID.

The forecast provenance explicitly has `identity_verified=false` and
`out_of_sample_eligible=false`. Three samples are not three independent models.
The export cannot establish the performance of rejected entries, because their
complete later OHLC and subsequent model exits are not saved. The gate settings
were therefore preserved.

A fixed-entry comparison of the corrected boundary targets leaves all 34 saved
exits and net P&L unchanged: protection or Kronos closed these positions first.
The median measured target falls from 34.80% to 32.73% away from entry, still
far beyond most observed holding paths. HUBS's old confirmation-based target
was 88.62% above entry; boundary anchoring reduces it to 70.81%. This is a
geometric correction, not evidence of improved historical profit.

The existing 2R target-cap diagnostic yields +$401.84 and profit factor 1.073
on the same saved entries, compared with +$111.96 and 1.020 for baseline.
That is a comparison on this inspected sample with saved external exits held
fixed, not an untouched holdout or a replay of altered capital and signal flow.
No 2R target cap was enabled on the strength of this result. Both directions,
3% paper protection, and the existing model policies remain available.

## Individual closes

Returns below include transaction costs. Sessions count the entry session.

| Symbol | Side | Pattern | Entry session | Exit session | Sessions | Net P&L | Net return | Exit |
| --- | --- | --- | --- | --- | ---: | ---: | ---: | --- |
| USB | Short | ascending_channel | 2025-11-18 | 2025-11-19 | 2 | $-299.99 | -3.000% | stop_loss |
| MSM | Short | symmetrical_triangle | 2025-11-21 | 2025-11-21 | 1 | $-298.56 | -3.000% | stop_loss |
| UNM | Short | symmetrical_triangle | 2025-12-04 | 2025-12-05 | 2 | $-325.51 | -3.262% | stop_loss |
| KRC | Short | symmetrical_triangle | 2025-12-05 | 2025-12-18 | 10 | $+343.03 | +3.440% | kronos_exit |
| FLO | Long | falling_wedge | 2025-12-12 | 2025-12-23 | 8 | $-137.41 | -1.375% | trailing_stop |
| LEN | Short | symmetrical_triangle | 2025-12-18 | 2025-12-23 | 4 | $+369.58 | +3.730% | kronos_exit |
| ONDS | Long | descending_triangle | 2026-01-02 | 2026-01-05 | 2 | $+1,116.99 | +11.172% | kronos_exit |
| SHOP | Short | symmetrical_triangle | 2026-01-15 | 2026-01-22 | 5 | $+984.29 | +9.949% | trailing_stop |
| NTES | Short | symmetrical_triangle | 2026-01-16 | 2026-01-28 | 8 | $+136.61 | +1.376% | trailing_stop |
| BXSL | Long | falling_wedge | 2026-03-05 | 2026-03-06 | 2 | $-299.77 | -3.000% | stop_loss |
| GSAT | Short | symmetrical_triangle | 2026-03-09 | 2026-03-10 | 2 | $-299.12 | -3.000% | stop_loss |
| DDS | Short | descending_triangle | 2026-03-24 | 2026-03-31 | 6 | $-72.78 | -0.756% | trailing_stop |
| AMAT | Long | symmetrical_triangle | 2026-04-09 | 2026-04-14 | 4 | $+339.30 | +3.521% | kronos_exit |
| AFG | Long | descending_channel | 2026-04-10 | 2026-04-15 | 4 | $-297.31 | -3.000% | stop_loss |
| MA | Long | falling_wedge | 2026-04-09 | 2026-04-16 | 6 | $+228.69 | +2.385% | kronos_exit |
| UGI | Short | symmetrical_triangle | 2026-05-04 | 2026-05-13 | 8 | $+494.90 | +4.950% | trailing_stop |
| LNG | Short | symmetrical_triangle | 2026-05-08 | 2026-05-15 | 6 | $-139.88 | -1.399% | trailing_stop |
| OGE | Short | ascending_channel | 2026-05-18 | 2026-05-19 | 2 | $-299.41 | -3.000% | stop_loss |
| HUBS | Long | falling_wedge | 2026-06-02 | 2026-06-02 | 1 | $-295.96 | -3.000% | stop_loss |
| IMNM | Short | symmetrical_triangle | 2026-06-03 | 2026-06-03 | 1 | $-299.77 | -3.000% | stop_loss |
| SPSC | Long | falling_wedge | 2026-06-02 | 2026-06-03 | 2 | $-299.98 | -3.000% | stop_loss |
| KKR | Short | symmetrical_triangle | 2026-06-04 | 2026-06-04 | 1 | $-299.19 | -3.000% | stop_loss |
| PPTA | Short | descending_triangle | 2026-06-08 | 2026-06-09 | 2 | $-242.78 | -2.433% | trailing_stop |
| SCCO | Short | symmetrical_triangle | 2026-06-11 | 2026-06-11 | 1 | $-296.87 | -3.000% | stop_loss |
| FORM | Short | ascending_triangle | 2026-07-07 | 2026-07-08 | 2 | $+182.59 | +1.836% | trailing_stop |
| WDAY | Long | descending_triangle | 2026-07-14 | 2026-07-20 | 5 | $+532.49 | +5.337% | trailing_stop |
| TOL | Short | symmetrical_triangle | 2026-08-18 | 2026-08-19 | 2 | $-103.76 | -1.045% | trailing_stop |
| HELE | Short | ascending_channel | 2026-08-21 | 2026-08-21 | 1 | $-299.33 | -3.000% | stop_loss |
| FMC | Long | falling_wedge | 2026-08-20 | 2026-08-25 | 4 | $+303.62 | +3.039% | kronos_exit |
| TLN | Short | symmetrical_triangle | 2026-08-25 | 2026-09-02 | 7 | $+222.26 | +2.235% | trailing_stop |
| EBAY | Short | symmetrical_triangle | 2026-09-09 | 2026-09-09 | 1 | $-299.32 | -3.000% | stop_loss |
| LITE | Short | ascending_triangle | 2026-09-15 | 2026-09-16 | 2 | $-277.24 | -2.997% | trailing_stop |
| GTM | Short | ascending_triangle | 2026-09-21 | 2026-09-21 | 1 | $-299.99 | -3.000% | stop_loss |
| KLIC | Long | falling_wedge | 2026-09-14 | 2026-09-22 | 7 | $+341.53 | +3.423% | kronos_exit |

## Reproduction and validation

Run the read-only saved-evidence review:

```bash
.venv/bin/python -m analysis.paper_trade_review pt.json --output /tmp/pt-risk-review.json
```

The checked-in JSON retains every trade and comparison; its quote-clock map is
compacted to a symbol count while retaining unresolved intervals. No new market
data, model forecasts, parameter sweeps, account writes, or detector rescans
are required for this review.

Validation of the isolated staged snapshot: **373 passed, 31 skipped,
5 deselected** across the focused pattern,
protection, entry, scanner, accounting, saved-export, and replay checks. The
skipped integration cases require optional test infrastructure; five slow
fixture cases were deselected. Both long and short regressions cover normal
3% net stops, opening gaps, restarts, adopted positions, incompatible engines,
queued exits, stale/duplicate candles, malformed live ranges, and all-candidate
latest-bar causality. `git diff --check` is clean. The same focused checks in
the operator workspace passed 389 tests; its additional 16 cases belong to the
pre-existing bounded-family changes. The isolated snapshot contains HEAD plus
only the staged files, uses nonsecret fixture environment values, and does not
copy the operator's `.env`.

The complete `pytest -m "not slow"` run was interrupted at a FastAPI TestClient
hang. A bare FastAPI app with no trading code reproduces that hang. A broader
Kronos callback test also stalls during asyncio runner shutdown in this
environment. Full-suite and asynchronous integration success are not claimed;
no dependency or unrelated async implementation was changed to mask them.

Pre-existing changes to the bounded-family rail fitting, run-setting logging,
other documents, and the raw export are outside this commit. They are retained
in the working tree. No `.env`, runtime `data/`, immutable pattern registry,
deployment units, or scheduled shell jobs are edited by this repair.
