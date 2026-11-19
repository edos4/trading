# Arrow

Arrow is the name of the staged direction-classifier pair: **Arrow 1D** predicts
the next trading session, and **Arrow 3D** predicts three trading sessions ahead.
Both return **up**, **down**, or **flat**. They run on CPU in the application
`.venv`; no GPU is needed.

The current pair is available through `runs/arrow`. This is a friendly
alias for its versioned experiment directory. `arrow.json` records its name and
version, tied to the locked selection hash. Naming does not alter either
checkpoint or its original training/evaluation provenance.

## Predict a direction

From the repository root:

```bash
# US: next trading session
.venv/bin/python scripts/arrow.py predict --symbol AAPL --market us --horizon 1

# Philippine stock: three trading sessions ahead
.venv/bin/python scripts/arrow.py predict --symbol BDO --market ph --horizon 3

# Use a specific completed historical anchor
.venv/bin/python scripts/arrow.py predict --symbol AAPL --market us --horizon 1 --as-of 2026-11-11

# Show the model version and validation status
.venv/bin/python scripts/arrow.py info
```

By default, predictions read daily bars from the existing PostgreSQL history
configuration. They need 400 completed bars. Open/future bars are excluded;
stale or insufficient history causes an explicit error. An anchor whose next
target overlaps training/selection history is rejected.

The JSON result includes the symbol, anchor date, horizon, direction, and the
up/down/flat model probabilities. These probabilities are not calibrated
historical success rates. If a context fails Arrow's liquidity, stale-price,
discontinuity, or suspension rules, the result has `abstained: true` and names
the reasons instead of issuing a direction.

The November 13 revision improves US **one-day Flat decisions**. It fits a
bounded adjustment to the Flat score separately for 60-session average dollar
turnover below $250 million, $250 million–$1 billion, and at least $1 billion.
The offsets are selected using pre-November-7 validation macro-F1, with minimum
coverage and recall for every class. It can still predict Up and Down; its
relative evidence between those two directions is preserved. The ATR definition
of Flat, Philippine forecasts and three-day forecasts keep their existing rules.
Adjusted probabilities are normalized decision scores, not calibrated chances
of success. Selection and the calibration report are hash-locked together.

The original version remains loadable with
`--model-dir runs/kronos-direction-2026-11-12`. The revision applies to future
Arrow calls; it does not rescore a past forecast as though it had predicted Flat
originally.

To use a local CSV instead of PostgreSQL:

```bash
.venv/bin/python scripts/arrow.py predict --symbol AAPL --market us --horizon 1 --csv daily.csv
```

The CSV needs `date,open,high,low,close,volume` columns. The same completion,
history, and quality checks apply. Supply the correct market-session dates.

## Register another trained pair

After completing the [direction-training workflow](kronos-direction.md):

```bash
.venv/bin/python scripts/arrow.py register --model-dir runs/my-direction-experiment
```

This names the pair Arrow and updates `runs/arrow` atomically. Existing regular
files/directories are never replaced by the alias. Registration and inference
verify the selection, model hashes, feature implementation and quality policy.
Use `--model-dir /path/to/bundle` on `info` or `predict` to inspect a specific
registered pair without changing the default alias.

## Reproduce the Flat revision

```bash
.venv/bin/python scripts/arrow_flat.py \
  --dataset runs/kronos-flat-recalculation-2026-11-12/snapshot \
  --parent-model runs/kronos-direction-2026-11-12 \
  --output runs/another-arrow-flat-revision \
  --feedback-through 2026-11-12 --forward-start 2026-11-13 --activate
```

Use an empty output directory and reserve dates that are still in the future
when running this command. Omit `--activate` to leave the `runs/arrow` alias
unchanged. The original all-history corpus and the AXRX/GASE/GBDMF exclusions
remain in force. Insufficient coverage, a collapsed class, or lower validation
macro-F1 rejects the revision. A natural-frequency retraining trial was rejected
because it lost too much Up/Down recall.

## Validation status

The current Arrow pair remains experimental. The revision's fresh forward test
is **November 13–December 10, 2026**, with exclusive end December 11. November 12
has already supplied feedback and is excluded from that test. Earlier validation
is reused selection evidence, not a new accuracy result. The original version's
reserved dates remain November 7–December 4.

Prediction does not consume or run the final evaluation. `info` reports `final_test_pending`
until the locked evaluation is performed. Arrow is available for manual
prediction; it is not connected to automatic paper/live trade decisions.

The existing `scripts/kronos_direction.py --evaluate-final` command recognizes
revised bundles and evaluates their complete fresh period with the adjusted
model. It verifies the locked historical prefix, permits later appended bars,
and rejects changed checkpoints, calibration code, policy or report. A report
can be written only once.

Training, walk-forward comparisons, fixed exclusions, and final-test commands
are described in [the training documentation](kronos-direction.md).
