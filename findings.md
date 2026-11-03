# Paper trade post-mortem — `pt.json`

> **Status update — the ledger moved while this was acted on.** `pt.json` is a
> live paper-stream output. The committed file is now the **17-trade
> `2026-11-03T02:18:56Z` export: -$2,326.09, 23.5% win rate, PF 0.667** — not
> the 18-trade `05:59Z` snapshot below. The ledger **flipped sides**: shorts are
> **+$1,047.12** (`double_top` 2/8, **+$1,233.48**) and longs are **-$3,373.21**,
> dominated by one real **-40% overnight gap** (EFOR, -$4,041.28). Therefore the
> "stop trading shorts" and "exclude `double_top`" conclusions **do not survive**
> and were **not** shipped as defaults. The surviving, ledger-independent finding
> is the trail/stop scale mismatch (still 0 trailing exits). See **Implementation
> status** at the end. Keep validating on a frozen ledger; do not tune on a live
> file.

**Snapshot A (superseded, retained for the analysis).** Export:
`2026-11-03T05:59:02Z` (`filter: all`). Only the **US book** had data:
**18 closed, 0 open**. The PH book is empty (0 rows), so nothing below applies to
PSE. All figures are USD.

## Headline

| Metric | Value |
|---|---:|
| Net P&L | **-$1,405.17** |
| Win rate | **11.1%** (2/18) |
| Profit factor | 0.64 |
| Avg win / avg loss | $1,259.05 / -$245.20 |
| Payoff ratio | 5.13 |
| **Break-even win rate** | **16.3%** |
| Expectancy | **-0.40R per trade** (sum -7.26R) |
| Exits | 15 stop_loss, 2 take_profit, 1 kronos_exit |
| Exited on the first bar | **13 / 18** (median hold = 1 bar) |
| Fees | $362.58 |
| Gross before fees | -$1,042.59 (fees are **26% of the loss**) |

The two winners are the entire result: RUM +$1,532.64 and SMHI +$985.46
(+$2,518 combined); the other 16 trades lost -$3,923.27. Remove those two and
the book is -15.8R. **This is not an edge with a bad exit — it is a sample of 18
with no demonstrated edge, whose exit geometry also leaks money.**

## The two mechanical bugs (highest confidence)

### 1. The trailing stop is calibrated to a stop the engine did not use, so it never arms

The pattern sets the trail and its arming distance as **2R of the ATR stop**
(`patterns_v2/signals.py:312-315`, where `stop_pct = clamp(1.5*ATR14, 3%, 8%)`):

```
trailing_stop_pct      = 2 * stop_pct     # 11%–16% in this ledger
trailing_activation_pct = 2 * stop_pct    # 11%–16%
```

But `_apply_fixed_stop` (`core/scanner.py:1461-1480`) then overwrites the stop
with a flat **3% of the signal close** and sets `stop_loss_pct_cap = 0.03`
(`STOP_LOSS_PCT` in `config.py:74`). It does **not** recompute the trail. The
executed stop is therefore ~3% (often less, because `_effective_stop` in
`core/backtester.py` takes the tighter of the resolved stop and the cap), while
the trail still waits for an **11%–16%** move.

Evidence in the export:

| Symbol | Effective risk | Export trail / activation |
|---|---:|---:|
| STKS | 0.99% | 15.58% |
| OXM | 1.10% | 16.00% |
| MYSE | 1.08% | 16.00% |
| XP | 1.66% | 13.64% |
| NBR | 2.98% | 15.30% |
| FIGR | 0.03% | 16.00% |

Consequence: **zero trailing exits in 18 trades**. Trades that ran +2R went
back to the 3% stop and were counted as -1R. Measured on the frozen tapes:

- median favorable excursion **before** the stop was hit: **1.79R**
- **10/18** reached ≥1R and **9/18** reached ≥2R before stopping
- only **3/18** ever touched the measured-move target within 20 bars (RUM and
  SMHI, the two winners, plus FLEX only *after* it had already been stopped)

So the book repeatedly gave back a real move because the only profit-protection
rule was armed at 4–5× the actual risk.

### 2. The fixed stop is anchored to the signal close, but the fill is the next open

`_apply_fixed_stop` computes `stop = signal.price * (1 -/+ 0.03)` from the
**signal-bar close**. Execution then fills at the **next observed open**
(`entry_mode: next-observed-open`, `core/execution.py:65`). An adverse gap eats
the risk budget before the trade starts. The existing gap guard
(`core/execution.py:72-75`) only rejects an open that has already crossed the
stop — it does not reject a gap that leaves almost no room.

| Symbol | Signal close | Fill | Adverse gap | Effective risk |
|---|---:|---:|---:|---:|
| FIGR | 29.33 | 30.20 | +2.97% | **0.03%** |
| STKS | 2.46 | 2.41 | -2.03% | 0.99% |
| OXM | 39.87 | 40.62 | +1.88% | 1.10% |
| MYSE | 2.11 | 2.15 | +1.90% | 1.08% |
| NEXA | 13.31 | 12.60 | -5.33% | 3.0% (resolved stop was 8.8% away, cap wins) |

FIGR is the degenerate case: a 0.03% stop is not a risk level, it is a coin
flip. The trade was stopped 0.23% later. One signal (HYMC) was correctly
rejected for "opening gap crossed the structural stop", but the guard does not
catch the near-zero-risk version.

## Root causes, ranked

1. **No demonstrated entry edge.** Win rate 11.1% vs the 16.3% break-even the
   realized payoff requires. 15 of the 16 losers are -1R stops (NBR is -0.8R on
   a Kronos exit); the two winners are both target hits. A driftless walk with a
   3% stop and a 20% target stops first ~87% of the time, which is the observed
   15/18. The entries are not
   beating that null model after fees.
2. **Trail/stop mismatch (bug 1).** It converts would-be scratches into -1R
   losses. Fixable without touching signal generation.
3. **The stop sits inside daily noise.** Median effective risk is **0.47×
   ATR14**; **10/18** are below 0.5× ATR; 13/18 exit on the entry bar. But
   *widening the stop alone is not the fix* (see counterfactual): the loss per
   stop scales with the stop while the win rate barely changes.
4. **Targets are too far.** Targets are 10%–34% measured moves (median ~21%).
   Only RUM, FLEX and SMHI ever touched target. A 5–20% target with a 3% stop
   mechanically needs a high win rate, which the entries do not supply.
5. **The short side is broken in this sample.** `double_top` is **0/8,
   -$1,844.36 (-8R)**; all shorts combined are **0/10, -$2,346.19**. The DII
   gate requires *bearish 16–40* for a SELL, i.e. bulls 60–84 — it systematically
   shorts names in strong uptrends. That is the worst place to short and the
   data says so.
6. **The DII gate is now the only filter.** It rejected **44 of 63 unique
   signals (~70%)**; every other gate (min price, confidence, regime, cooldown,
   risk) is a no-op stub in the 2026-09 refactor (`core/scanner.py:47-84`).
   There is no A/B evidence the gate adds expectancy, and its two bands are
   mirror images that leave the balanced 40–60 zone untraded
   (`analysis/pattern_dii.py:14-17`).
7. **Sub-$10 microcaps are back in the book** (6/18: STKS, EMPD, MYSE, RUM,
   SMHI, ANIX). They have the tightest effective risks and the largest daily
   ranges. The declared $20M ADV floor (`core/market.py:28`) is only applied at
   universe fetch time; names can degrade into the replay, and no per-signal
   price/liquidity floor is enforced.
8. **Fees are material.** 0.2% round trip = ~$20/trade. Against ~$300 average
   risk that is ~0.07R per trade; over 18 trades it is ~1.2R, or 26% of the
   realized loss. At a 3% stop the signal has to clear costs before it is even
   at break-even.

## Counterfactual (exit-only, frozen tapes, repo's own `_check_exit`)

Entries and quantities held fixed, costs 0.1% each side. Baseline reproduces
the export except NBR (the export closed it on Kronos, the replay uses the
stop). Treat as directional, not proof.

| Scenario | Closed | P&L | Wins |
|---|---:|---:|---:|
| Export (trail 11–16%) | 18 | **-$1,465*** | 2 |
| Trail = 2× effective risk (arms at 2R, gives back 2R) | 18 | -$1,148 | 4 |
| Trail = 1× effective risk | 18 | -$82 | 5 |
| Trail = 0.5× effective risk | 18 | +$411 | 5 |
| ATR stop `clamp(1.5*ATR,3%,8%)` + 2R-of-ATR trail | 17 | **-$8,266** | 3 |
| ATR stop, no trail | 17 | -$9,244 | 2 |

\* Replay baseline; the export itself is -$1,405.17 (NBR difference).

Two things this shows:

- **Fixing the trail scale is worth ~$300 conservatively** (the 2R case, which is
  what `signals.py` says it intends). Tighter trails look better on this sample,
  but that is curve-fitting 18 trades.
- **Widening the stop in isolation is much worse** (-$8.3k). With a wider stop
  the losers lose 2–3× more and 14/17 still hit the stop. The binding problem is
  the entry/target, not the stop width.

## Recommended changes (ship in this order)

1. **Make the trail a multiple of the executed stop, not the ATR stop.**
   Either recompute `trailing_stop_pct` / `trailing_activation_pct` inside
   `_apply_fixed_stop` (e.g. `2 * 0.03`), or, better, express the trail in R at
   fill time in `_effective_stop`. Ship 2R first (matches the documented intent
   and is the conservative counterfactual); test 1R only on an independent
   period.
2. **Add a minimum-risk / gap guard at the fill.** In `opening_candidate`,
   reject the entry when `|fill - stop| / fill` is below a floor (e.g.
   `max(0.75%, 0.25*ATR14)`), or re-anchor the flat stop to the fill so the
   trade always risks the configured 3%. This kills the FIGR/STKS/OXM/MYSE
   class of instant-stop trades. Re-anchoring changes the level, so log it.
3. **Cap/scale the target.** The measured move is 10–34% and is rarely reached.
   Take partial profit at 1R–2R of the *effective* stop, or cap the target at
   ~3R, and let the R-based trail handle the rest. Do not keep a 20% target
   against a 3% stop.
4. **Turn risk sizing on** (`risk_pct > 0`, currently 0 in
   `core/engine_defaults.py:32`). Flat $10k notional means dollar risk ranges
   from $3 (FIGR) to $300 (EMPD) on the same "3%" rule. Risk a fixed dollar
   amount per trade.
5. **Stop trading shorts, or isolate them.** 0/10 across all short patterns;
   `double_top` 0/8. At minimum exclude `double_top` while the DII regime is
   bullish, and paper short signals in a separate book until they show edge.
6. **Shadow the DII gate.** Run it in log-only mode alongside an ungated book
   for a fixed window and compare forward outcomes. It currently removes ~70%
   of signals with no measured benefit, and its short band is actively harmful
   in this sample.
7. **Restore a price/liquidity floor for the paper universe.** Sub-$10 names
   and low dollar volume are where a percentage stop is meaningless. Enforce it
   per signal, not only at screener fetch.
8. **Do not tune on these 18 trades.** Every number above has a bootstrap
   interval that straddles zero. Re-run the whole pipeline on an independent
   period (not 2025-11..2026-10) with ≥100 trades before changing a threshold
   again. A parameter sweep on 18 trades will produce a good-looking, false
   answer — which is how the 5% cap -> ATR stop -> fixed-3% override sequence
   already happened.

## What not to do

- Do not widen the stop in isolation (measured: -$8.3k).
- Do not conclude `double_bottom` has an edge from +$941 on 2 winners.
- Do not treat the two target hits as validation of the target distance.
- Do not read the 11.1% win rate as "unlucky" — it is below the 16.3%
  break-even the payoff requires, and fees widen that gap.

## Caveats

- 18 trades, 2 wins. Every cohort is too small for inference; the pattern and
  side splits are descriptive only.
- The post-stop excursion analysis uses daily bars; intrabar ordering within the
  bar that hit the stop is unknowable, so "MFE before the stop" is approximate.
- The counterfactuals hold entries fixed (exit-only); changed exits would alter
  cooldowns, capacity and later entries.
- The frozen tapes are a local input, not a portable CI fixture.
- PH has no trades — no conclusion about PSE.

## Reproduce

```sh
# the export and its aggregates
.venv/bin/python - <<'PY'
import json; b=json.load(open('pt.json'))['books'][0]
print(b['metrics'])
PY
```

The per-trade excursion and counterfactual scripts used for this note were
throwaway analyses over `data/barcache/stream_tapes/us` plus the repo's
`core.backtester._check_exit`; they were not committed.

Note: `scripts/replay_pt_exits.py` is currently stale for this export — it
requires exactly one frozen tape per symbol (symbols now have 2–10) and it
hard-codes the old 5%/5% trail. It fails before replaying. Fix it before using
it as the regression harness for any of the changes above.

## Implementation status (2026-11-03)

Re-checked against the current committed ledger (`pt.json`,
`2026-11-03T02:18:56Z`, 17 closed):

| Metric | Value |
|---|---:|
| Net P&L | **-$2,326.09** |
| Win rate / profit factor | 23.5% / 0.667 |
| Longs | -$3,373.21 (`double_bottom` 2/8) |
| Shorts | **+$1,047.12** (`double_top` 2/8, +$1,233.48) |
| Exits | 11 stop_loss, 4 kronos_exit, 2 take_profit |
| Largest loss | EFOR -$4,041.28 (real -40% gap: 40.43 close → 24.30 open) |
| Trail vs executed risk | 8–16% vs 0.7–3% → **0 trailing exits** |

| # | Recommendation | Disposition |
|---|---|---|
| 1 | Trail in R of the executed stop | **Shipped.** `STOP_LOSS_TRAIL_R` / `STOP_LOSS_ARM_R` (2/2) set in `_apply_fixed_stop`, then re-derived from the effective fill risk in `opening_candidate`. Restores the documented invariant; P&L effect on this ledger is mixed because a 2R trail cuts target-bound winners. |
| 2 | Gap / minimum-risk guard | **Shipped.** `STOP_LOSS_MIN_RISK_FRAC=0.5` rejects a fill left with <1.5% of the planned 3% risk. Removes OXM (-$129.84) and KOS (-$93.70) from the current ledger. |
| 3 | Cap/scale the target | **Knob only, off.** `TARGET_MAX_R=0`. The current winners are 5.2R (RUM) and 12.4R (UPXI); a 3R cap would have cut them. |
| 4 | Turn risk sizing on | **Env-gated, off.** `TRADING_RISK_PCT=0`. EFOR shows sizing to the stop does not cap an overnight-gap loss and can enlarge notional; decide after event-risk handling. |
| 5 | Stop shorts / exclude double_top | **Withdrawn.** Shorts are the profitable side now; `SHORT_ENTRIES_ENABLED=true`. The kill-switch remains for a long-only paper book. |
| 6 | Shadow the DII gate | **Shipped.** `DII_GATE_SHADOW=false`; when on, the rejection is logged with status `shadow` and the entry continues. |
| 7 | Price/liquidity floor | **Knob only, off.** `MIN_SIGNAL_PRICE_USD=0`. The sub-$10 cohort is +$2,989 and holds UPXI and RUM; a $10 floor would remove the winners. |
| 8 | Independent-period validation | **Still required.** The ledger flip is the proof: every side/pattern conclusion changed within one replay. Freeze the ledger before the next tuning pass. |

### Gap risk is now the headline, not stop width

EFOR closed 40.43 and opened 24.30 the next session (10.0M shares vs ~0.55M);
the 3% stop could only fill at the gap. That one trade is **174% of the book's
net loss**. A percentage stop cannot protect against this, and widening or
tightening it changes nothing. The relevant controls are event/earnings
avoidance and a per-trade notional or gap cap — neither was in the original
note.
