**The latest run has a negative trading edge under its recorded execution policy. Improving targets and holding periods reduces the loss, but no tested replacement makes it profitable.** The main problems are losses on the entry bar, a stop/target mismatch in Patternsv2, and a largely short RSI-divergence strategy that loses independently of Patternsv2. Costs and opening gaps make those weaknesses substantially more expensive.

This report analyzes the **14 November 2026 export**, not the older 35-trade export discussed in `pt.md`. All results below are USD for the US book. The PH book has zero positions and zero trades; its ₱1,000,000 is excluded.

![Loss attribution](verification/pt-review-2026-11-14/loss-analysis.png)

**What actually happened**

| Measure | Latest export |
| --- | ---: |
| Replay period | 14 Nov 2025–13 Nov 2026; 251 daily replay steps |
| Initial capital / ending marked equity | $100,000 / $77,492.10 |
| Total marked P&L | −$22,507.90 / −22.51% |
| Closed trades / winners / losers | 422 / 172 / 250 |
| Closed-trade P&L | −$22,606.01 |
| Open positions / marked P&L | 2 shorts / +$98.11 |
| Profit factor / win rate | 0.642 / 40.76% |
| Mean net P&L / mean net R | −$53.57 / −0.201R per closed trade |
| Average winner / average loser | +$236.20 / −$252.93 |
| Break-even win rate at those payoffs | 51.71% |
| Median holding length | 2 native bars; daily and weekly must be separated |
| Maximum drawdown from saved marks | $25,592.46, or 25.59% of starting capital |
| Peak overlapping positions, including entry bars | 9 |

At the observed 40.76% win rate, the average winner needed to be **$367.63**, about 56% larger than the actual winner. Merely increasing the advertised target does not accomplish this: trades usually exit long before reaching it.

The drawdown is reconstructed from the export's marks, not an independently sampled intraday equity curve. Weekly positions have weekly marks; their labels do not supply true daily portfolio values. The account's $97,340.41 cash balance includes short-sale accounting and must not be mistaken for equity. Hypothetically liquidating the two shorts at their saved marks after configured exit costs reduces account equity to $77,462.31; this is an accounting stress, not a certified fill.

**The losses are not confined to one strategy or side**

| Cohort | Closed trades | Net P&L | Profit factor | Mean net R |
| --- | ---: | ---: | ---: | ---: |
| Patternsv2, all daily | 232 | −$9,528.02 | 0.683 | −0.152 |
| RSI divergence, daily | 173 | −$10,349.66 | 0.633 | −0.225 |
| RSI divergence, weekly | 17 | −$2,728.34 | 0.451 | −0.630 |
| All longs | 123 | −$6,788.23 | 0.613 | −0.203 |
| All shorts | 299 | −$15,817.78 | 0.654 | −0.200 |

RSI divergence contributes **57.85%** of the realized loss. Removing its observed trades leaves a losing Patternsv2 subset; it does not establish a profitable replacement portfolio. Turning off shorts likewise leaves a losing long subset.

Patternsv2 longs lose $4,570.42 across 119 trades; Patternsv2 shorts lose $4,957.60 across 113. RSI contains 186 closed shorts losing $10,860.18 and only four closed longs losing $2,217.82. Those four longs are too few for a reliable side conclusion.

**1. Entry-bar failures deserve the first research effort**

There are **146 exits on the entry bar**, losing **$25,156.66**. These comprise 112 stop losses and 34 take profits. The other 276 trades make **$2,550.65** in aggregate. Restricting this calculation to daily trades gives 139 entry-session exits losing $23,506.75; seven observations in the broader calculation are entry-week exits.

For Patternsv2 specifically, 51 entry-bar exits lose **$14,843.97**: 50 losses and one very small winner. Every later exit improvement starts too late to prevent those 50 immediate failures. This supports investigating when a breakout is admitted and how its protection fits normal volatility.

It does **not** support a rule that selects only trades that survive their first day. Survival is known after entry and is a hindsight filter. Actionable experiments would use information available beforehand: a confirmed retest as a new entry event, a later completed-bar confirmation, or a volatility-compatible bracket with smaller quantity. These alternatives need fresh signal generation and full replay; their profitability cannot be inferred by deleting the first-bar losers.

The old invalid-opening-breakout defect is already guarded in this run. Its journal records 44 rejections for returning inside the frozen Patternsv2 boundary. That earlier fix is not a new $14,844 saving available from these filled trades.

**2. Patternsv2's actual exits describe a short-horizon strategy with distant pattern objectives**

| Measure | Patternsv2 | RSI divergence |
| --- | ---: | ---: |
| Median effective initial stop / signal-time ATR | 0.774 ATR | 0.710 ATR |
| Median original target / initial gross risk | 8.85R | 1.64R |
| Median original target distance | 24.36% | 4.28% |
| Median native holding bars | 2 | 1.5 |
| Original take-profit exits | 1 of 232 | 54 of 190 |

Across both strategies, the independent paper stop is the binding initial stop on **399 of 422** closed trades. Its 3% net-loss budget leaves approximately 2.75% gross price distance after costs. There is no requirement that this bracket preserve the detector's structural invalidation. In Patternsv2, the original pattern trail activates on only **one** closed trade; changing legacy trail settings would not repair the active exit policy.

There are 301 trades with initial protection closer than one signal-time ATR. First-bar exits have median initial distances of 0.585 ATR for Patternsv2 and 0.556 ATR for RSI. This is a coherent explanation for frequent stop-outs, but the export does not establish that widening stops creates an edge.

In fact, simply keeping trades with at least one ATR to the stop leaves **121 trades, −$6,902.17, PF 0.507**. At least 1.5 ATR leaves only **22 trades, −$874.20, PF 0.587**. All four coarse ATR buckets lose in both strategy families. Do not ship `paper_v2_min_stop_atr=1` on the strength of this sample: it removes activity without improving the observed profit factor. That setting applies only to V2; the totals here describe both families. A structural-stop strategy with quantity scaled to risk is a different experiment from an ATR admission filter.

The existing 3% giveback is also not a break-even trail. A small favorable surviving close can activate its accounting state while the resting stop still permits a net loss. That explains why an exit called `trailing_stop` is often negative; it is not automatically an arithmetic defect.

| Actual exit reason | Trades | Net P&L |
| --- | ---: | ---: |
| Stop loss | 150 | −$44,133.95 |
| Trailing stop | 59 | −$6,274.08 |
| Take profit | 55 | +$15,855.42 |
| Kronos exit | 158 | +$11,946.60 |

These are descriptive outcome groups. Selecting trades by eventual exit reason would be lookahead.

**3. Shorter targets help, but are insufficient**

I replayed the full coarse target grid below using the production target-cap formula. A cap only moves an existing target closer; it leaves an already nearer target unchanged. Entries, quantities, costs, original external exit decisions, and available OHLC remain fixed. Both risk bases are shown because “2R” can mean very different prices.

| Target cap | Pattern-risk basis: closed P&L | Effective initial-protection basis: closed P&L |
| --- | ---: | ---: |
| None: baseline | −$22,606.01 | −$22,606.01 |
| 1R | −$18,492.58 | −$17,846.62 |
| 1.5R | −$19,072.75 | −$16,782.05 |
| 2R | −$20,988.59 | −$16,493.67 |
| 3R | −$21,585.21 | −$19,972.87 |

The **2R initial-protection diagnostic improves closed P&L by $6,112.34**, changes 44 trade outcomes, and raises PF to **0.733**. It still loses in both strategy families: Patternsv2 −$6,291 and RSI −$10,203. Its first chronological half is worse than baseline (−$8,728 versus −$8,068), while its second half improves substantially. That instability matters when choosing a policy.

The matching existing controls for this experiment are `target_risk_basis=paper_initial` and `target_max_r=2`. The denominator is the nearer initial effective stop distance, not a net-of-fees 2R return and not necessarily the original structural stop. This is a research arm to test, not a validated production recommendation.

There are five baseline trades with bars touching both resting stop and target without a resolving opening gap. The saved stop-first convention is reproduced. The 2R initial-protection replay has seven such trades, including two new ambiguity cases. Daily bars cannot reveal their actual intrabar sequence.

All target alternatives resolve inside the original observed paths; none requires later bars for a formerly closed trade. The two original open positions are kept separate. For example, the 2R candidate's total including hypothetical liquidation is −$16,425.35, compared with −$22,537.69 for baseline. Neither value is the marked-account P&L quoted at the start.

**4. A shorter holding objective is a useful second experiment**

The following adds a decision after N surviving native bar closes and executes at the next observed open. Earlier baseline exits remain. It does not use the decision close as an available next-open fill. Weekly bars remain weekly.

| Time-exit decision | Always exit at next open | Exit only if net liquidation estimate is nonpositive at decision |
| --- | ---: | ---: |
| After 1 surviving bar | −$15,121.55 | −$20,303.89 |
| After 2 surviving bars | −$21,042.93 | −$21,960.57 |
| After 3 surviving bars | −$21,168.31 | −$21,825.21 |
| After 5 surviving bars | −$21,586.17 | −$22,435.44 |
| After 10 surviving bars | −$22,726.74 | −$22,544.31 |

These are closed-trade results on the same 422 entries. The best result in this displayed grid is still a **$15,122 loss and PF 0.713**. A one-bar objective improves both strategy families and both chronological halves, making it a more consistent candidate for further testing than longer loss-only time stops. It still cannot prevent first-bar stop-outs. It also cannot be configured through the legacy fixed-stop/trail knobs; a strategy-level native-bar time exit would need an explicit implementation and evaluation.

These target and time-exit savings must not be added together. I have not tested their combined interaction or the opportunities displaced or enabled by changed holding periods.

**5. The RSI addition needs its own research book and entry economics**

The detector accepts any correctly signed RSI difference; it does not require a minimum divergence magnitude. In the observed closed trades, 74 signals with less than two RSI points of divergence lose $6,314; 56 with two to five points lose $3,852; 60 with five or more lose $2,911. Stronger divergence is not a demonstrated profitable filter.

The target check only requires a positive net reward after costs. It does not require reward to compensate for the initial loss budget. Among RSI trades:

| Planned target reward / initial loss, both net of configured costs | Trades | Net P&L | PF |
| --- | ---: | ---: | ---: |
| Below 1 | 72 | −$5,490.68 | 0.474 |
| 1–2 | 48 | +$1,004.55 | 1.161 |
| 2–4 | 44 | −$3,227.67 | 0.644 |
| At least 4 | 26 | −$5,364.19 | 0.280 |

The middle group is an exploratory clue, not an optimized band to deploy. Very distant targets also lose badly, so a generic “minimum 2R” filter is not supported here. A basic net reward/risk floor of one, applied to all observed entries, leaves 349 trades losing $17,181 with PF 0.675.

The RSI strategy also does not receive a balanced long/short evaluation. Of **2,761 distinct RSI BUY decisions, 2,602 are rejected by the mandatory daily DII bulls-uptrend gate** and only four fill. Of 3,089 RSI SELL decisions, 188 fill, including the two still open. The reversal strategy therefore mostly becomes a short strategy under the combined controls.

Across the whole run, the opportunity funnel reconciles exactly: **6,318 decisions = 5,817 rejected + 424 filled + 77 pending**. All 77 pending entries are RSI; they are unfilled candidates, not losses. Of all rejections, 2,609 are the per-pattern position cap. RSI shares two concurrent slots across daily and weekly signals. Changing a holding rule or disabling weekly RSI would change which daily opportunities get those slots, so subtracting a losing subset is not a funded rerun.

The broader DII setting being `shadow` applies to a separate pattern-band filter. `run_manifest.active_controls.dii_bulls_uptrend` still says `mode=filter`, one day, and applies to every BUY. A report that says “DII was off” would be wrong. Test the long gate explicitly against a shadow arm; this export does not tell us whether its rejected longs would have won.

**6. Fees and gaps materially worsen a strategy that already loses**

Holding the actual raw execution events and quantities fixed gives this accounting decomposition:

| Component | P&L effect |
| --- | ---: |
| Raw entry/exit event P&L before modeled friction | −$10,016.67 |
| Fees on closed trades | −$8,392.89 |
| Adverse entry and exit slippage | −$4,196.44 |
| Closed-trade net P&L | −$22,606.01 |

Costs explain **55.7%** of the net loss. At the configured 0.10% fee and 0.05% slippage per side, an approximately $10,000 round trip costs about $30. Actual average friction is $29.83 per closed trade, on top of a $23.74 average raw-event loss. Lower real brokerage costs would help, but even eliminating both modeled deductions leaves a $10,017 raw-event loss. This is an accounting decomposition, not a cost-free strategy replay: changing costs also changes cost-aware stops and admission.

There are **17 trades losing more than the 3% net budget**, all with an opening gap through the paper stop. Together they lose $10,026; the loss beyond a hypothetical exact 3% fill is $4,957. That latter amount is not an available saving: stops cannot fill at prices skipped by the market.

The largest examples are CORT short, −$1,834 / −18.43%; FICO weekly long, −$1,491 / −15.19%; and AJG weekly short, −$751 / −7.60%. CORT and FICO were labeled `kronos_exit`, but their saved opens already crossed protection. Deleting that model exit label would not restore a stop-price fill. Even excluding the three worst outcomes leaves **−$18,530 and PF 0.687**, so the loss is not just three unusual gaps.

A separate, verified earnings/event calendar and smaller event exposure are reasonable gap-risk experiments. Earnings dates, stock borrow, corporate actions, and actual spreads are not certified by this file. Do not reconstruct an event filter after seeing which trades gapped and claim it as a causal backtest improvement.

**7. Neither model exits nor simple filters should be blamed or promoted from outcome labels**

Kronos entry confirmation is **off**, while position exits are active with three stochastic samples and a two-vote direction decision. Model exit trades make $11,947 overall; Patternsv2's model exits alone make $12,818. This does not prove the model helped relative to holding, but it contradicts an explanation that all losses came from model exits. Comparing exit on versus shadow requires prices after the original model closes, subsequent portfolio capacity, and regenerated decisions.

The saved checkpoint has a declared selection-data end of **6 November 2026**, with eligibility from **7 November**. Of 544 saved position forecasts, **533 fail the model's own out-of-sample eligibility checks**. The year-long run therefore does not demonstrate a historically deployable model edge. Three samples from one checkpoint also do not constitute three independently validated forecasting systems. A new comparison needs a checkpoint fixed before the evaluation window.

Other obvious entry filters do not uncover a reliable edge:

| Keep only this observed subset | Trades | Net P&L | PF |
| --- | ---: | ---: | ---: |
| Signal volume ≥1.5 times prior 20-bar mean | 101 | −$4,760.76 | 0.682 |
| Trade direction agrees with close versus 50-bar SMA | 245 | −$8,654.53 | 0.718 |
| Initial protection ≥1 ATR | 121 | −$6,902.17 | 0.507 |
| All Patternsv2; exclude RSI | 232 | −$9,528.02 | 0.683 |

Volume is measured on each signal's native timeframe, using completed decision context and excluding the signal bar from the comparison mean. This proxy is not a complete replay of the app's volume gate, which has additional semantics. The trend measure is a 50-native-bar diagnostic, not the inactive SMA200 gate.

Among Patternsv2 trades, the ≥1.5 relative-volume subset actually has lower PF (0.628) than the full family (0.683). Across both families it makes $1,465 in the first chronological half and loses $6,225 in the second. Simply turning on volume confirmation is not a justified cure.

The pattern breakdown also changes the earlier diagnosis: falling wedges are now nearly flat (30 trades, −$105, PF 0.967), while ascending triangles (28, −$2,848, PF 0.370), ascending channels (36, −$2,724, PF 0.487), and symmetrical triangles (95, −$2,745, PF 0.768) contribute more. Descending triangles make $386 across 18 trades, but one or two tail outcomes can dominate such a small sample. Restricting trading to the winning pattern names would overfit this export.

**8. What I would change, in order**

| Priority | Concrete action | Purpose and required evidence |
| --- | --- | --- |
| 1 | Keep RSI in a separate research book; evaluate daily and weekly variants separately. | It adds $13,078 of observed losses, differs from Patternsv2, and shares two capacity slots across timeframes. Compare independently funded runs rather than subtracting trades. |
| 1 | Test a coherent Patternsv2 short-horizon bracket: baseline versus `paper_initial` 2R cap, then a separate one-bar/next-open time-exit arm. | These are the clearest bounded loss-reduction candidates. All remain losing; require complete opportunity and funding replay before promotion. Do not add their savings. |
| 1 | Research the entry itself: confirmed retest or later completed-bar confirmation, with explicit entry expiry and unchanged cost assumptions. | First-bar Patternsv2 failures lose $14,844. This requires new signals and prices; the export cannot score the counterfactual admission policy. |
| 2 | Make structural risk versus fixed notional protection an explicit strategy choice, with quantity scaled to the chosen risk. | The paper stop overrides the intended structural bracket on most trades. Wider stops alone increase loss size; ATR-only filtering was unsuccessful here. |
| 2 | Evaluate RSI's mandatory long gate and its net target economics independently. | Only four RSI longs fill; many targets pay less than the planned loss. Start with a simple net reward/loss floor, without fitting a narrow winning band. |
| 2 | Compare Kronos exits versus shadow using a checkpoint fixed before the next uninspected period. | Neither exit-reason P&L nor this checkpoint's historical run establishes model value. Keep all adverse forecasts, abstentions and rejected opportunities in the journal. |
| 3 | Reduce research notional or use an equity-based risk budget, and test verified event exposure controls. | This limits dollars at risk while the edge is unproven. The unchanged $10,000 position becomes 12.9% of ending equity; a normal $300 budget grows from 0.30% to about 0.39% of equity. It does not cure negative expectancy or cap gaps. |

Do not enlarge the universe to compensate for losses or assume a higher geometric confidence is a win probability. This run already has 1,759 configured symbols, rejects 92.1% of opportunities, and has `collect_first=false`. Capacity rules and candidate processing affect the chosen trades; scaling the same unprofitable selection process can scale the problem.

**How to decide whether a replacement is actually better**

The first and second halves are split at **14 May 2026**, by entry date. The combined strategy loses $8,068 and $14,538 respectively. Patternsv2 moves from +$739 / PF 1.062 to −$10,267 / PF 0.431; RSI loses in both halves. These are diagnostics after inspecting the full year, not untouched validation sets.

A seeded descriptive bootstrap resampling 13 entry-month blocks gives a 95% range for mean net P&L per trade of approximately **−$82 to −$26** overall, **−$77 to −$4** for Patternsv2, and **−$129 to −$14** for RSI. Only 13 blocks are available, partial endpoint months are included, and regimes and symbols remain dependent. These intervals summarize this sample; they neither prove a permanent market property nor validate any searched filter.

Freeze the candidate source, universe, controls, costs, sizing, model identity and selection cutoff before new evaluation. Use complete independent funded replays with identical opportunity tapes and enough completed trades for each strategy family; mark remaining positions at a common cutoff. Report net expectancy and PF, drawdown and gap tails, count, exposure and costs, performance across time, and all tested variants. Require improvement without collapsing activity to a handful of trades. A positive result should survive modest additional friction and should not rely on one ticker, one month, or the best two trades.

The existing `analysis.paper_policy_evaluation` can preregister supported target, ATR, pattern and Kronos controls. Its current allowed-control list does not include RSI enablement, the mandatory bulls gate, or a new time-exit policy; those comparisons need explicit evaluator support before they can receive the same eligibility checks. I have not created or backdated a validation plan for this already-inspected year. Repeated selection from the same history increases the chance of finding a spurious winner; see Bailey et al., [The Probability of Backtest Overfitting](https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf).

**Evidence and reproducibility**

Input: `pt.json`, exported **14 November 2026 at 17:28:42 Asia/Manila**. SHA-256:

```text
1ba55014ae995c8c09bb3eadcb206001dd0ab9a5bdeb5229297078f63a7dfd9f
```

US run ID: `002ef516c1b24f3499d4b7f8ab884b49`. Repository HEAD at review: `c15f79187f075864592c9d02771b167b3e880097`, with pre-existing local changes. The saved trades use exit-engine hash `610fbafd8d89a6486dead139e1372c36960fd83b1409dd68ee702fbdd82c9a2b`. Detailed current analysis source hashes are stored with the metrics.

The run ends with `termination_reason=end_of_tape`, complete scan status, and no recorded unresolved execution intervals. All **422 closed and two open positions** reproduce their saved orders, fill prices, stop/trail state, exit reasons, dates, bar counts, P&L and fees using `analysis.paper_trade_review`. Realized P&L, open marked P&L and the $8,412.86 fees including open entry fees reconcile to the account. All 10,210 journal rows fall within this scan's saved run window, and every filled position has matching journal identity. The saved OHLC checks have no inconsistent ranges. This verifies the exported execution evidence; it does not independently certify source prices, historical universe membership, corporate actions, borrow or real fills.

The existing review-tool tests pass: **38 passed** (`.venv/bin/pytest analysis/test_paper_trade_review.py -q`). The generated CSV independently reconciles to the 422-trade realized total. The final input hash remains unchanged, and all 18 bounded exit experiments remain losing. The report chart was rendered and visually inspected.

Reproduce from the repository root:

```bash
.venv/bin/python docs/verification/pt-review-2026-11-14/analyze.py pt.json
.venv/bin/python docs/verification/pt-review-2026-11-14/plot.py
```

Outputs: [metrics.json](verification/pt-review-2026-11-14/metrics.json), [trades.csv](verification/pt-review-2026-11-14/trades.csv), and the chart above. The script reports all eight target variants, all ten time-exit variants, entry subsets and chronological splits. Subset tables are attribution; alternative exits are bounded fixed-entry experiments. Neither is a complete strategy rerun with capital reallocation. No trading settings, production implementation, account state or input export was changed for this review.
