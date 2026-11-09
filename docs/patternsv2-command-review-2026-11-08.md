# Patternsv2 command trace and detector repair — 8 November 2026

```sh
VOLUME_GATE_ENABLED=false .venv/bin/python main.py --paper --patterns-v2 \
  --paper-reset --market us --symbols=2000 --stream=2025-11-03 \
  --kronos-gate-mode confirm --export-trades-log=pt.json
```

## What the command actually selects

`main.py` parses the date, selects the US paper account, resets that account
and its signal journal, and preserves an existing export as a numbered
backup. It requests 2,000 names from the cached present-day US screener. The
available cached list contains 1,931 names; the request is a limit, not a
promise of 2,000 usable histories. Historical membership is not reconstructed.

The historical stream advances once per completed universe scan. The scanner
uses completed daily candles and only consumes breakouts on the current bar.
Patternsv2 requires at least 60 bars, calculates ATR and causal ZigZag pivots,
then converts raw breakouts into the bot's structural stops and measured
targets. A signal is a decision, not a fill: orders execute at the next
observed bar's open after the execution checks.

`--patterns-v2` selects the family but respects its Settings mute list. The
current configuration mutes `double_bottom`, `double_top`, and `rising_wedge`.
The package also leaves both head-and-shoulders variants disabled. Thirteen
families therefore remain enabled. The repair preserves these choices.

Volume confirmation is off. DII is still active: the current environment
selects **Filter**, whereas the saved three-trade export selects **Shadow**.
Kronos entry confirmation uses three forecasts and requires a directional
majority; sideways and split votes do not approve an entry. Kronos position
exits inherit **Exit** from Settings even though the command specifies only
the entry mode. Collect-first and vision confirmation are off in this run.
Portfolio, liquidity, opening-gap, cooldown, and stop checks still apply.

The CLI now prints DII and both Kronos modes. Scanner startup prints the
enabled and disabled Patternsv2 names, and paper exports preserve those lists.
This makes the effective configuration visible alongside the command.

## Why valid bounded patterns disappeared

Triangles, wedges, channels, and rectangles share `_triangle_base.py`. Its
original regression required every high pivot and every low pivot to lie on
its respective boundary. ZigZag also detects partial swings inside a base.
Those interior turns bent the regression away from the true rails or exceeded
its fit tolerance, causing a valid base to return no signal.

The regression fixture keeps eight boundary touches and inserts one partial
recovery/pullback inside the unchanged rails. Before the repair, six of the
eight bounded families lost their signals. All eight now detect their
respective patterns and label only the actual boundary touches.

The fit retains the existing regression when it works. Otherwise it considers
supporting edges of the upper/lower pivot convex hull, enclosing the interior
swings rather than demanding that they touch a rail. The original minimum of
five touches, with at least two per side, still applies. Candidate endpoints
must themselves be boundary touches. Width, duration, wick, containment,
participation, apex, breakout, and data-quality checks remain active.

Candidate ranking now retains a best candidate **per family**. Previously,
an earlier or longer triangle from the same pivot sequence could discard a
valid channel or wedge. Ranking remains causal: an earlier confirmation in
the same family cannot be displaced by a breakout that happens later.

No numerical detection thresholds, entry filters, exit rules, position sizes,
model checkpoints, or model-vote thresholds were selected by trading returns.

## Evidence and interpretation

The existing three-trade file is
`/home/engine-33ai/Downloads/paper_trades_all_2026-11-08T1003.json`, SHA-256
`706f3b0e089337497e568d225c39812a1da267fe33dfbb6cdad4d976c881ab70`.
It contains 250 sessions from November 10, 2025 through November 6, 2026,
466,651 detector evaluations, 26 signals, 23 Kronos rejections, and three
fills. Its DII mode is Shadow. The older workspace `pt.json` is a different
run: it has 14 closed trades and Kronos entry gating off. The two files must
not be treated as the same baseline.

A preliminary full-history comparison on the same 1,931 cached symbols found
25 enabled-pattern events before the repair and 439 after it, from November
3, 2025 onward. That diagnostic uses complete cached histories rather than
the scanner's changing replay window; it is not a portfolio or fill count.
Double-top and double-bottom counts were unchanged. Most added events came
from the repaired bounded families.

The pinned 29-symbol fixture corpus passes all 5,989 prefix comparisons:
appending future bars does not change an already-confirmed signal. The eight
new interior-swing fixtures also compare every eligible prefix and the
latest-bar optimization with the full detector. Eight negative fixtures
verify that an intervening rail pierce still invalidates the base.

The full paper rerun uses the production scanner, detector worker pool,
Kronos GPU worker, account, and export code. Its transport adapter serves
the frozen local tapes through the production stream-server snapshot/control
methods in memory. The universe cache is read without refreshing it. No
existing replay server is started, stopped, or advanced. The reset account,
signal journal, detected-pattern log, and export live under
`/tmp/trading-patternsv2-confirm-replay`.

## Completed GPU replay

The replay finished all **255 sessions**, November 3, 2025 through November
6, 2026, in about 14 minutes 19 seconds. It used the current environment's
DII **Filter** and Kronos entry **Confirm** / position **Exit**, with all
existing pattern mutes preserved. This differs from the saved three-trade
run's DII Shadow mode and November 10 start; the trade totals are not an
experiment holding every filter and date constant.

| Stage | Count |
| --- | ---: |
| Requested / returned symbols | 2,000 / 1,931 |
| Empty symbols dropped | 17 |
| Detector evaluations | 475,921 |
| Detected decisions | **451** |
| DII rejections | 365 |
| Kronos decision-stage rejections | 73 |
| Orders queued | 13 |
| Kronos execution-price rejections at the next open | 3 |
| Filled trades | **10** |
| Closed / still open at end of tape | **9 / 1** |
| Snapshot errors / invalid bars skipped | 0 / 0 |

The funnel reconciles: `365 + 73 + 3 + 10 = 451`. The 13 queued rows are
intermediate events; they are not additional detected decisions or trades.
The journal contains 464 rows, all within this run's scan window, and every
ledger fill has matching decision/trade IDs. There are no pending orders.

| Enabled family | Detected | Filled |
| --- | ---: | ---: |
| ascending_channel | 51 | 1 |
| ascending_triangle | 38 | 1 |
| bear_flag | 0 | 0 |
| bear_pennant | 0 | 0 |
| bull_flag | 1 | 0 |
| bull_pennant | 0 | 0 |
| cup_handle | 1 | 0 |
| descending_channel | 38 | 0 |
| descending_triangle | 39 | 0 |
| falling_wedge | 106 | 2 |
| inv_cup_handle | 0 | 0 |
| rectangle | 2 | 0 |
| symmetrical_triangle | 175 | 6 |

More detected patterns did **not** improve the simulated trading result:
nine closes produced **-$1,124.16** after costs, and the remaining AME short
was marked **-$95.49**, for **-$1,219.65** total marked P&L. One close won
and eight lost. Five exits used the protective stop, four the protective
trail; no position was closed by Kronos. The unchanged SHOP trade accounts
for the sole winner. No thresholds or exits were subsequently adjusted to
make these additional trades profitable.

The saved-candle review independently reproduces and verifies all nine
closes, their fees, and the remaining open mark. It reports no OHLC anomalies,
no ambiguous exits, and one gap exit. Final validation: **2,664 tests passed**,
154 skipped, 10 slow tests deselected, and five subtests passed. The refreshed
`patternsv2-audit.json` / `.md` record 29 events on the pinned fixture corpus
(previously 17), with all 5,989 prefix comparisons passing.

Local run artifacts are retained in the ignored research directory:

- [Completed trade export](../runs/patternsv2-review-2026-11-08/pt-fixed-confirm.json)
- [Funnel, settings, and validation summary](../runs/patternsv2-review-2026-11-08/summary.json)
- [Independent saved-candle review](../runs/patternsv2-review-2026-11-08/trade-review.json)
- [Recovered MCD and AMT chart examples](../runs/patternsv2-review-2026-11-08/repair-examples.png)
- [Isolated replay harness](../runs/patternsv2-review-2026-11-08/run_isolated_paper.py)

The completed export's SHA-256 is
`7b9a42e8134436ff79967e351da110e652c3328ba25d4e9b9bbc202d445a71d4`.
The original workspace `pt.json`, downloaded exports, `.env`, runtime accounts,
and existing stream server were preserved.

To reproduce the saved three-trade export's DII policy on a future run,
explicitly prepend `PAPER_DII_GATE_MODE=shadow` to the command. To include
double tops/bottoms, change their Settings toggles or explicitly override
`DISABLED_PATTERNS_V2`; `--patterns-v2` alone does not clear that list. These
are separate policy choices, not required by the detector repair.

These are historical simulations on a present-day selected universe. More
detected setups establish that the missed-pattern defect is repaired; their
count does not establish predictive value or improved profitability.
