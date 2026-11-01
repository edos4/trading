# Detection base rate: why a 2,000-symbol year yields dozens of signals

Measurements taken 2026-11-01 on the US universe
(`data/cache/symbols_america_value_2000_adv20000000.json`) and the daily
history store. Scripts: `~/.hermes/cache/scratch/detect_sweep.py`,
`cup_diag.py`.

## The funnel, as the live paper scan sees it

| Stage | Count |
|---|---|
| Symbols requested | 2,000 |
| Symbols with a snapshot | 1,633 |
| Pattern evaluations per session | ~1,550 |
| Evaluations in 50 sessions | 77,530 |
| Signals that reached the DII gate (50 sessions) | 7 |
| Signals passing DII | 0 on the last session |

Extrapolated to 260 sessions: ~36 detections, which is what the previous
window produced (69 logged rows = 56 unique, 13 filled).

## Where the loss happens

Detection, not gating. Over 150 symbols of full history the detectors emitted
32 hits, and `tradable()` rejected **0** of them. The min-share-price gate was
a no-op stub, so it was not filtering either.

`double_bottom`, measured over 40 symbols / 131,203 bars (~521 symbol-years):

| Condition | Rejected |
|---|---|
| not an alternating H,F,T,F pivot run | 6,193 |
| two lows more than 3% apart (`tol_pct`) | 3,357 |
| low-to-low width outside 10–120 bars | 1,456 |
| neck less than 10% above the lows | 603 |
| no prior high / prior high under the neck | 571 |
| remaining close, spike, retest and split checks | ~250 |
| **confirmed** | **6** (1 per 2,084 windows) |

ZigZag(2×ATR) produces **24 pivots per symbol-year**, so the whole library sees
about 24 candidate windows per symbol per year and confirms one shape per ~80
symbol-years for `double_bottom`.

## Loosening the knobs does not add edge

Forward return from the next open, sign-adjusted, 200 symbols, 15 cells:

| zz_mult | tol_pct | n | mean +5 | mean +10 | mean +20 |
|---|---|---|---|---|---|
| 1.5 | 0.03 | 14 | -2.41 | -4.90 | -9.65 |
| 2.0 | 0.02 | 20 | -1.40 | -1.13 | -4.66 |
| 2.0 | 0.03 | 30 | -1.70 | -0.55 | -1.45 |
| 2.0 | 0.04 | 28 | -0.91 | +0.61 | -1.45 |
| 2.0 | 0.06 | 21 | -0.88 | +0.13 | -4.07 |
| 2.5 | 0.02 | 31 | -1.89 | -2.25 | -4.76 |
| 2.5 | 0.05 | 34 | -1.22 | -0.55 | -3.37 |

Every cell is negative at 5 bars and no cell is reliably positive at any
horizon; the best cells rest on 28–40 trades. A stricter pivot threshold
(2.5) produced *more* signals than 2.0, which says the count is noise-dominated
rather than controlled by any one knob. Do not loosen detection expecting more
edge — the edge has to come from exits and selection.

## Two structural findings

**`cup_handle` could not fire.** It required the handle to be a fourth ZigZag
pivot, i.e. a 2×ATR swing, while capping the handle's retrace at 33% of the cup
depth. On a tight cup those two constraints cannot both hold. The handle is now
read from raw bars after the right rim (`patterns_v2/patterns/cup_handle.py`),
and `patterns_v2/test_detectors.py` feeds it a textbook cup so the regression
cannot return silently.

**`cup_handle` still does not fire on real data.** After that fix the binding
constraint is `min_w=35`: the cup must span 35+ bars from left rim to right rim,
but consecutive pivots sit ~10 bars apart, so 16,153 of 17,998 candidate pivot
triples fail on width alone. A 7-week cup needs a coarser rim finder than the
2×ATR ZigZag. That is a rewrite, not a knob, and it should wait until the
pattern's forward edge is measured — nothing in the sweep above suggests
adding detection volume is worth money right now.

**`bull_flag`, `bull_pennant`, `rectangle`, `ascending_channel`** emitted no
hits in any sample and no exceptions. They are strict rather than provably
broken, but nothing in the suite would have noticed if they were dead: the
existing tests mock the raw detection dict and never feed real bars.
