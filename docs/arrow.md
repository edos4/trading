# Arrow

Arrow is the name of the staged direction-classifier pair: **Arrow 1D** predicts
the next trading session, and **Arrow 3D** predicts three trading sessions ahead.
Both return **up**, **down**, or **flat**. They run on CPU in the application
`.venv`; no GPU is needed.

The existing trained pair is available through `runs/arrow`. This is a friendly
alias for the original experiment directory. `arrow.json` records its name and
version, tied to the locked selection hash. Naming does not alter either
checkpoint or its original training/evaluation provenance.

The `/kronos` page also uses Arrow in [Experiment 3](arrow-experiment3.md),
with frozen forecasts and automatic collection of the two target closes.

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

## Validation status

The current Arrow pair remains experimental. Its reserved final test is
**November 7–December 4, 2026**, with exclusive end December 5. Prediction does
not consume or run that final evaluation. `info` reports `final_test_pending`
until the locked evaluation is performed. Arrow is available for manual
prediction; it is not connected to automatic paper/live trade decisions.

Training, walk-forward comparisons, fixed exclusions, and final-test commands
are described in [the training documentation](kronos-direction.md).
