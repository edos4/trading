# Breakout + Retest — Draft Conditions

**Status:** DRAFT — not backtested, not locked. Rules transcribed from a
whiteboard trading explainer (concept video: "How to trade breakouts"), not
derived from a backtest run on this codebase's universe/data. Treat the
thresholds below as reasonable starting points, not tuned values.

**Direction:** Long only. The source video only illustrates the bullish case
(range → breakout above resistance → retest holds → continuation), plus one
failed/invalidated example (breakout pokes above resistance, then price closes
back down through it — marked with an X, not traded). No bearish/breakdown
case was shown, so this draft does not include a short side.

## What the video shows

1. Price chops sideways inside a horizontal range, bounded by a resistance
   line above and a support line below, touching each line multiple times.
2. Price closes above resistance (the breakout).
3. Price pulls back ("retest") down toward the old resistance line, without
   closing back below it.
4. A bullish confirmation candle closes back above the retest bar's high —
   this is the entry, marked with a dot in the video.
5. Counter-example: a breakout that pokes above resistance and then closes
   back down through it (retest fails to hold) is crossed out with an X — not
   a valid trade. Comments on the post echo this: wait for the retest/pullback
   to hold, wait for a confirming (engulfing) candle, don't chase the initial
   breakout print.

## Proposed conditions (R1–R10)

| # | Condition |
|---|---|
| R1 | Range window: look back up to 90 bars before the breakout bar for the consolidation. |
| R2 | Range must span ≥10 bars from first touch to breakout (not a 2–3 bar blip). |
| R3 | Resistance = the range window's highest high; needs ≥2 swing-high touches within 1.5% of that level. |
| R4 | Support = the range window's lowest low; needs ≥2 swing-low touches within 1.5% of that level. |
| R5 | Range tightness: (resistance − support) / resistance ≤ 15% — a real consolidation, not a wide trending swing. |
| R6 | Breakout bar: first close above resistance — no earlier bar in the range window closed above it. |
| R7 | Retest window: within 8 bars of the breakout, the lowest low of the post-breakout bars comes within 2% above resistance (price actually comes back to test the level, doesn't just run away). |
| R8 | Hold: every close from the breakout bar through the confirmation bar stays ≥ resistance × 0.99 — this is what the X'd-out counter-example violates. |
| R9 | Confirmation bar (within 5 bars of the retest low): bullish (close > open), closes above the retest bar's high, and closes above resistance. |
| R10 | Entry at the confirmation bar's close (LONG). |

## Proposed exit

- Stop: just below the retest low (retest low × 0.99).
- Target: measured move — entry + (resistance − support), i.e. the range
  height projected above the breakout.
- Trailing stop: 3% below the highest close since entry, activating after a
  4% gain — lets a strong continuation run past the measured-move target
  instead of capping it.

## Open questions for backtesting

- Whether to require breakout/retest volume confirmation — the video never
  shows volume, so none is required here; untested whether adding a volume
  filter helps or just cuts sample size (same trade-off seen in flag_pattern.md).
- Whether 15% range height and 2%/1% retest/hold tolerances are the right
  bands for this codebase's universe, or need re-tuning per Bulkowski-style
  empirical testing like the other locked patterns.
- Whether a short (support breakdown + retest-from-below) mirror is worth
  adding — not shown in the source video, so left out for now.
