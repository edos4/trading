# Patternsv2 paper exit audit — current export, 2026-10-13

`pt.json`, exported 2026-10-12 23:48 UTC, contains 201 simulated days,
**nine US closed trades and one open trade**; PH has no trades. This replaces
the earlier seven-close export discussed below.

The closed trades lost **$1,672.61**. Four winners average $229.94; five losers
average $518.48. Win rate is 44.4%, but that payoff requires about 69.3% wins
to break even. All four winners exited via trailing stops at +1.17–2.75% net;
the five losses were approximately -5.2%. No trade reached its target.
Open OTLY contributes $1,354.56 unrealized, so total book P&L is -$318.05.

## Cause and change

The export already has the previous fix: a 5% hard-stop cap and a 5% trail
activated after a 5% favorable close. Equal activation and trail widths
surrender almost the whole initial gain: a long entry at 100, closing at 105,
sets a stop at 99.75 before costs. OSCR also illustrates gap risk: its old
trail filled at 13.50 after a reversal, leaving only $274.70 profit.

New Patternsv2 signals now retain the 5% cap and 5% activation but use a
**2.5% trail**. At exactly 5% activation, the next-bar stop protects 2.375%
for a long or 2.625% for a short before costs, assuming no gap through it.
This addresses excessive giveback; it does not improve losing entries.
Existing saved positions retain their snapshotted settings.

This is a strategy-default change, not an accounting-bug fix. No threshold
sweep or pattern-specific exclusions were used. The smaller trail is a
candidate for forward validation; nine closes cannot establish an edge.

## Offline replay evidence

Frozen OHLC tapes are available in `data/barcache/stream_tapes/us`. Replaying
exported entry dates, prices, quantities and rules reproduces all nine
closed P&Ls within one cent, their dates and reasons, and OTLY remaining open.
Costs are 0.1% each side and zero slippage, matching the export.

| Exit policy | Closed trades | Realized P&L |
| --- | ---: | ---: |
| Export: 5% activation, 5% trail | 9 | -$1,672.61 |
| Delay activation to 10%, keep 5% trail | 9 | -$2,925.27 |
| Remove trail, retain hard stop/target | 9 | -$4,810.03 |
| New: 5% activation, 2.5% trail | 10 | +$893.81 |

For the **same original nine closes**, the new policy yields **-$359.81**.
The additional $1,253.63 comes from closing OTLY on October 2. Thus the
+$893.81 result must not be read as nine previously losing trades becoming
profitable, or as a comparison of realized P&L with equal open exposure.

Reproduce with:

```sh
.venv/bin/python scripts/replay_pt_exits.py
```

The script reads local files only and fails if baseline closes do not match.
It holds entries fixed: this is an exit-only counterfactual, not a new scanner
or portfolio replay. Changed exits can affect cooldowns, capacity and later
entries. Tape rows after export time are excluded. The export and tapes are
local inputs, so this experiment is not a portable CI fixture.

Regression tests cover signal defaults and both directions through paper
accounting, prior-close activation, restart persistence, gaps and structural
stops. A fresh full paper run and an independent period are still needed;
tighter trailing may cut future trends early. Historical ledgers are unchanged.

Validation: 59 tests passed across Patternsv2 signal/exit tests, paper trader,
paper books and parity suites; 18 integration tests skipped. Three socket
tests initially failed under sandbox restrictions and passed when rerun with
local socket access. `git diff --check` passed.

---

## Previous export audit (superseded figures and defaults)

# Patternsv2 paper exit audit — 2026-10-13

Source: `pt.json` (the workspace has no trading export named `tr.json`).
The export records 201 simulated days. US: seven closed trades, three open;
PH: no trades. US realized P&L is -$12,762.28, unrealized +$5,527.35,
equity $92,765.07 (-7.23%). Closed profit factor is 0.119.

All ten US trades have null trailing-stop settings, null stop percentage caps,
and no time exit. The six losing closes hit structural stops 13.68–33.93%
from entry. OSCR had a +22.00% best recorded close before losing 34.17%
after costs; the January XNET trade reached +8.24% before losing 16.52%.
Six of seven closed setups offered less than 1R at entry. This sample is too
small to establish an edge or justify pattern-specific exclusions.

Changes:

- New Patternsv2 signals retain their structural stop and target for charting,
  but execution uses the tighter of the structural stop and a 5% entry cap.
- A 5% favorable close activates a 5% trail from the best close. The close
  establishes the next bar's stop; it cannot retrospectively stop out that bar.
  The trail never loosens. A 5% long activation initially protects approximately
  entry minus 0.25%, before fees; it is not a guaranteed breakeven stop.
- Patternsv2 takes the tighter active stop, recognizes intraday target touches,
  and fills gaps at the open. If both levels are touched and the open did not
  already execute an order, the stop wins because OHLC cannot reveal sequence.
- Legacy pinned pattern exit behavior stays unchanged. Its previous runtime
  hash remains compatible. New entries snapshot the new engine hash.

The 5% values are an initial protective baseline, not an optimization result.
Gaps and costs can exceed the cap. No profitability claim follows from these
changes. The export contains daily marks (including exit fills), not complete
OHLC bars; it cannot support an exact counterfactual replay.

Run a fresh paper replay and then an independent period with costs before
judging expectancy, trade count, and stop frequency. Existing saved positions
keep their original rule fields: restarting does not install the new trailing
settings on those positions. Historical ledgers and the export were not edited.

Regression coverage includes signal wiring, both trade directions, activation
without same-bar lookahead, tighter stop selection, gaps, target touches,
ambiguous bars, monotonic trailing, persisted trailing state, and paper cash
accounting. Database-backed lifecycle and golden-tape parity tests may skip
when their fixtures are unavailable.
