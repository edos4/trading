# Paper-trade post-mortem — `pt.json` (exported 2026-11-03T19:18Z)

Scope: the **US book only**. The PH book is empty (`settings: {}`, `scan_stats: null`,
`closed_count: 0`) — it was never run, so nothing in this file applies to PSE yet.
All figures below are USD, from `books[0]` of the export.

---

## Verdict

The book did not "lose". It made +$870 (+0.87%) over 259 simulated sessions on a
$100,000 account, and that result is **inside the noise band of its own fill model**.

- 14 closed trades, 4W/10L, 28.6% win rate, PF 1.29, expectancy **+0.62% / +0.24R per trade**.
- Remove one trade (`HOLO`, +$1,575) and the book is **−$705**.
- Block bootstrap over the 14 trade P&Ls: **P(PF > 1) = 0.63**, 95% PF interval **[0.29, 3.23]**.
- Assume stop-outs fill **1% worse** than modelled (still generous for $0.20–$4 names):
  total becomes **−$129**, PF **0.97**. At 2% worse: −$1,128, PF 0.78.

So: there is no demonstrated edge to tune. What follows is therefore split into
(a) mechanisms that are **wrong by construction** and should be fixed regardless of the
sample, and (b) hypotheses that can only be settled by a paired re-run, not by more
parameter fitting on 14 trades.

---

## Where the money actually went

| Block | $ |
|---|---|
| Gross win, 4 winners | +3,917 |
| — of which 3 exact target hits | +3,479 |
| — of which 1 trailing stop (CDNS) | +438 |
| Gross loss, 10 losers | −3,047 |
| Fees (already netted in the above) | −282 = 24.5% of gross |
| **Net** | **+870** |

Every one of the 10 losses is between **−3.197% and −3.203%** of notional, i.e. exactly
−1R. That is not a market outcome, it is a configuration: `stop_loss_pct = 0.03`
(`config.py:74`) replaces each pattern's own stop with a bot-wide 3% band, and every
entry is sized at a fixed $10,000 notional, so the loss per trade is a constant $300 + fees.

**7 of 14 trades were stopped on the first session they were held.** This book did not
lose to a trend or a regime; it lost at the stop, immediately, over and over.

---

## Findings

### F1 — Critical — the fixed 3% stop destroys the pattern's invalidation level

`MarketScanner._apply_fixed_stop` (`core/scanner.py:1461`) overwrites `signal.stop_loss`
with `signal_close × (1 ∓ 0.03)` for every signal before it is logged, and the dual-stop
resolver (`core/execution.py:43-54`, `core/backtester.py:175-184`) always takes the
*nearest* of the structural stop and `entry × (1 ∓ cap)`. Consequence: the executed stop
is **never wider than 3% from the fill**, and the pattern's own failure level is discarded.

Evidence in the export:

- All 14 `requested_rules.stop_loss` values are exactly `signal_close × 0.97` for BUYs and
  `× 1.03` for SELLs (±0.0001). The pattern's real stop is **not recoverable from the
  export at all** — the override happens before `requested_rules` is stamped.
- `NEXA` is the clean example. The signal bar closed at 13.31; the entry filled at 12.60
  (a 5.33% adverse gap) and the executed stop was 12.978 — **2.5% below the price the
  pattern had just broken down from**, i.e. inside the range the signal bar itself
  travelled. A plain retest of the breakdown level killed it on bar 1. Wherever the
  pattern's own invalidation level was, the clamp put the stop strictly closer than it.
- `patterns_v2/signals.py:242-243` states the design assumption outright: *"A confirmed
  reversal closes beyond the neckline, so its full-depth risk necessarily exceeds its
  measured height."* A `double_bottom` entered above the neckline therefore has a natural
  **R:R ≤ 1:1**. The 3% stop is what turns that sub-1R geometry into the multi-R targets
  in this export. Shrinking the stop does not shorten the distance price must travel — it
  only guarantees the first pullback removes you, while the win still requires the
  *entire* measured move.

**Change (ship first):** stop conflating "cap the dollar risk" with "put the stop at 3%".

1. `STOP_LOSS_PCT=0` — let `_levels()` supply the structural stop.
2. `ENGINE.risk_pct > 0` (e.g. `0.003`) so `_apply_risk_sizing` sizes to that stop. A 6%
   stop simply gets half the shares; the dollar risk stays constant and the invalidation
   level stays honest. This is the one change that fixes the loss pattern without touching
   the winners.
3. If a pattern-stop ceiling is still wanted, reject entries whose structural distance is
   absurd (e.g. > 8%) rather than tightening the stop.

If the operator insists on keeping `STOP_LOSS_PCT=0.03`, then the target must be capped in
the same units (`TARGET_MAX_R`, currently `0.0` = off, `config.py:86`), otherwise the trade
is required to complete a 3–5R measured move while being fenced in at 1R of noise.

### F2 — Critical — the trailing stop is arithmetically incapable of banking profit

`STOP_LOSS_ARM_R = 2.0` and `STOP_LOSS_TRAIL_R = 2.0` (`config.py:79-80`). The trail arms
at +2R and gives back 2R from the peak, so its **best possible** exit is ~0R; it can never
lock in a gain. It is a breakeven stop wearing a trailing stop's name.

- `CDNS` peaked at **+3.35R** and exited at **+1.55R** — exactly `268.50 × 1.06 = 284.61`.
  A 6% giveback on a 3% stop is a 2R giveback.
- `CMPS` reached **+3.74%** (≈1.25R) and `ANIX` **+3.37%** (≈1.4R); neither armed the trail
  (arm is 6%), both round-tripped to a full −1R.

**Change:** arm at 1R with a 0.5R trail (`STOP_LOSS_ARM_R=1.0`, `STOP_LOSS_TRAIL_R=0.5`),
and/or take a partial at +1R. This needs no sample to justify — the current pair is a
one-way giveback by construction. Expect it to *reduce* average winner size; the point is
to convert the two round-tripped 1R+ winners into small positives.

### F3 — High — "fill at the next open" systematically enters at a worse price

Every fill is the next observed open (`resolved_rules.entry_mode = "next-observed-open"`),
and the gap is adverse in 10 of 14 trades (2 flat, 2 favourable): median **+1.04%**, mean
**+1.25%**, worst `NEXA` **+5.33%**. Against a 3% stop that is a third of the risk budget
thrown away before the trade starts, and it moves the clamped stop *inside the range the
signal bar just travelled*.

- 5 of the 7 first-session stop-outs had an adverse entry gap ≥ 1.17%.
- `stop_loss_min_risk_frac = 0.5` (`config.py:83`) only rejects fills that gap *through*
  the stop (it caught `IE`, `FIGR`, `HYMC`). It does **not** reject a 5.33% adverse gap,
  because it compares risk-after-gap to planned risk and both are ~3%.

**Change:** rejection rule on adverse gap as a fraction of the planned stop distance, e.g.
reject when the next open is worse than the signal close by more than 0.5 × `stop_pct`;
or enter with a limit at/inside the signal close and let it go unfilled. Cheap, and
directly targets the failure mode with the cleanest evidence in the book.

### F4 — High — the entire result rests on sub-$10 names with no liquidity gate

`volume_gate: false`, `min_signal_price_usd: 0.0` (`config.py:90`), fixed $10k notional.

- 9 of 14 trades under $10, 2 under $2. `AUMN` is a short of **49,975 shares at $0.2001**;
  its 3% stop is **0.6¢**, roughly one tick. `HOLO` (5,235 sh @ $1.91) and `ABCL`
  (2,538 sh @ $3.94) are the two biggest winners — i.e. **all of the profit is booked in the
  names where the fill model is least trustworthy**.
- These are exactly the names where a stop fills worse than the stop price, which is the
  assumption that turns PF 1.29 into 0.97 (see Verdict).

**Change:** add a 20-day dollar-volume floor (e.g. ≥ $1–2M) and a price floor (≥ $1;
`min_signal_price_usd` already exists) as *screening* gates, then re-run. If the edge
disappears, it was an artifact of the fill model and this book was never viable.

### F5 — High — 6 of 10 stop-loss rows are internally impossible in the ledger

The exported `stop` is the requested stop (`signal_close ∓ 3%`); the trade actually exits at
the *nearer* of that and `fill ∓ 3%`, which after a gap is a different price. The export
shows both without explaining the difference:

| trade | shown stop | actual exit | impossible? |
|---|---|---|---|
| LEVI BUY | 21.6213 | 21.6698 | long exited **above** its stop |
| XNET BUY | 7.4205 | 7.6339 | long exited **above** its stop |
| NRXP BUY | 2.0176 | 2.0661 | long exited **above** its stop |
| NVAX BUY | 9.0792 | 9.3799 | long exited **above** its stop |
| URBN SELL | 73.6656 | 72.8004 | short exited **below** its stop |
| NEXA SELL | 13.7093 | 12.978 | short exited **below** its stop |

Anyone auditing the ledger concludes the engine is broken. It is not — the dual-stop model
is simply invisible. This also means the export cannot answer "was the clamp justified?"
because `pattern_stop` is overwritten before it is saved.

**Change:** stamp `pattern_stop` (pre-override) into `requested_rules`/`rule_evidence`, and
emit the *executed* stop into `closed_trades[].stop` and `daily_marks[].stop`. Reporting
only; no behaviour change.

### F6 — Medium — the DII gate removes 67% of signals and preferentially kills the best-looking ones

`analysis/pattern_dii.py` accepts BUY only when bulls ∈ [16, 40] and SELL only when
bears ∈ [16, 40]. Since bulls + bears = 100, this demands a *counter-trend* tape and
rejects the entire balanced 40–60 zone in both directions.

- 35 of 52 unique signals died on this gate. 71% were "too extended" (>40%), 29% were
  "too counter-trend" (<16%).
- It rejected 6 of the 9 high-confidence (≥0.8) signals: `FGBI` 0.92, `SF` 1.00,
  `ILMN` 1.00, `SCCO` 1.00, `TRIN` 1.00, and `IE` 0.85. The survivors are mostly
  0.22–0.35-confidence `double_bottom/top` signals. The confidence scales are not
  comparable across detectors (v2 reversals 0.22–0.59, v2 wedges/triangles 0.85–1.00), so
  this is not a like-for-like comparison — but it is the gate's actual selection effect.
- There is **no counterfactual in this export**: `dii_gate_shadow` is false and there are
  zero `status="shadow"` rows.

**Change:** do not hand-tune the band on n=14. Set `DII_GATE_SHADOW=true`
(`config.py:97`; the code path already exists at `core/scanner.py:1562`) and replay the same
259 days to get the ungated book. If a change is wanted sooner, the defensible one is
dropping the *lower* bound only (accept 0–40), because rejecting a 0%-bullish double-bottom
breakout is arbitrary — it rejects the most washed-out setups.

### F7 — Medium — the short side is 1W/4L and all four `double_top` shorts were dead on arrival

| side | n | wins | $ |
|---|---|---|---|
| BUY | 9 | 3 | +1,621 |
| SELL | 5 | 1 | −751 |

All four `double_top` shorts (`URBN`, `NEXA`, `NXDT`, `AUMN`) had a first-bar close at or
below −2.3%, i.e. they never traded in their favour at all. Do **not** disable shorts on
this — 4 trades is nothing, and the `config.py:91-94` note cites an older ledger at 2/8
(also weak). The honest read is "no evidence either way". Fix F3 (gap) first; if the
instant-loss signature survives, then a short-specific entry rule (require the fill to
remain below the neckline) is the next experiment, not a blanket ban.

### F8 — Low — the export contradicts itself on entry semantics

`signal_logs[].reason` says *"Filled signal-bar entry"* while `resolved_rules.entry_mode`
says `next-observed-open` — and the fill really is the next open (`LEVI`: signal close
22.29, fill 22.34). The Paper Logs tab is the operator's audit surface; it should say
exactly one thing.

Also `summary` prints `Avg hold: 0.0d` while `metrics.avg_hold_bars = 4.2` and one trade
(`GOOD`) was held 28 bars, and `metrics.total_pnl_pct = 0.87015` is a percentage under a
name that implies a fraction.

### F9 — Low, but investigate — the funnel implies the replay is not seeing 2,000 names

52 unique signals over 259 sessions with `n_symbols: 2000` is ~1 signal per 10,000
symbol-days. That is implausibly selective for daily `double_bottom`/`double_top`
detection; either the historical store covers far fewer names than the header claims, or
`latest_bar_only` plus confirmation drops almost everything. Until that is measured, the
14 trades may not be a sample of the stated universe. Add a counter of symbols evaluated
and signals-by-pattern per session to `scan_stats` and check it on the next replay.

---

## The only defensible next step

Do **not** tune any threshold on this sample. Run a **paired** replay — same 259 days,
same symbols — one knob per book, and compare on R and PF *together with trade count* so a
filter cannot "win" by collapsing to zero trades:

| book | change | hypothesis |
|---|---|---|
| A | `STOP_LOSS_PCT=0` + `ENGINE.risk_pct=0.003` | structural stop restores the loss profile (F1) |
| B | A + `STOP_LOSS_ARM_R=1.0`, `STOP_LOSS_TRAIL_R=0.5` | trail banks some of the 1R+ runners (F2) |
| C | A + adverse-gap rejection at 0.5 × stop | instant stop-outs disappear (F3) |
| D | C + ADV ≥ $1M and price ≥ $1 | edge survives realistic names (F4) |
| E | A + `DII_GATE_SHADOW=true` | gate adds or subtracts (F6) |
| F | stop-fill slippage 1% | is any of it real (Verdict) |

A, B, C are justified by mechanism and need no sample. D, E, F are the actual questions.

## What not to change

- Do not disable shorts on 0/4 (F7).
- Do not retune the DII band by hand (F6).
- Do not add patterns or loosen detectors hoping for more signals — the funnel is already
  the binding constraint and 14 trades cannot distinguish any threshold.
- Do not read the `by pattern` block in `summary` as an edge: `double_bottom n=7` and
  `double_top n=4` are single-digit samples; `symmetrical_triangle n=1` is one trade.

---

## Appendix — the 14 trades

MFE/MAE are max favourable / adverse excursion from `daily_marks` (%). `exit − stop` is the
discrepancy flagged in F5; `R` is gross of fees (fees = 0.94R total). `CDNS` is the one
trailing exit, so its `exit − stop` is expected, not an inconsistency.

| # | symbol | side | pattern | entry | exit | stop shown | exit−stop | R | bars | MFE% | MAE% | reason |
|---|--------|------|---------|-------|------|-----------|-----------|---|------|------|------|--------|
| 1 | LEVI | BUY | double_bottom | 22.34 | 21.67 | 21.62 | +0.0485 | −1.00 | 5 | +0.45 | −3.20 | stop_loss |
| 2 | CMPS | BUY | descending_channel | 6.69 | 6.489 | 6.489 | 0 | −1.00 | 4 | +3.74 | −3.20 | stop_loss |
| 3 | XNET | BUY | double_bottom | 7.87 | 7.634 | 7.42 | +0.2134 | −1.00 | 1 | −3.20 | −3.20 | stop_loss |
| 4 | ABCL | BUY | double_bottom | 3.94 | 4.40 | 3.783 | +0.617 | +3.89 | 3 | +11.46 | +4.31 | take_profit |
| 5 | URBN | SELL | double_top | 70.68 | 72.80 | 73.67 | −0.8652 | −1.00 | 1 | −3.20 | −3.20 | stop_loss |
| 6 | CDNS | SELL | symmetrical_triangle | 298.46 | 284.61 | 311.75 | −27.14 | +1.55 | 6 | +10.04 | +0.70 | trailing_stop |
| 7 | GOOD | BUY | descending_channel | 11.16 | 12.03 | 10.81 | +1.223 | +2.60 | 28 | +7.58 | +0.36 | take_profit |
| 8 | NRXP | BUY | double_bottom | 2.13 | 2.066 | 2.018 | +0.0485 | −1.00 | 1 | −3.20 | −3.20 | stop_loss |
| 9 | NVAX | BUY | double_bottom | 9.67 | 9.38 | 9.079 | +0.3007 | −1.00 | 1 | −3.20 | −3.20 | stop_loss |
| 10 | NEXA | SELL | double_top | 12.60 | 12.98 | 13.71 | −0.7313 | −1.00 | 1 | −3.20 | −3.20 | stop_loss |
| 11 | HOLO | BUY | double_bottom | 1.91 | 2.215 | 1.833 | +0.3817 | +5.32 | 3 | +15.75 | +4.19 | take_profit |
| 12 | NXDT | SELL | double_top | 4.55 | 4.645 | 4.645 | 0 | −1.00 | 1 | −2.30 | −2.30 | stop_loss |
| 13 | AUMN | SELL | double_top | 0.20 | 0.2061 | 0.2061 | 0 | −1.00 | 1 | −3.20 | −3.20 | stop_loss |
| 14 | ANIX | BUY | double_bottom | 3.26 | 3.182 | 3.182 | 0 | −1.00 | 3 | +3.37 | −2.60 | stop_loss |

R totals: 4 wins avg **+3.34R**, 10 losses **−1.00R** each, net **+3.36R** over 14 =
**+0.24R/trade**. Breakeven win rate at a +3.34R average winner: **23.0%**; realised
**28.6%**. Gross of fees, before any fill-realism haircut.
