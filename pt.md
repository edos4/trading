# Patternsv2 paper trade entry and exit review — 10 November 2026

**Remediation record.** The analysis below describes the original export and source revision. Implementation changes are recorded here separately; historical results are not rewritten as results of the repaired strategy.

| Issue | Implementation status |
| --- | --- |
| Opening price returns inside the V2 breakout | Pending: preserve boundary policy and reject invalidated openings. |
| Inconsistent target R and risk labels | Pending: explicit risk basis shared by execution and diagnostics. |
| Boundary diagnostic shifts already correct targets | Pending: require objective-policy evidence. |
| Volatility/holding-objective mismatch | Pending: causal budget-fit evidence and configurable research controls; no outcome-fitted default. |
| Kronos selection is not a holdout / incomplete policy comparisons | Pending: enforce evaluation eligibility and compare complete frozen runs. |
| Falling-wedge/DII cohort losses | Pending: reproducible cohort attribution and temporally separate evaluation, rather than historical winner selection. |
| Export loses manifest and exit-decision history | Pending: preserve source/control identity and full position decisions. |
| Misleading inactive-gate comments and confidence semantics | Pending: align documentation and exported labels with active V2 behavior. |

**Detector scope: Patternsv2 (`patterns_v2/`).** All 35 closed trades explicitly save `provenance="patternsv2"`; all 35 matching filled journal rows and all 961 journal rows have the same provenance. Each trade also has a saved chart entry annotation tagged `patternsv2`, and the run settings have `patterns_v2=true` and `indicators_only=false`. There are zero legacy `patterns/` trades in this analysis. References below to a “pattern,” “pattern risk,” or “pattern trail” mean the Patternsv2 formation or its saved execution rules. The shared scanner, paper account, and exit engine execute those V2 rules. `pattern_only=false` is a separate gate-mode setting and does not select the legacy detector family. The null `pattern_version_id` values are expected for this V2 path, which does not use pinned legacy pattern versions.

**The losses come from an unsuccessful combination of entries, forecast selection, and exit distances. The saved accounting and long/short exit arithmetic reproduce correctly.** We repeatedly enter breakouts that fail quickly, protect them with a stop smaller than their normal daily range, and retain targets much farther away than the positions usually survive. Kronos rejects most opportunities but has not demonstrated a profitable selected subset in this export.

The US book lost **$5,201.68, or 5.20% of its $100,000 starting capital**, across 35 closed trades. Twelve entry-session stop-outs account for **$3,564.46, or 68.5% of the net loss**. Every executed exit came from independent paper protection or Kronos; the pattern trailing stop never activated and no original take-profit executed.

There are also two consequential problems in the existing analysis tooling: its “2R” comparison uses a different risk denominator from the production target setting, and its “boundary objective correction” can adjust targets that are already correct. These can misdirect attempts to improve the strategy.

**1. Evidence, scope, and verification**

Input: [pt.json](/home/engine-33ai/codes/trading/pt.json), exported **10 November 2026 at 16:16:01 Asia/Manila** (`2026-11-10T08:16:01.840380+00:00`). Export SHA-256:

```text
bdf1196190ea29bd1e03a6cc1a679a52c4ee9637777a4bfead7f0861b90867b7
```

Execution engine SHA-256, matching every saved trade:

```text
f1d110291fcca45cd4214612081660feba4b18ff195f47343ff4ea22c0d8e3d3
```

Source inspected at repository HEAD `b727f390fc4600ebf5a3efeb52b4266dcbd769f2`. The US replay covers 251 sessions, starting 10 November 2025 and ending 9 November 2026, with `termination_reason=end_of_tape`. Actual entries span 20 November 2025 through 6 November 2026. The PH book is idle, with no trades; its PHP capital is excluded from all USD calculations.

The replay ended flat, with one unfilled ONTO falling-wedge order decided on 9 November. A pending order is not a position or a realized loss. The run used 1,950 configured US symbols, Patternsv2, DII Shadow, volume confirmation off, Kronos entry Confirm with three samples, Kronos position Exit, and a funded account.

Using [paper_trade_review.py](/home/engine-33ai/codes/trading/analysis/paper_trade_review.py:82), all 35 baseline trades reproduce their saved prior orders, fills, exit reasons, dates, holding-bar counts, protection state, fees, and P&L. Cash reconciles to **$94,798.32**. There are no inconsistent saved OHLC ranges, original stop/target ambiguity bars, or recorded unresolved execution intervals. All 961 journal rows belong to this run, and every trade matches a filled journal identity.

This verifies the exported execution evidence. It does not independently verify source prices, corporate actions, historical universe membership, actual stock borrow, or real fills. This export contains historical daily-bar management, not a live tick sequence. No strategy settings, trading code, runtime ledgers, or input data were changed for this review.

**2. What the loss actually consists of**

| Measure | Result |
| --- | ---: |
| Closed trades / winners / losers | 35 / 7 / 28 |
| Net P&L / return on initial capital | -$5,201.68 / -5.20% |
| Win rate / profit factor | 20.00% / 0.297 |
| Average winner / average loser | +$314.64 / -$264.43 |
| Average net P&L per trade | -$148.62 |
| Average net R, using initial gross protection distance | -0.545R |
| Break-even win rate at these average payoffs | 45.67% |
| Sum of winning P&L / sum of losing P&L | +$2,202.45 / -$7,404.13 |
| P&L before fees, with executed slippage retained | -$4,510.29 |
| Fees | $691.39 |
| Median / mean / maximum holding sessions | 2 / 2.97 / 15 |
| Maximum drawdown from saved daily marks | $5,447.46, or 5.45% of initial capital |
| Peak overlapping positions, including entry sessions | 3 |

At a 20% win rate, the average winner would need to be about **$1,057.73** to offset the observed average loser. It is $314.64. A high advertised target-to-stop ratio does not solve this when almost none of that target is realized.

Fees explain 13.3% of the net loss. Holding the observed raw entry/exit events and quantities fixed, adverse slippage costs approximately another **$345.69**. Even before both those deductions, the same raw execution events lose **$4,164.60**. This is an accounting decomposition, not a zero-cost strategy replay: changing costs also changes the cost-aware stops and opening forecast check.

An additional 0.05% friction per side, with fills and quantities fixed, lowers P&L to **-$5,547.37**. Costs matter, but they are not the primary explanation.

**3. How a trade opens**

The relevant implementation is [Patternsv2 signal construction](/home/engine-33ai/codes/trading/patterns_v2/signals.py:281), [scanner processing](/home/engine-33ai/codes/trading/core/scanner.py:1829), [opening execution](/home/engine-33ai/codes/trading/core/execution.py:89), and [paper admission](/home/engine-33ai/codes/trading/core/paper_trader.py:899).

1. A Patternsv2 detector identifies a breakout on a completed daily candle. The `patterns_v2/signals.py` adapter creates BUY/SELL, a structural invalidation stop, a measured target, and an ATR-based V2 pattern ladder. The ATR stop cap is `clamp(1.5 × ATR14 / signal_close, 3%, 8%)`; pattern trail activation and giveback are each twice its risk basis. The geometry score becomes a ranking value called confidence.
2. The scanner checks direction permissions and basic stop/target geometry. DII is recorded in Shadow mode. Volume is off, the visual rule filter is off, and Laya is off in this run. The live scanner's confidence, regime, cooldown, and generic risk helpers currently return permissive/no-op results. Some nearby comments still describe active controls; the implementations at [scanner.py:56](/home/engine-33ai/codes/trading/core/scanner.py:56) are authoritative.
3. Kronos predicts one-day closes. At least two samples must agree with the trade direction outside an ATR-scaled flat band. A confirmation queues the setup; it does not fill at the decision candle's close.
4. At the next observed daily open, the engine applies adverse entry slippage, retains the structural levels, and rejects a crossed stop/target, expired order, or excessively compressed pattern stop distance. It rechecks the saved forecasts against the executed price, both fees, and exit slippage. It does not forecast using that execution day's later close.
5. Sizing uses approximately $10,000 notional, rounded to permitted quantity. `risk_pct=0`, so this is fixed-notional sizing. Funding, symbol/pattern caps, liquidity participation, and positive net target reward are checked. Independent 3% net paper protection is then installed.
6. That entry day's full OHLC bar can execute the orders because the retrospective fill is at its open. Surviving positions are subsequently managed and assessed for a Kronos exit.

The observed funnel reconciles exactly:

| Stage or terminal outcome | Decisions |
| --- | ---: |
| Distinct detected entry decisions | 519 |
| Rejected before queue: Kronos opposed | 262 |
| Rejected before queue: Kronos had no opinion | 200 |
| Queued | 57 |
| Queued, then rejected by net opening-price Kronos check | 20 |
| Queued, then rejected because opening crossed structural stop | 1 |
| Filled | 35 |
| Still pending at end of tape | 1 |

Thus `519 = 462 + 35 + 21 + 1`. Total rejections are 483, or 93.06%. The cumulative Kronos rejection count of 482 includes the 20 opening rechecks. Queue events, fills, and the 381 DII shadow rows are stages or annotations of existing decisions, not additional opportunities. No funding, liquidity, symbol, or pattern cap rejection appears in this run.

**4. An opening gap can invalidate the breakout thesis without failing entry geometry**

`validate_entry_geometry()` checks the structural stop and target. It does **not** require the opening price to remain beyond the broken boundary. The minimum-risk check measures distance to the pattern stop, not whether price has already returned inside the formation.

All 35 signal closes were beyond their saved breakout boundary, but six actual entries were back inside it:

| Symbol | Side | Executed entry | Saved breakout boundary | Back inside boundary | Net P&L |
| --- | --- | ---: | ---: | ---: | ---: |
| APLD | Short | $29.5852 | $29.1671 | 1.43% | -$299.99 |
| JKHY | Long | $153.0265 | $153.4549 | 0.28% | -$298.40 |
| IOT | Long | $30.1551 | $30.6312 | 1.55% | -$299.44 |
| PRGO | Long | $11.0055 | $11.5350 | 4.59% | -$166.56 |
| APPF | Long | $182.4312 | $183.3978 | 0.53% | -$295.54 |
| CWEN | Long | $32.5163 | $32.6824 | 0.51% | -$299.47 |

These six lose **$1,659.41**; three stop on entry day. This is an observable thesis-preservation gap in admission. APLD shorts above broken support; PRGO buys materially below broken resistance.

The comparison uses the exact slipped fill and the saved boundary at the signal candle. A future rule must explicitly choose a frozen boundary or a rail projected to the next session. A raw-open comparison should also be distinguished from the slipped-fill comparison. Those choices need to be specified before validation.

The other 29 trades still lose **$3,542.27**. Skipping these six historical fills would not by itself establish a profitable strategy, and the effect on later funding and signals has not been replayed. Median signed movement from signal close to slipped entry is **-0.60%** in the intended trade direction, showing that many fills occur after price has already moved back against the breakout. This is not a slippage measurement; it includes the overnight move.

**5. The practical stop is much tighter than the pattern's risk model**

Independent paper protection is closer than the structural/ATR stop on **all 35 entries**. The median pattern stop distance is **5.765%**. The actual initial paper trigger is only **2.746%–2.754%** from executed entry, leaving room for both fees and adverse exit slippage inside a 3% net loss budget.

The median saved forecast ATR is **4.00%** of the signal close. The median ratio of actual initial gross stop distance to that ATR is **0.687**; 31 of 35 stops are smaller than one ATR. This is an approximate volatility comparison because entry and signal prices differ and the forecast ATR calculation is distinct from the adapter's ATR cap calculation.

The median entry-session high-low range is **4.39% of entry**, or **1.595 times the actual initial gross stop distance**. Twenty-three entry-session ranges exceed that distance. These are descriptions of observed bars, not filters available at the opening auction. The later day's range must not be used to decide that morning's entry.

Twelve trades stop in their entry session: six longs and six shorts, spanning four pattern families. All lose, together costing $3,564.46. Even the 23 positions that survive entry day lose another **$1,637.22** with a 30.43% win rate. Avoiding the first-day failures would leave a weaker but still losing remainder.

APLD is the clearest mismatch: forecast ATR is 14.12%, but initial gross protection is about 2.75%, only **0.194 ATR**. Its entry-session range is **19.76%**, or about **7.20 initial R**. The short stops at -3% net while that session ultimately closes much higher.

Three entry-session stop-outs—**IOT, BL, and URI**—later close at prices that would give a positive net open-to-close return. That does not make their recorded intraday stops incorrect. It shows why forecasting a closing price and trading a path-dependent intraday stop are different objectives. Moving to close-only stops would create a different risk policy and could expose the other nine entry-session failures to larger losses.

The engineering question is whether these entries can fit a 3% net budget at all. Candidate approaches should use information available before entry: prior volatility, preserved breakout geometry, or a defined later confirmation/retest. Reducing position size can reduce account-dollar risk, but it does not make a tight stop wider in volatility units.

**6. How a trade closes, including the two different trails**

The combined ladder is implemented in [backtester.py:677](/home/engine-33ai/codes/trading/core/backtester.py:677) and [position_protection.py](/home/engine-33ai/codes/trading/core/position_protection.py:39). Paper fills and settlement are in [paper_trader.py:1390](/home/engine-33ai/codes/trading/core/paper_trader.py:1390).

1. An already queued market exit executes at the next open. A saved `kronos_exit` therefore takes reason precedence over other orders facing the same opening price.
2. Otherwise, opening gaps through resting protective orders or the target execute first. A stop gap fills at the observed open, not the unavailable trigger price.
3. Intrabar checks use the nearest protective stop, with the target also active. If a bar could reach both without a known ordering, the engine favors protection. There are zero such original ambiguity cases in this export.
4. Only after the position survives those checks can that completed close update references, activate the pattern trail, or ratchet independent paper protection for the next bar. Close-dependent pattern decisions queue an exit for the next open. Kronos position decisions are also queued after completed-bar management.
5. Closing a long sells with adverse downward slippage; covering a short buys with adverse upward slippage. Both entry and exit fees are deducted. Short proceeds remain offset by the position liability and funding reserves.

| Executed reason | Trades | Winners | Net P&L | Actual trigger source |
| --- | ---: | ---: | ---: | --- |
| `stop_loss` | 19 | 0 | -$5,696.65 | Independent paper protection in all 19 |
| `trailing_stop` | 11 | 4 | -$316.44 | Independent paper protection in all 11 |
| `kronos_exit` | 5 | 3 | +$811.41 | Queued Kronos market exit |
| `take_profit` | 0 | 0 | $0.00 | No original target filled |

The independent paper trail ratchets immediately after a favorable surviving net close. Its floor is:

```text
net floor = max(-3%, best surviving net return - 3 percentage points)
```

It has no pattern-R activation threshold. A positive peak changes its reported reason to `trailing_stop`, even when the floor remains below breakeven. Seven of the eleven trailing exits lose money. For example:

| Position | Best surviving net close | Final net return | Explanation |
| --- | ---: | ---: | --- |
| CVX short | +2.334% | -0.666% | Exactly three points of giveback |
| LNG long | +0.218% | -2.782% | Small profit never established a positive floor |
| NTES short | +4.376% | +1.376% | Three points of giveback retain a profit |
| ACLS short | +11.102% | +4.209% | Opening gap exceeds the intended giveback |

Nine eventual losing trades had at least one positive surviving net close. That is evidence of profit giveback, not proof that an earlier profitable exit was executable at the eventual best close or that an automatic breakeven rule would improve future results.

The **pattern trail** is separate. Its saved activation threshold is 6%–16%, median **11.53%**; this is median **4.20 times the actual initial gross paper-stop risk**. It never activates in these 35 trades. The general settings `stop_loss_trail_r=1` and `stop_loss_arm_r=1.75` do not replace the Patternsv2 ladder: [the scanner explicitly skips that override for V2](/home/engine-33ai/codes/trading/core/scanner.py:1793). Changing those legacy settings would therefore miss the mechanism producing these exits.

**7. Targets, holding time, and inconsistent definitions of R**

Original targets are **10.78%–75.26%** away from executed entry, with a median of **31.83%**. The median is **11.59 initial gross paper-stop R**, while the median holding period is only two sessions. No original target executes. In practice, this book trades short-lived safety-stop/model outcomes while carrying much longer-distance pattern objectives.

There are three related but different quantities:

| Quantity | Meaning in the current implementation |
| --- | --- |
| Pattern risk | Distance to the nearer structural/ATR stop; used when opening code rescales the pattern ladder and applies `target_max_r` |
| Reported initial R | Gross price distance to the nearest initial stop, including paper protection after installation |
| Paper loss budget | 3% of entry notional **net** of configured fees and exit slippage |

An ordinary full paper stop is approximately **-1.09 reported net R**, because the denominator is the gross trigger distance of about 2.75%, while the loss numerator includes 3% net costs. This is an intelligible accounting convention, but it must be labeled. “One R” should not silently mean a gross stop distance in one place, a pattern stop in another, and a net loss budget elsewhere.

More consequentially, [production target capping](/home/engine-33ai/codes/trading/core/execution.py:149) happens **before independent protection is installed**. The existing review's `target_max_r=2` diagnostic instead uses `trade_risk_dollars()` after paper protection. These are different target distances on every trade in this export.

Holding the saved entries and all other observed decisions fixed:

| Interpretation of “2R target” | Net P&L | Wins | Profit factor | Changed exits |
| --- | ---: | ---: | ---: | ---: |
| Two times actual initial gross paper-stop distance | -$2,910.71 | 9 | 0.570 | 7 |
| Two times structural/ATR pattern stop distance, matching the production option's basis | -$4,052.17 | 7 | 0.453 | 1 |

The second row reconstructs the target formula from each saved fill's pattern stop/cap and replays that target. It is still a fixed-entry diagnostic, not a new funded run. **Enabling `TARGET_MAX_R=2` does not implement the first row.** The only changed exit under the production-basis diagnostic is ACLS.

**8. Gaps explain all three losses beyond the 3% budget**

| Position | Resting paper trigger | Next raw open | Net loss | Net return |
| --- | ---: | ---: | ---: | ---: |
| NVMI long | $366.624927 | $365.510010 | -$323.01 | -3.2953% |
| AME short | $232.952102 | $233.250000 | -$312.41 | -3.1316% |
| MTZ short | $296.700538 | $323.880005 | -$475.55 | -4.7967% |

MTZ had reached a +6.9893% surviving net close. Its intended paper floor was +3.9893%, and Kronos also queued a next-open exit. The next open jumped through protection. Both routes faced the same $323.88 opening price; relabeling this `kronos_exit` as a stop cannot recover the unavailable $296.70 trigger.

ACLS remained profitable but had the same gap problem. Its paper trigger was $135.983998; the short covered at a $141.75 raw open, realizing +4.2093% instead of the intended +8.1017% floor. Thus giveback can exceed three points even without an overall losing trade.

Seventeen of the nineteen `stop_loss` exits lose exactly 3% net; NVMI and AME are the exceptions. MTZ is the third overall breach and is labeled Kronos. These are gap executions in the saved evidence, not reversed long/short logic or a fee double charge. The budget is a trigger policy, not an absolute bound across unavailable prices.

**9. Kronos selection is restrictive, unprofitable here, and not an untouched evaluation**

All 35 entry forecasts have verified model file identity but explicitly record **`out_of_sample_eligible=false`**. The declared checkpoint selection boundary is **6 November 2026**, with eligibility beginning **7 November 2026**. Every filled entry decision precedes that boundary. The gate allows historical forecasts with this flag; [provenance code records the distinction rather than vetoing the forecast](/home/engine-33ai/codes/trading/core/kronos_provenance.py:10).

This sample therefore cannot certify the selected model's future trading edge. It does not prove that every individual bar was trained on; it establishes that the declared model-selection boundary prevents treating these fills as an untouched holdout. Three stochastic samples from the same checkpoint are not three independently trained models.

The selected trades provide weak evidence for the forecast-to-execution objective:

| Entry forecast agreement | Trades | Winners | Net P&L |
| --- | ---: | ---: | ---: |
| 2 of 3 samples | 27 | 6 | -$3,244.60 |
| 3 of 3 samples | 8 | 1 | -$1,957.08 |

Increasing vote unanimity would not have rescued this observed subset. Confidence is also poorly informative as a probability: 31 of 35 filled journal confidences equal 1.0, and the adapter explicitly treats its score as uncalibrated geometric strength.

Using the raw closing price saved in each entry day's OHLC, only **11 of 35** closes move in the predicted direction relative to the forecast anchor. From the actual slipped entry, **16 of 35** have a positive hypothetical net entry-to-that-close return. The latter average is **-0.686%**, compared with a +3.141% average trade-direction mean forecast after the saved opening cost adjustment. These are forecast/path diagnostics on selected trades; they include later closing prices after an intraday stop-out and are not additional realized trades.

The entry model predicts a one-day closing outcome; the position can be stopped intraday, or survive for up to 15 sessions under different exit rules. A correct closing direction does not guarantee survival of that stop path, and a confirmed one-day vote does not establish that a 31.8%-away pattern target is reachable before protection exits.

Kronos position exits have a separate policy: an opposing vote may queue a next-open exit, subject to an entry-displacement noise band. Five such exits produce +$811.41 in total. That conditional subgroup does not prove the exit layer adds $811.41 relative to other exits.

A bounded replay that removes only the saved Kronos exit requests shows why:

- MORN instead hits paper protection in the same final session, earning **$276.42**, versus $399.83 actually earned.
- PWR instead hits paper protection in the same final session, earning **$335.33**, versus $493.71 actually earned.
- MTZ still covers through the gap for **-$475.55**, with its reason becoming `trailing_stop`.
- SUPN and MMS remain open when their original saved paths end. Their later exits are unknown.

There is consequently no complete “Kronos exits off” portfolio result from this export. Similarly, rejected opportunities lack complete subsequent management paths. Removing the entry gate could improve or worsen results; the 93% rejection rate alone cannot answer that question.

**10. Which cohorts lose**

| Side | Trades | Wins | Net P&L | Profit factor | Average net P&L |
| --- | ---: | ---: | ---: | ---: | ---: |
| Long | 19 | 2 | -$3,701.96 | 0.150 | -$194.84 |
| Short | 16 | 5 | -$1,499.72 | 0.508 | -$93.73 |

Both directions lose. Disabling shorts would leave the more negative long subset. Short accounting and stop direction reproduce correctly.

| Patternsv2 family | Trades | Wins | Net P&L | Profit factor |
| --- | ---: | ---: | ---: | ---: |
| Falling wedge | 14 | 1 | -$3,040.62 | 0.077 |
| Symmetrical triangle | 12 | 4 | -$923.39 | 0.545 |
| Descending channel | 1 | 0 | -$475.55 | 0.000 |
| Descending triangle | 1 | 0 | -$295.54 | 0.000 |
| Ascending triangle | 1 | 0 | -$264.51 | 0.000 |
| Ascending channel | 6 | 2 | -$202.08 | 0.806 |

Falling wedges, all long here, contribute **58.5% of net loss** and 13 losing trades. The other five long positions also lose, together **-$661.34**. No traded pattern family is profitable in this export. Pattern names describe geometry; for example, the observed ascending-channel positions are shorts and the descending-triangle position is long.

The DII journal can be linked to filled signal identities:

| DII outcome at signal | Filled trades | Wins | Net P&L |
| --- | ---: | ---: | ---: |
| Would veto, but ran in Shadow | 23 | 4 | -$3,595.87 |
| No DII shadow veto recorded | 12 | 3 | -$1,605.80 |

Restoring the veto would remove many historical losers, but the retained saved subset still loses. It is not a complete DII-filtered funded replay. The same limitation applies to muting falling wedges after inspecting their losses.

All 35 trades are on different symbols. There is no repeated-symbol loss-chasing episode within these fills. The worst realized closing session is **23 October 2026, -$889.70**, from HP, SAIA, and URI. The configured $1,500 daily entry-loss limit was never reached, and it is an admission control rather than an instruction to liquidate every position. No daily-limit rejection appears.

The loss is therefore not explained by an oversized concurrent book or one enormous outlier. Typical notional is about 10% of initial capital per position, ordinary initial net loss about $300 or 0.3% of initial capital, and peak overlap is three positions. Repeated negative expectancy accumulates the drawdown.

**11. What smaller-target replays do and do not establish**

The following caps use **reported initial gross paper-stop R**, preserve original entries, quantities, costs, and saved external exit requests, and never need bars after an original exit. If a tighter target and stop could both touch in an unordered daily bar, the engine favors protection.

| Maximum target distance | Winners / 35 | Net P&L | Profit factor | Trades with an ambiguous stop/target bar |
| --- | ---: | ---: | ---: | ---: |
| Original measured targets | 7 | -$5,201.68 | 0.297 | 0 |
| 0.5R | 18 | -$2,809.38 | 0.443 | 3 |
| 1R | 13 | -$3,096.74 | 0.515 | 1 |
| 1.5R | 9 | -$3,248.24 | 0.520 | 1 |
| 2R | 9 | -$2,910.71 | 0.570 | 0 |
| 3R | 7 | -$4,471.62 | 0.396 | 0 |
| 4R | 7 | -$4,471.62 | 0.396 | 0 |

Every inspected cap still loses. The 0.5R cap raises the win rate above 50% while retaining strongly negative expectancy because its average winner is only $123.97 against an average loser of $296.52. Win rate alone is a poor optimization target.

The paper-risk 2R cap improves P&L by **$2,290.97**. Its seven changed exits are:

| Symbol | Original net P&L | Capped-target net P&L | Difference |
| --- | ---: | ---: | ---: |
| MORN | +$399.83 | +$523.59 | +$123.76 |
| PRGO | -$166.56 | +$524.63 | +$691.19 |
| SCHW | +$254.77 | +$520.44 | +$265.67 |
| ACLS | +$418.40 | +$521.83 | +$103.43 |
| MTZ | -$475.55 | +$520.48 | +$996.02 |
| SUPN | +$423.32 | +$524.68 | +$101.36 |
| PWR | +$493.71 | +$503.25 | +$9.53 |

MTZ and PRGO account for **73.6%** of the improvement. Both long and short capped subsets still lose: **-$2,621.34** and **-$289.37**, respectively. The identical 3R/4R results arise because the changed ACLS exit gaps through both alternative targets at the same observed open.

These comparisons diagnose the target/holding-time mismatch. They do not select a validated optimal cap. They retain the historical opportunity set and do not reallocate freed capital or regenerate forecasts after a changed exit. All parameters have now been inspected on this sample.

Likewise, removing independent paper protection leaves **24 of 35 trades still open at the end of their original individual evidence windows**. A result valued at those different cutoffs would not establish whether wider stops improve the portfolio through 9 November. Complete later paths are required for that experiment.

**12. A misleading boundary-target comparison in the existing review**

[boundary_objective_comparison()](/home/engine-33ai/codes/trading/analysis/paper_trade_review.py:271) assumes the old adapter anchored every target to the signal close, then applies:

```text
alternative_target = saved_target + breakout_boundary - signal_close
```

It checks that the requested and resolved targets agree, but does not verify which adapter policy created the original target. That assumption is invalid for demonstrably corrected targets in this export.

For **ACLS**, the saved breakout boundary is **$160.865013**. The two saved rails have a width of **$42.768318** on the breakout bar, the measured maximum for that formation. The correct boundary-based short target is therefore:

```text
160.865013 - 42.768318 = 118.096695
saved target rounded to four decimals = 118.0967
```

The helper nevertheless shifts it again using the $144.50 signal close:

```text
118.0967 + 160.865013 - 144.50 = 134.461713
```

That is a new, closer target, not a correction to the saved objective. Its apparent $730.05 improvement is exactly the helper's whole-book “boundary correction” improvement, from -$5,201.68 to -$4,471.62.

I independently reconstructed the two rails and formation width for the **10 trades whose entire saved rail/marker history fits within the journal's 60-bar decision context**. All ten match the saved boundary-based objective within $0.00006. The other 25 were not certified by this reconstruction because their rail starts extend outside that context; they must not be presumed to use the old policy.

The helper's baseline verification remains useful, but its boundary-comparison label is unsafe on this export. Any repair should record/require an objective-policy version or reconstruct the original measured height before claiming a correction. Do not change the saved targets based on this output.

The export also loses broader provenance that already exists in the account. [engine_hash()](/home/engine-33ai/codes/trading/core/pattern_provenance.py:14) hashes `backtester.py`, `execution.py`, and `position_protection.py`. It does not pin the Patternsv2 adapter/detectors, scanner gate implementation, or every threshold. [build_run_manifest()](/home/engine-33ai/codes/trading/core/run_manifest.py:10) already hashes the broader sources, including an external detector root when used, and the scanner records active controls in that manifest. However, [the trade exporter explicitly removes `run_manifest`](/home/engine-33ai/codes/trading/utils/trade_export.py:169), so it is absent from `pt.json`. Retain this existing evidence in future exports and add resolved policy identifiers/thresholds where missing. Enabled-pattern names and model identity alone cannot establish identical entry logic.

**13. Recommended work, in order**

| Priority | Concrete work | Why it comes first | Acceptance evidence |
| --- | --- | --- | --- |
| 1 | Preserve breakout-boundary evidence in the signal and revalidate the thesis at the opening price. Define frozen versus projected rail semantics explicitly. | Six observed fills enter after returning inside the formation; current stop/target geometry does not detect this. | Mirrored long/short opening tests, causal rail projection if used, explicit rejection reason, and a full replay showing displaced opportunities as well as skipped losses. |
| 1 | Make risk bases explicit in target settings, analysis, and exports; align a diagnostic with the actual production formula. | The two “2R” policies differ by $1,141.46 in this same fixed-entry comparison. | Identical target levels between configured entry execution and its analysis, with gross/net budget labels and saved initial effective protection. |
| 1 | Repair or gate the boundary-objective helper. | It presently calls an additional target tightening a geometric correction. | An already boundary-anchored target is unchanged; a provably legacy target is corrected; missing policy/geometry is reported as unverified. |
| 2 | Evaluate a coherent entry/stop/target policy against a defined holding objective. Keep any chosen 3% budget explicit. | Sub-ATR protection, 31.8%-away targets, and two-session median survival do not work together in this sample. | Predefined volatility/entry rules and target alternatives, evaluated on complete bars with identical costs, funding, gap handling, and ambiguity rules. |
| 2 | Compare Kronos entry Off/Shadow/Confirm and position Exit/Shadow on synchronized opportunity streams. Freeze model and gate parameters first. | Selection is restrictive and loses, but rejected paths and alternative holding histories are incomplete here. | A checkpoint selected before the test period, later uninspected sessions, paired decisions with complete paths, and net portfolio outcomes. |
| 2 | Investigate falling-wedge longs as a separate failure cohort; compare DII without assuming it helps. | Wedges contribute 58.5% of net loss; DII-passing fills also lose. | Predeclared cohort comparisons on another period rather than thresholds selected from these 35 outcomes. |
| 3 | Include the existing run manifest in trade exports, add missing resolved policy thresholds, and persist a complete position-exit decision journal. Remove misleading active-gate comments. | The export discards broader source identity and active-control evidence; each trade retains only its latest Kronos exit evidence. | A run can be reproduced from its exported manifest, and all queue, rejection, deferred exit, and actual trigger decisions can be traced without inferring missing events. |

This export's observed win rate has a descriptive Wilson 95% interval of approximately **10.0%–35.9%**. Trades can share market conditions and overlap, so that interval is not a guarantee of independent trials. It reinforces the need for a larger, temporally separate evaluation rather than declaring a six-trade filter or seven-exit improvement a finished strategy.

The evidence supports prioritizing opening-thesis checks and trustworthy risk/analysis semantics, followed by a complete frozen-policy evaluation. It does not support a claim that changing one trailing parameter, removing shorts, enabling DII, or selecting a smaller target already solves the losses.

**14. Reproduction and calculation notes**

From the repository root, baseline execution verification and the existing bounded diagnostics can be regenerated without loading runtime ledgers:

```bash
.venv/bin/python -m analysis.paper_trade_review pt.json --output /tmp/pt-review.json
```

Interpret the generated `boundary_objectives` and `target_cap_2r` fields with the limitations above. Baseline verification passing does not validate the assumptions or labels of every counterfactual.

All tables use unrounded `resolved_rules.price` and quantity for calculation, with fees inferred from `entry_fee_paid`, slippage checked against the first saved OHLC open, and actual exits replayed from `daily_marks[].exit_check`. Displayed dollars are rounded only at presentation. Entry/exit dates in the ledger below are market session dates, rather than the wall-clock timestamps when the historical replay ran.

Additional diagnostics are reproducible with these definitions:

- Initial gross risk: quantity times distance to the nearest saved initial structural stop, percentage cap, or initial paper stop, using the direction-aware selection in `trade_risk_dollars()`.
- Entry inside boundary: `sign × (executed_entry - saved_level) < 0`, with `sign=+1` for BUY and `-1` for SELL; exactly one saved `hline` labeled `level` exists for each trade.
- Best surviving net close: highest hypothetical liquidation return after both configured fees and exit slippage among marks with no raw exit fill, floored at zero. The later close of an exit bar is excluded from this peak.
- Entry-day forecast diagnostic: the first mark's raw OHLC close, compared separately with the saved forecast anchor and with a hypothetical net liquidation from the actual entry. It is not the last mark's display `close`, which can be replaced by an exit fill.
- Paper-risk target sweep: `replay_trade(trade, target_max_r=cap)` for caps 0.5, 1, 1.5, 2, 3, and 4.
- Production-basis 2R diagnostic: choose the nearer saved structural/ATR stop, compute twice its distance from the executed entry, cap the original target at that level, round to four decimals, and call `replay_trade(trade, target_price=target)`.
- DII subset: join shadow rows beginning `DII` to filled trades by `signal_id`; count one trade once. This evaluates observed fill subsets, not newly funded alternative portfolios.

Focused verification passed **91 existing tests**: 58 analysis-replay/risk-geometry/Patternsv2-exit cases, 22 protection-recovery cases, and 11 synchronous Kronos exit-policy cases. The full Kronos exit test file stalled in its first asynchronous test at `asyncio.run()` and was interrupted; its remaining asynchronous cases are not claimed as passing. No new trading rules were implemented as part of this report.

**15. All observed trades**

Returns are net of configured fees and slippage. Sessions include the entry session. `Stop` and `Trail` below both refer to independent paper protection; `Model` is a queued Kronos exit.

| Symbol | Side | Patternsv2 family | Entry session | Exit session | Sessions | Net P&L | Net return | Exit |
| --- | --- | --- | --- | --- | ---: | ---: | ---: | --- |
| CVX | Short | symmetrical triangle | 2025-11-20 | 2025-12-01 | 7 | -$66.01 | -0.666% | Trail |
| FLO | Long | falling wedge | 2025-12-12 | 2025-12-23 | 8 | -$137.41 | -1.375% | Trail |
| BWIN | Long | symmetrical triangle | 2026-01-09 | 2026-01-12 | 2 | -$299.64 | -3.000% | Stop |
| CRC | Long | falling wedge | 2026-01-09 | 2026-01-12 | 2 | -$299.49 | -3.000% | Stop |
| NTES | Short | symmetrical triangle | 2026-01-16 | 2026-01-28 | 8 | +$136.61 | +1.376% | Trail |
| APLD | Short | ascending channel | 2026-02-06 | 2026-02-06 | 1 | -$299.99 | -3.000% | Stop |
| INDB | Short | ascending channel | 2026-03-02 | 2026-03-02 | 1 | -$297.68 | -3.000% | Stop |
| BXSL | Long | falling wedge | 2026-03-05 | 2026-03-06 | 2 | -$299.77 | -3.000% | Stop |
| JKHY | Long | falling wedge | 2026-05-04 | 2026-05-05 | 2 | -$298.40 | -3.000% | Stop |
| AIR | Short | symmetrical triangle | 2026-05-18 | 2026-05-20 | 3 | +$75.81 | +0.759% | Trail |
| IOT | Long | symmetrical triangle | 2026-05-20 | 2026-05-20 | 1 | -$299.44 | -3.000% | Stop |
| MORN | Long | symmetrical triangle | 2026-06-01 | 2026-06-02 | 2 | +$399.83 | +4.009% | Model |
| BL | Long | falling wedge | 2026-06-02 | 2026-06-02 | 1 | -$299.95 | -3.000% | Stop |
| KKR | Short | symmetrical triangle | 2026-06-04 | 2026-06-04 | 1 | -$299.19 | -3.000% | Stop |
| PRGO | Long | symmetrical triangle | 2026-06-09 | 2026-06-11 | 3 | -$166.56 | -1.667% | Trail |
| SCHW | Long | falling wedge | 2026-06-11 | 2026-06-18 | 6 | +$254.77 | +2.570% | Trail |
| LNG | Long | falling wedge | 2026-06-29 | 2026-07-01 | 3 | -$276.95 | -2.782% | Trail |
| ACLS | Short | ascending channel | 2026-07-06 | 2026-07-09 | 4 | +$418.40 | +4.209% | Trail |
| MTZ | Short | descending channel | 2026-07-29 | 2026-07-30 | 2 | -$475.55 | -4.797% | Model |
| APPF | Long | descending triangle | 2026-07-30 | 2026-07-30 | 1 | -$295.54 | -3.000% | Stop |
| SUPN | Short | ascending channel | 2026-08-25 | 2026-09-15 | 15 | +$423.32 | +4.236% | Model |
| CWEN | Long | falling wedge | 2026-09-09 | 2026-09-10 | 2 | -$299.47 | -3.000% | Stop |
| NVMI | Long | falling wedge | 2026-09-09 | 2026-09-10 | 2 | -$323.01 | -3.295% | Stop |
| LITE | Short | ascending triangle | 2026-09-15 | 2026-09-16 | 2 | -$264.51 | -2.855% | Trail |
| ASND | Short | symmetrical triangle | 2026-09-16 | 2026-09-16 | 1 | -$294.68 | -3.000% | Stop |
| ESI | Long | falling wedge | 2026-09-23 | 2026-09-24 | 2 | -$143.66 | -1.437% | Trail |
| MDB | Short | symmetrical triangle | 2026-09-29 | 2026-09-29 | 1 | -$291.43 | -3.000% | Stop |
| EG | Short | ascending channel | 2026-09-30 | 2026-10-08 | 7 | -$146.94 | -1.475% | Trail |
| NRG | Long | falling wedge | 2026-10-08 | 2026-10-08 | 1 | -$296.85 | -3.000% | Stop |
| MMS | Long | falling wedge | 2026-10-09 | 2026-10-12 | 2 | -$29.90 | -0.300% | Model |
| PWR | Short | symmetrical triangle | 2026-10-16 | 2026-10-21 | 4 | +$493.71 | +5.150% | Model |
| HP | Short | ascending channel | 2026-10-23 | 2026-10-23 | 1 | -$299.18 | -3.000% | Stop |
| SAIA | Long | falling wedge | 2026-10-23 | 2026-10-23 | 1 | -$295.30 | -3.000% | Stop |
| URI | Long | falling wedge | 2026-10-23 | 2026-10-23 | 1 | -$295.22 | -3.000% | Stop |
| AME | Short | symmetrical triangle | 2026-11-06 | 2026-11-09 | 2 | -$312.41 | -3.132% | Stop |
