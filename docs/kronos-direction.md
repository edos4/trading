# Direction-training experiments

`scripts/kronos_direction.py` implements a small supervised alternative for the
up/down/flat task. It trains two NumPy softmax classifiers, for one and three
stored trading sessions ahead. It uses no additional packages and does not
replace, retrain, or activate the production Kronos tokenizer/predictor pair.
The classifiers provide a reproducible baseline before spending another full
Kronos fine-tune on a changed objective.

## Training and labels

Each example uses 400 completed OHLCV bars by default. Twenty causal features
describe recent returns, volatility, ATR, candle shape, price position, volume,
turnover, stale prices, and market membership. No target bar enters a feature,
quality decision, normalization statistic, or flat-band calculation.

Both targets use the live `core.kronos_confirm.classify()` function and its
ATR-scaled flat band. Future returns use unrounded source closes. The loss is
three-class cross-entropy, optionally class weighted, rather than candle-token
reconstruction. Feature normalization is fitted on training examples only.
Softmax outputs are uncalibrated model probabilities.

Each run compares two sampling variants under the same budget and the same
validation/development examples:

- **Uniform:** market-balanced draws from all eligible historical windows.
- **Recent:** 10% uniform draws plus 90% recency-weighted draws, with a default
  two-year half-life. Recency mass is approximated in fixed 90-calendar-day
  buckets. Older windows retain a positive sampling probability; there is no
  five-year date cutoff.

Training samples can repeat. Validation and test samples are deterministic,
uniform within each market, and sampled without replacement. Each market has
its own sample cap, and selection gives US and PH equal weight.

## Fixed data-quality rules

Eligibility depends on the completed context, before labels are inspected:

- Mean 60-bar native-currency turnover must reach the existing paper-universe
  floors: **USD 20 million** for US and **PHP 5 million** for PH.
- At most 50% of those bars may have zero volume; at most 80% of the 59
  close-to-close transitions may be unchanged within relative tolerance 1e-6.
- A fourfold price discontinuity in that 60-bar context rejects the example.
- A context cannot cross more than five missing expected sessions, or retain
  a historical close more than ten times its anchor close.
- GBDMF, AXRX and GASE remain excluded. Additional exclusions are configurable.

These rules are stored before training and are identical for all splits and
dated inference. They never rewrite source bars. The quality report counts
rejection reasons (which can overlap), candidate windows, and eligible windows.
The existing frozen corpus already enforces finite positive OHLC, correct OHLC
ranges, and nonnegative volume. All experiments hash-verify that corpus first.

The available database universe is fixed by its existing snapshot; this is not
a certification of historical point-in-time exchange membership.

## Chronological model selection

Default development evaluation has three consecutive four-week test periods.
Each fold trains on all eligible earlier history and selects checkpoints using
the preceding eight-week validation period. A three-stored-session embargo
precedes validation and test boundaries, and every complete three-day target
must fit its assigned split. Contexts may use earlier bars.

Models are selected using validation macro-F1 (or balanced accuracy), with
balanced accuracy and up/down precision as tie breakers. The sampling variant
for each horizon is chosen from validation scores across folds. Development
test results do not participate in either choice.

Reports include per-market accuracy, balanced accuracy, macro-F1, all three
classes' precision/recall/F1/support, confusion matrices, and always-flat and
past-return-persistence baselines. Optional saved Kronos comparisons use only
identical eligible dated contexts with valid forecasts from both saved pairs.
Those comparisons are explicitly reused development evidence, not fresh tests.

## Run training

Use the application venv; PyTorch and the GPU are unnecessary:

```bash
.venv/bin/python scripts/kronos_direction.py \
  --dataset runs/kronos-flat-recalculation-2026-11-12/snapshot \
  --output runs/kronos-direction-2026-11-12 \
  --final-test-start 2026-11-07 \
  --kronos-evaluation runs/kronos-flat-recalculation-2026-11-12/rescored-evaluation.json
```

These dataset/report paths refer to the existing artifacts on the training
host. For another environment, supply its frozen corpus. Omit
`--kronos-evaluation` when a dated, recalculated forecast report is unavailable.
The output directory must be empty. `--dry-run` prints the plan without reading
the corpus or writing artifacts. Budgets, class weighting, quality thresholds,
recency parameters, embargo, and validation metric have explicit CLI options.
Keep thresholds fixed while comparing candidates.

## Reserved final evaluation

The final period is fixed **before training**, starts at `--final-test-start`,
and lasts 28 calendar days by default. It is excluded from every development
split and final-model selection. The start may equal the original snapshot's
exclusive end, allowing a genuinely later period to arrive after training.
Choose a period that has not already influenced your research; the program
cannot determine whether someone inspected another report previously.

Training writes `selection.json`, its SHA-256, and one checkpoint per horizon.
The selection binds configuration, implementation files, features, checkpoint
hashes, and the exact historical source prefixes. A later snapshot may append
new bars; historical bars for training symbols must still match exactly.

Once the entire reserved period and both markets' latest expected sessions are
available, run:

```bash
.venv/bin/python scripts/kronos_direction.py \
  --dataset /path/to/newer/frozen-corpus \
  --output runs/kronos-direction-2026-11-12 \
  --evaluate-final
```

Final evaluation reuses the locked policy and models. It rejects changed
history, checkpoints, policy, selection, implementation, incomplete dates, or
stale market data. It writes `final-evaluation.json` exactly once. Subsequent
requests must read that report, rather than retuning against the same period.

Research gates require coverage of symbols, samples and all classes, and
improvement over always-flat and persistence on both balanced accuracy and
macro-F1 in each market/horizon. Results that fail these gates are still saved.
No outcome automatically changes the active production model.

For the November 7 start, the reserved period is **November 7–December 4, 2026**
(exclusive end December 5). It is incomplete as of November 12. Development
results from this implementation must not be described as final accuracy gains.
