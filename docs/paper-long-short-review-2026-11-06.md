# Long/short paper execution review — November 6, 2026

Source: `pt.json`, exported November 6 at **07:05:17 Manila**
(`2026-11-05T23:05:17.147607+00:00`). SHA-256:
`b069e40298addb738c6012eba74fb83caafc0b089edc22a0d24055bcdf78c95c`.
This is the nine-trade export with shorts enabled, distinct from the earlier
two-trade and 65-trade exports. Its simulated cutoff is November 4.
The companion JSON records all calculations and trade-level evidence.

## Findings

The US book holds independent $10,000 research trades, with Patternsv2, DII,
0.1% fees per side and zero simulated slippage. The PHP book has no trades;
no currencies are combined. Six closed trades are too few to establish an
edge or tune a new stop, target, time limit or DII band.

| US measure | Result |
| --- | ---: |
| Closed / open | 6 / 3 |
| Closed P&L / profit factor | −$1,589.81 / 0.532 |
| Closed wins / average net R | 1 of 6 / −0.483R |
| Closed longs | 0 of 3 wins; −$1,881.58 |
| Closed shorts | 1 of 3 wins; +$291.77 |
| Open marked P&L, after entry fees | +$992.41 |
| Total marked P&L | −$597.40 |
| Total with hypothetical liquidation at each open trade's last close | −$627.14 |
| Fees already paid, closed and open | $147.65 |
| Daily marked drawdown / peak overlapping positions | $4,323.77 / 5 |
| Current gross exposure | 29.92% of equity |

The single winner, GLXY, contributes $1,804.01; the other five closed trades
lose $3,393.82. The descriptive 95% win-rate interval is approximately
3.0%–56.4%, before accounting for dependence between trades. Short success
is concentrated in one trade, but removing shorts would worsen this sample:
long-only closed plus hypothetical open liquidation is **−$1,487.90**.

There are 37 signal-log rows representing **28 decisions**. Nine queued
decisions have additional fill rows. Longs: 12 decisions, 8 DII rejections,
4 fills. Shorts: 16 decisions, 10 DII rejections, 5 fills and one pending PXS
order. Entry activity is constrained mostly by DII; rejected candidates have
no subsequent saved paths from which to judge an alternative filter.

## Closed-trade diagnosis

| Symbol | Side | Bars | Initial risk | Net P&L | Net R | Exit |
| --- | --- | ---: | ---: | ---: | ---: | --- |
| STRR | Long | 2 | $745.05 | −$764.30 | −1.026 | Hard stop |
| MRT | Short | 11 | $799.91 | −$820.71 | −1.026 | Hard stop |
| CSGP | Long | 2 | $502.25 | −$521.68 | −1.039 | Hard stop |
| GLXY | Short | 13 | $799.68 | +$1,804.01 | +2.256 | Trail |
| STVN | Short | 17 | $670.87 | −$691.53 | −1.031 | Hard stop |
| CTRI | Long | 1 | $576.18 | −$595.60 | −1.034 | Hard stop |

Five hard stops and one trailing exit reproduce from the saved rules and
raw bars; there are no opening-gap exits or bars touching both resting stop
and target. Fees turn a nominal −1R stop into approximately −1.03R. Longs
last only 1.67 bars on average; shorts last 13.67 bars. The closed longs never
establish a profitable surviving close. The file cannot show whether they
recovered after their exits, so it cannot justify widening their stops or
suppressing entry-day protection. CTRI's entry-session range is 1.84R.

MRT reaches only +0.67R on a surviving close before stopping out. STVN
reaches +0.48R. Neither reaches the approximately +2R trailing activation;
their inactive trails are consistent with the saved rules. GLXY reaches
+3.69R on a surviving close and eventually trails out at +2.26R net.

Closed gross loss is $1,472.09, plus $117.72 in fees. Adding another 0.1%
friction per side to the same closed fills lowers net P&L to −$1,707.53.
This is an accounting stress, not a full slippage-aware replay.

## Open positions and their closing decisions

| Symbol | Side | Bars | Marked P&L | Hypothetical net liquidation | Best surviving close |
| --- | --- | ---: | ---: | ---: | ---: |
| SPCE | Short | 17 | +$589.20 | +$579.80 | +1.54R |
| GSHD | Long | 1 | +$404.05 | +$393.68 | +0.52R |
| ZBRA | Short | 1 | −$0.83 | −$10.81 | +0.012R |

**SPCE:** entry $2.67, effective stop $2.8836, target $2.05. Its best close
is +12.36%, below the 16% activation, and it gives back part of that move
to +5.99%. Its latest Kronos vote is `no_opinion`. These rules have not
ordered a close; reducing the activation just to capture this one reversal
would be an in-sample parameter choice.

**GSHD:** entry $54.10, effective stop $49.772, target $61.015. Its target is
only +1.60R, inside its +2R trail activation. A target exit before trail
activation is expected. Its saved `oppose` forecast was deferred because
the old Kronos guard required a loss of at least 0.5 ATR. Every profitable
position failed that test regardless of the size of its gain. At the saved
anchor, GSHD is +4.16%, outside a 0.5 ATR noise band of approximately 2.63%.
The corrected decision would queue an exit for the following open. That
open is absent from the export, so no realized price or profit is claimed.
The change takes effect on future processed votes; it does not retroactively
execute an exported vote or rewrite the account.

**ZBRA:** entry $332.745, effective stop $357.4247, target $273.1198. The
small gross gain is smaller than its entry fee. Its latest forecast confirms
the short, so there is no model exit decision. Both shorts close by buying
back shares; adverse exit slippage increases their cover price. Long exits
sell shares, with adverse slippage decreasing the price.

## Shipped changes

1. **Fixed-dollar R giveback for new R-based trails.** Previously a trail
   described as 2R used `best_price × trailing_pct`. With a $100 entry,
   $8 initial risk and 16% giveback, first activation at $116 puts a long's
   trail at $97.44; activation at $84 puts a short's trail at $97.44. Those
   are −0.32R and +0.32R respectively. A fixed $16 giveback puts both at
   $100, or gross breakeven. Fees and gap fills can still produce a loss.
   `trailing_stop_basis="entry"` makes this explicit in saved rules.
   Patternsv2 signals and the scanner's legacy R override select it. The
   existing next-open resolver scales the coefficient to the executed stop
   distance, including a nearer structural stop and opening gaps.

2. **Allow profitable positions to honor opposing Kronos exits.** The same
   configured noise threshold now measures absolute displacement from entry.
   Small gains and small losses remain inside the band; either side can
   exit outside it. Votes still execute at the next observed open, with the
   normal fees and adverse slippage. Evidence now distinguishes a deferred
   vote from a queued exit and records the displacement and noise threshold.

3. **Keep existing open positions manageable.** Missing trail basis retains
   the saved reference-percentage behavior. The specifically audited source
   engine hash is accepted after reload, while unknown hashes still fail.
   Saved stops, targets and trails are retained. The Kronos decision fix also
   applies to existing positions on future votes. Old pending entries still
   require the current entry runtime; a changed-runtime PXS order must be
   rejected explicitly, rather than silently filled under new rules.

4. **Make incomplete and inconsistent evidence visible.** The review tool
   reports raw OHLC anomalies, surviving-close MFE, open liquidation, target
   versus trail activation and censored counterfactuals. It retains strict
   OHLC validation by default.

An exit-only 2R target cap reduces total liquidation P&L from −$627.14 to
**−$704.59**, preempting GLXY's trail. It remains disabled by default. The
fixed-dollar short trail is wider than the old reference trail after a
decline; GLXY remains open at its last saved bar in that diagnostic. There
are no later bars in the export, so the tool withholds a comparable total
for that scenario. This is a correction to the stated R geometry, not a
claim of improved historical returns. The historical sweep quoted in the
converter predates the correction and does not validate it.

## Evidence quality and validation

Three recorded opens are below their reported lows: MRT on October 15
($1.7700 versus $1.7711), MRT on October 19 ($1.7600 versus $1.7802), and
STVN on October 29 ($19.6000 versus $19.6100). They are non-exit bars. Raw
replay reproduces the saved order decisions, fills, quantities, costs and
totals, but this does **not** certify the market data as valid. The report
sets `baseline_reproduced=true` and `baseline_verified=false` for the US
book. It neither normalizes ranges nor infers a hidden intraday path.
Resolve the inconsistent tape before treating a fresh replay as validation.

Reproduce the explicitly qualified audit:

```sh
.venv/bin/python -m analysis.paper_trade_review pt.json \
  --allow-inconsistent-ohlc \
  --output /tmp/paper-long-short-review.json
```

Validation: **110 focused tests passed**; the full non-slow suite passed
with **2,371 passed, 154 skipped, 10 deselected**. Tests cover directional
R symmetry across opening gaps, causal trail activation, legacy ladders,
reloads, profitable long/short Kronos exits, next-open fills, slippage,
fees, cash/equity reconciliation, audit anomalies and missing future bars.
Async scanner/HTTP tests required local socket access outside the sandbox.
The full run includes the checkout's pre-existing detector edits; those
edits and the modified input export are excluded from this change's commit.
The exact staged source was also tested in a temporary checkout without
those detector edits: **110 focused tests passed** with synthetic settings.

Before claiming better expectancy, compare the corrected policy on an
independent period with identical entries, costs and complete subsequent
bars, then perform a full rescan because changed exits affect later entry
eligibility. Retain both sides and report unresolved trades. The research
ledger does not model borrow availability, borrow costs or broker margin.
The [SEC's short-sale overview](https://www.sec.gov/investor/pubs/regsho.htm)
describes buying shares back and the possibility of unlimited losses. The
[SEC's stop-order bulletin](https://www.investor.gov/introduction-investing/general-resources/news-alerts/alerts-bulletins/investor-bulletins-15)
explains why a stop price does not guarantee its execution price; opening
gaps continue to fill at the observed open in both directions.
