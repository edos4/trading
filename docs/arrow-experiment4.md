# Arrow Experiment 4

The `/kronos` page's Experiment 4 displays the existing Arrow 1D and Arrow 3D
models. The reset prepared on November 12, 2026 uses the November 11 close and
the top 50 NASDAQ stocks/depositary receipts by TradingView `Value.Traded`.
ETFs are excluded. Predictions target November 12 (+1 trading session) and
November 16 (+3 trading sessions). Weekends and exchange holidays are skipped.

Arrow returns up/down/flat directions and uncalibrated model probabilities.
It does not forecast closes. The chart therefore shows observed closes only.
Each stock's actual direction is its cumulative return from the anchor compared
with the same frozen ATR band used for Arrow's three-class labels. Both horizon
reports show observed, scored, correct, incorrect, abstained and pending counts.
Accuracy is correct/scored. A missing actual or abstention cannot count as a
correct prediction. SPCX and SKHY have insufficient history for the required
400-bar context and are explicit abstentions in this reset.

The previously prepared Arrow run is moved intact into Experiment 4. The
original Kronos-base/fine-tuned Experiment 3 is restored above it, including
its original forecasts, actuals, dates, report and separate collector.

The November 13 Flat calibration is a separate Arrow revision for future calls.
This experiment keeps its original `c610c589d958` predictions and scores. Updating
the default Arrow alias does not regenerate either horizon of this saved run.
See [Arrow revisions](arrow.md) for the current model and fresh validation dates.

```bash
# One-time adoption of the existing Arrow run; no new forecasts.
.venv/bin/python scripts/kronos_experiment4.py --adopt-arrow-experiment3 --install-cron
```

Adoption renames only the former `kronos_experiment3_arrow` schema to
`kronos_experiment4` in one transaction. Model identity, inputs, predictions,
actual observations, run identifier and original generation timestamp stay
unchanged. A conflicting destination is never overwritten. Repeating adoption
after a successful move only reads the existing Experiment 4.

## Freeze or reset

Create forecasts after the anchor's completed close and before the next session
opens. The default anchor is the latest completed US session. All 50 exact
contexts, predictions, model/checkpoint hashes, ranking and timestamps are
prepared and validated before any replacement is committed:

```bash
# First start; repeats only read existing results.
.venv/bin/python scripts/kronos_experiment4.py --start --install-cron

# Explicit replacement, only before the next session opens.
.venv/bin/python scripts/kronos_experiment4.py --reset --install-cron

# Reuse a ranking captured for this same completed anchor.
.venv/bin/python scripts/kronos_experiment4.py --reset --as-of 2026-11-11 \
  --universe-file runs/arrow-experiment3-reset-2026-11-12/universe.json --install-cron
```

`--model-dir` selects a registered Arrow bundle. It must pass the normal Arrow
selection, implementation and checkpoint integrity checks. Failed preparation
leaves existing saved data intact. Before replacing an earlier Arrow experiment,
the full run, frozen contexts, forecasts and actuals are written to
`runs/experiment4-archives/<previous-run-id>.json`; `--archive-dir` overrides
the directory. Replacement is one PostgreSQL transaction in the dedicated
`kronos_experiment4` schema. Readers see a consistent old or new snapshot.
A collector finishing after a reset checks the run identifier and discards its
old fetch. The original price-model experiment remains in the separate
`kronos_experiment3` schema and its original code/universe are retained.

## Collect and inspect

```bash
.venv/bin/python scripts/kronos_experiment4.py --status
.venv/bin/python scripts/kronos_experiment4.py --collect
.venv/bin/python scripts/kronos_experiment4.py --report
.venv/bin/python scripts/kronos_experiment4.py --install-cron
.venv/bin/python scripts/kronos_experiment4.py --remove-cron
```

The marked `kronos-experiment4` cron block polls every 15 minutes. The Python
gate uses New York time regardless of the host timezone. It collects only saved
target dates after 16:30 on those dates, using existing daily bars then a Yahoo
read-only fallback. Actuals are insert-once. It never rewrites predictions or
shared bars. Collection stops when all 100 closes are present or at the next
session's 09:30 after the last target (November 17 for this reset). The cron
stores that deadline in `--stop-at` so it can remove itself even if PostgreSQL
is unavailable. Other marked jobs and environment assignments are unchanged.

Authenticated `GET /api/kronos/experiment4` reads the saved experiment;
`GET /api/kronos/experiment4/report` downloads the same direction report as
Markdown. The collector also refreshes
`logs/kronos_experiment4_direction_report.md`. Page refresh never forecasts.

Implementation: `core/arrow_experiment4.py`, the CPU worker
`scripts/arrow_experiment4_forecast.py`, the CLI `scripts/kronos_experiment4.py`,
and `web/static/arrow_experiment4.js`. Arrow's model weights and locked source
files are not changed by this integration. Its reserved final evaluation remains
pending; this forward experiment does not consume that final evaluation.

To retire collection, remove its cron first. Preserve/export the isolated
schema and run archives before removing the Experiment 4 routes, template
section and script. General Kronos forecasts use their existing models.
