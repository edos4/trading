# Weekly Kronos fine-tuning: all available US and PH daily bars

History policy updated **2026-11-12**: new weekly runs use all available history
before the intended Saturday, replacing the original rolling five-year limit.
The validation/evaluation holdouts, cleaning rules, balanced sampling and fixed
training budget remain unchanged. Sections 10 onward retain historical run and
source-audit results; their five-year references describe those earlier runs.

Manual runs support repeatable `--exclude-symbol SYMBOL` options. Exclusions
apply before corpus construction, covering training, validation, evaluation and
reload smoke contexts. The normalized list is stored in the configuration and
corpus metadata, participates in the corpus hash and resume identity, and forces
a fresh candidate when it differs from the active same-week run. No symbols are
excluded by default. User-requested retraining on 2026-11-12 excludes `GBDMF`,
`AXRX` and `GASE`; its scores use a changed evaluation universe and must be
reported with that scope.

Status: **implementation complete; approved one-off model active**, updated
2026-11-09. The shared US/PH pair was activated at **09:25:07 PHT** using the
user-approved 2% invalid-rate cap for this run only. Previous fine-tuned
versions and temporary training data were deleted, and the gitignored success
timestamp was written. Weekly defaults and the installed, disabled timer remain
unchanged; see sections 10–12 for operational history and verification.

## 1. Required behavior and confirmed decisions

| Item | Decision |
|---|---|
| Schedule | Start every Saturday at **06:00 PHT**, explicitly `Asia/Manila` |
| Source | PostgreSQL `stocks_history`, `daily_bars` joined to `symbols` |
| History | **All available history** before the intended Saturday, with no lower date cutoff |
| Markets | **One shared model** trained on US and Philippine/PSE stocks |
| Components | Fine-tune the tokenizer first, then the predictor using that tokenizer |
| Initialization | Start each new weekly run from the original cached base weights |
| Replacement | Automatically activate a successfully validated tokenizer/predictor pair |
| Retention | **Delete all previous fine-tuned versions after successful activation** |
| Failure | Keep the currently active version if the candidate fails before activation |
| Last fine-tune | Overwrite gitignored `kronos-finetuned-at.txt` after each successful scheduled run |
| Runtime | Isolated `.venv-kronos`; keep the application `.venv` torch-free |

The shared model and deletion of all previous fine-tuned versions were confirmed
by the user. There is no retained rollback model after a successful run. The
original pretrained weights remain available for the next fresh fine-tune.
Historical `stocks_history` rows are never deleted or rewritten by this job.

## 2. Existing implementation and gaps

Build on the current code, rather than the historical state described at the
start of `docs/kronos-finetune.md`:

| Existing code | Reuse | Required change |
|---|---|---|
| `learn/kronos_weekly_dataset.py` | Streaming DB scan, float32 memmaps, existing normalization helpers | Implemented bounded snapshot, session dates, explicit splits, balanced sampling |
| `core/kronos_finetune.py` | Two training stages, checkpointing, seeded sampling | Parameterized output root, split-aware validation, run-bound resume |
| `scripts/kronos_finetune.py` | Training CLI and hyperparameters | Expose the new dates/output options and safe output rules |
| `core/kronos_process.py` | Shared interprocess GPU lock | Have weekly training participate in the same lock |
| `core/kronos_eval.py` | Predictor loading and forecast scoring | Accept an explicit, resolved candidate/active model root |
| `scripts/kronos_eval_test.py` | Walk-forward comparison machinery | Evaluate the frozen weekly snapshot separately for US and PH |
| `core/kronos_provenance.py` | Hash-bound tokenizer/predictor manifest | Generate and verify provenance for each weekly release |
| `core/kronos_confirm.py` | Forecast cache and decision evidence | Include the deployed model identity in cache keys and evidence |

Original gaps addressed by the weekly path (the legacy dataset format remains supported):

- The default training cutoff is fixed at `2026-01-01`; there is no five-year
  lower bound, and SQL currently scans all `daily_bars`.
- Corpus reuse checks the cutoff, without a complete data/config fingerprint.
- Model outputs and resume files are hardcoded under `~/Kronos/finetuned`;
  training there can expose a new tokenizer with an old predictor.
- The builder derives dates from UTC timestamps instead of using the stored
  market-session `bar_date`.
- The current validation loader needs a whole `256 + 10 + 1 = 267` bar window
  inside its validation slice. Ten percent of five years is only about 125
  daily bars, so applying the existing split unchanged produces zero validation
  windows even for a full-history stock.
- The existing evaluator's cross-market liquidity ranking can favor US stocks;
  it is insufficient as the weekly acceptance check for both markets.

## 3. Full-history source snapshot

Freeze an aware `scheduled_at` in `Asia/Manila` when the job starts. For a normal
Saturday run, define:

```text
window_end_exclusive = scheduled_at.date()
requested lower bound = none
included bar dates    = all available bar_date < window_end_exclusive
recorded window_start = earliest retained bar_date in the frozen corpus
```

For Saturday **2026-11-14 06:00 PHT**, include all available history with
`bar_date < 2026-11-14`; the latest normal session is Friday 2026-11-13 for each
market. Newer listings and delisted stocks with sufficient usable training
history may participate. The dry run prints `history_scope=all_available` and a
null `window_start`, since it does not query the database. After extraction,
both the corpus and run manifest record the actual earliest retained date.

Read through `data.db.build_dsn()` on the designated GPU training host. Confirm
that it reaches the authoritative `stocks_history` database; this may be a
direct connection or an existing tunnel. Do not assume that the GPU host owns
the database, and do not fall back to CSV, screeners, or another history source.

Use one read-only, repeatable-read transaction and a server-side cursor. Resolve
market membership in the same snapshot. Parameterize bounds and markets:

```sql
SELECT s.market, b.symbol, b.bar_date, b.ts,
       b.open, b.high, b.low, b.close, b.volume
FROM daily_bars AS b
JOIN symbols AS s ON s.symbol = b.symbol
WHERE s.market IN ('us', 'ph')
  AND b.bar_date < %(window_end_exclusive)s
ORDER BY s.market, b.symbol, b.bar_date, b.ts;
```

The current DB schema has `symbols.market`; `daily_bars` has no market column.
Use the existing `bar_date` index and inspect the query plan before considering
any additional index. Keep streaming, one symbol at a time, rather than loading
the full universe into DataFrames. Close the snapshot before GPU training.

### Data readiness and cleaning

- At 06:00 PHT the US Friday regular session has already closed, including
  during US daylight saving time. Its latest data import may still be pending.
  Check ingestion freshness independently for US and PH before freezing data.
- Determine each market's expected last completed session using market-aware
  holiday/session rules. Review the existing `core.market` helpers, including
  their US holiday handling and PH holiday coverage; do not assume Friday is
  always a session or infer freshness from a single newly updated ticker.
- Default readiness: at least 95% of a recorded reference set of recently active
  symbols in **each** market have the expected last-session bar. Exclude known
  suspended/delisted names from that freshness denominator, while retaining
  their valid historical training rows. Record the reference sets and counts.
- If either market is not ready, poll at five-minute intervals until 08:00 PHT,
  then fail without promotion. Keep the original Saturday bounds during the
  wait. Require the existing ingestion process to finish; this job is read-only.
- Keep canonical keys such as `AAPL` and `BDO.PS`; use `ph_history_symbol()` at
  inference boundaries. Track `(market, canonical_symbol)` throughout the corpus
  and never concatenate different stocks or markets into one sample window.
- Sort by `bar_date`, deduplicate a symbol/session deterministically using the
  latest `ts`, and reject nonfinite/nonpositive OHLC, invalid high/low ranges,
  and negative/nonfinite volume. Allow genuine zero-volume bars. Record rejects.
- Use `bar_date` for stamps. Do not add weekend bars, forward-fill missing
  sessions, or invent bars for suspended stocks. Record long gaps and exclude
  training windows spanning suspensions longer than five expected sessions.
- Retain the six input features: open, high, low, close, volume, and synthetic
  `amount = volume * mean(OHLC)`. Keep USD and PHP values in their native units;
  normalize each window using its lookback only, as today.
- Apply `min_bars=500` **across all history before the cutoff**, and require at least
  one legal training window per included symbol. Report training-only symbols
  separately from those with usable validation/evaluation targets. Abort if
  either market has zero usable training, validation, or evaluation samples.
- Use the existing price-adjustment convention consistently in training and
  inference. Report suspected split/discontinuity problems; do not silently
  change adjustment conventions or rewrite source bars.

## 4. Chronological splits with fixed recent holdouts

Use shared calendar boundaries for all symbols and both markets. Implemented weekly
defaults, relative to the exclusive Saturday end `E`:

| Purpose | Target/data dates | Use |
|---|---|---|
| Training | `[window_start, E - 16 weeks)` | Gradients for both components |
| Validation | `[E - 16 weeks, E - 4 weeks)` | Select each stage's best checkpoint |
| Evaluation | `[E - 4 weeks, E)` | Final promotion decision; no gradients |

This uses all eligible history before the final 16 weeks for gradients, 12 weeks
for validation, and four weeks for evaluation. Recent evaluation bars are
deliberately held out; there is no unscored refit on them after acceptance.

Fix window construction instead of requiring 267 validation-only bars:

1. Training windows stay wholly inside the training interval.
2. Validation/evaluation windows may borrow **earlier** bars as lookback context
   from the same symbol, within the frozen corpus. Every scored target must
   be in its assigned split, and the full prediction suffix must fit there.
3. Keep normalization fitted only on the lookback. Return a target mask/date
   range with each validation sample. Predictor validation cross-entropy and
   tokenizer validation reconstruction error score only held-out positions;
   reused context must not contribute to checkpoint-selection metrics.
4. Validation and evaluation run without optimizer updates. Evaluation forecasts
   read only bars through each forecast anchor; future target bars are labels.
5. Record train, validation, and evaluation window counts per symbol and market.
   Sparse symbols can contribute to training without being required in all
   later splits, but each market must have sufficient later-split coverage.

Use one frozen snapshot for training, validation, candidate evaluation, and
baseline evaluation. Never reread changing live history during those stages.

## 5. Shared-model training and evaluation

Start each weekly candidate from cached `Kronos-Tokenizer-base` and `Kronos-base`
weights under `~/Kronos/weights`. Fine-tune the tokenizer, freeze the selected
tokenizer, then train the predictor against its encodings. Do not load the
previous week's fine-tuned weights or optimizer state into a new weekly run.

Implemented training defaults reuse the current campaign: lookback 256, prediction
length 10, clip 5.0, batch size 16, seed 42, 10 epochs per stage, tokenizer LR
`2e-4`, predictor LR `4e-5`, 2,000 training steps and up to 400 validation steps **per market**
per epoch, using the equal-weight mean for checkpoint selection. Require CUDA and fail explicitly if unavailable; avoid an unattended
CPU fallback. Calibrate batch size and duration with a short GPU smoke run.

For training, choose a market with probability 50% US / 50% PH, then sample a
legal window within that market. Make weights configurable and record actual
sample counts so the larger US universe cannot crowd PH out. Keep validation
sampling fixed across epochs and report each market plus their equal-weight
mean. The universe is a sampling pool; a fixed epoch budget does not visit every
window or every stock each week.

Evaluate the selected candidate against the original base pair using identical
frozen anchors and seeds, separately for US and PH. Sample up to 50 eligible
symbols per market, require at least 25 symbols and 50 scored windows per market
and horizon, and include liquid and less-liquid stocks using within-market
ranking. Never compare raw US dollar volume with raw Philippine peso volume.

Check one-day forecasts used by scanner confirmation and three-day forecasts
supported by the current evaluator. Record direction accuracy, absolute return
error, invalid forecast rate, sideways classification, sample counts, and
inference failures. Require finite, positive, credible prices using the scanner's
existing credibility rules.

Implemented automatic acceptance defaults, fixed before the first scheduled run:

- Both components reload from the candidate directory, all file hashes verify,
  and US plus PH smoke forecasts succeed through the production loader.
- Both markets meet coverage requirements; every required metric is finite and
  every scored forecast is valid.
- For **each market and horizon**, direction accuracy is no more than two
  percentage points below base and mean absolute return error is no more than
  10% above base. When baseline error is zero, require candidate error to be zero.
- Both components have finite validation losses and a selected best checkpoint.

Treat these thresholds as initial engineering defaults, exposed in the run
configuration and locked before evaluating the candidate. A failure means no
replacement that week. Record the metrics without claiming improved trading
profitability. If comparing with the active fine-tuned model as well, verify its
selection boundary before treating those windows as unseen by that model.

## 6. Replacement and deletion of previous fine-tuned data

Make the full model pair a single release. Implemented layout, on one filesystem:

```text
~/Kronos/weights/                    # original pretrained weights; retained
~/Kronos/weekly/runs/<run-id>/
    dataset/                        # freshly built, temporary corpus
    tokenizer/best_model/
    predictor/best_model/
    selection_manifest.json
    run_manifest.json
    evaluation.json
~/Kronos/finetuned -> weekly/runs/<active-run-id>
~/Kronos/weekly/reports/<run-id>.json # small audit reports; no weights/corpus
```

Required activation sequence:

1. Acquire a dedicated weekly-job file lock before any build. Also hold the
   existing `/tmp/kronos_gpu.lock` across training, GPU evaluation, activation,
   and deletion. Use the same path and lock protocol as `core.kronos_process`.
   Wait at most 30 minutes for that GPU lock, then fail cleanly. All direct
   model readers/writers must follow this protocol; do not nest a worker call
   that tries to reacquire the same lock.
2. Build into a new run directory; pass an explicit output root to **both**
   training stages, best checkpoints, and `last.pt` files. Never train through
   the active `finetuned` symlink. Check disk space for old plus candidate plus
   temporary training state before starting.
3. Validate and hash the complete candidate. Record snapshot bounds, split
   bounds, data fingerprint, source-code/base-weight identities, hyperparameters,
   per-market metrics, and component file hashes. Generate the existing
   schema-v1 `selection_manifest.json`, including `corpus_sha256`, both component
   file maps, `selection_data_end`, and `eligible_from` strictly after that date.
   Selection bounds include validation **and promotion evaluation** data.
4. Resolve the old active root once. Create a temporary symlink beside
   `~/Kronos/finetuned` and atomically replace the active symlink with
   `os.replace()`. Flush completed artifacts and directory changes before
   reporting activation as durable. Readers resolve one release root once and
   load tokenizer, predictor, and provenance from that same root.
5. Under the lock, smoke-test the active path. If this immediate check fails,
   restore the still-present old pointer and fail. Deletion begins only after
   this activation check succeeds; the old copy exists only during this
   transaction, rather than as a retained rollback version.
6. Invalidate old forecast caches using a model identity derived from the
   manifest hash. Include that identity in cache keys, pending-batch matching,
   worker responses, and decision evidence. Reject stale responses and prevent
   cached forecasts from the previous model being returned after activation.
7. Delete **all superseded managed fine-tuned release directories**, their
   datasets, `last.pt` files, optimizer state, and rejected candidate artifacts.
   Remove the successful candidate's temporary dataset and resume state after
   copying the necessary counts/hashes into its manifests and audit report.
   Leave exactly one deployed fine-tuned tokenizer/predictor pair.
8. Retain only small metadata/evaluation reports for earlier runs, with a bounded
   report retention policy, such as 52 weeks. Preserve base weights and source
   database rows. Reports must not contain credentials or connection secrets.
9. Write `kronos-finetuned-at.txt` as the final successful-run step, after
   activation checks, cache invalidation, cleanup, and audit-report persistence.
   Return success to the scheduler only after the timestamp is safely written.

For the first run, `~/Kronos/finetuned` is currently a real directory. Under the
same lock, move it into the managed runs area and install the active symlink;
restore the original layout if migration fails. Inventory legacy campaign
fine-tuned outputs before enabling the schedule and include known superseded
ones in cleanup. Restrict deletion to an explicit inventory/managed root,
refuse symlink traversal or deletion of the active root, and never recursively
delete `~/Kronos` or `weights`.

Use a durable activation/cleanup journal. After a crash, verify which complete
release the active pointer names, finish any pending deletion, and remove
unreferenced candidates. A cleanup failure after activation is a reported
`cleanup_failed` state with the new model active; the job cannot report full
success until all previous fine-tuned versions have been deleted. Do not roll
back after deletion has started. There is no model rollback after full success.

Keep provenance truthful for historical replay: a decision before
`eligible_from` still runs, and its evidence records
`out_of_sample_eligible=false`. Separate a live decision's
timestamp from its most recent bar date, so a Monday live forecast anchored on
Friday is handled explicitly without declaring Friday a historical holdout.
Deleting previous releases means old backtests cannot reload those models;
their audit metadata and previously recorded forecasts remain available.

### Last successful fine-tune timestamp

The scheduled job must overwrite **`<checkout-root>/kronos-finetuned-at.txt`**
after each successful run. On this checkout the path is
`/home/engine-33ai/codes/trading/kronos-finetuned-at.txt`. Resolve it from the
checkout root, rather than the scheduler's current working directory.

The file contains exactly one ISO 8601 timestamp and a trailing newline, using
`Asia/Manila` with an explicit `+08:00` offset. For example:

```text
2026-11-14T07:12:34+08:00
```

This is the **actual successful completion time**, rather than the scheduled
06:00 start or the date of the latest training bar. Replace the previous value;
do not append a history. A successful manual recovery run uses the same rule.

Write through a temporary sibling named `.kronos-finetuned-at.<unique-id>.tmp`,
flush and fsync it, atomically replace the marker with `os.replace()`, and fsync
the parent directory. Keep the weekly-job lock through this final step. Persist
the same completion time and active model identity in the run manifest/report,
so readers can associate the marker with the deployed release.

Failed training, rejected evaluation, interrupted/failed cleanup, skipped runs,
and `--dry-run` must preserve the previous timestamp. Leave the file absent
until the first successful fine-tune; do not create a placeholder timestamp.
If the marker write fails after model activation, retain the active model,
report `timestamp_write_failed`, and return a nonzero job exit code. Recovery
must reconcile the marker with the active run's persisted completion time
without rerunning training or overwriting a newer release's timestamp.

Perform the write in the scheduled orchestrator's success path, so a cron
wrapper or systemd service observes it before the job exits successfully.
Do not put it in an unconditional shell command, `finally` block, or
`ExecStopPost`, which would also stamp failed runs. The root marker and temporary
siblings are excluded in `.gitignore`; the writer is implemented in the weekly
job's final success path.

## 7. Weekly scheduling

Use a host-local systemd **user timer** under the account owning the GPU and
cached weights. This is host maintenance, so no Codex automation is needed.
The job starts at 06:00; training finishes later, subject to data/GPU readiness.
PHT is UTC+08:00: Saturday 06:00 PHT is Friday 22:00 UTC.

Installed `~/.config/systemd/user/kronos-weekly-finetune.timer` (disabled):

```ini
[Unit]
Description=Weekly shared US and PH Kronos fine-tuning

[Timer]
OnCalendar=Sat *-*-* 06:00:00 Asia/Manila
AccuracySec=1s
RandomizedDelaySec=0
Persistent=false
Unit=kronos-weekly-finetune.service

[Install]
WantedBy=timers.target
```

`Persistent=false` intentionally skips a run while the host/user manager is
offline, keeping automatic starts on Saturdays. Any manual recovery run must
name its intended Saturday and record the actual start time. It must not import
bars after that scheduled snapshot boundary. See the official
[systemd timer semantics](https://github.com/systemd/systemd/blob/main/man/systemd.timer.xml)
and [calendar/timezone syntax](https://github.com/systemd/systemd/blob/main/man/systemd.time.xml).

Service template (the installer writes the actual absolute checkout paths):

```ini
[Unit]
Description=Build, train, validate, and replace the weekly Kronos model

[Service]
Type=oneshot
WorkingDirectory=%h/codes/trading
Environment=TZ=Asia/Manila
Environment=PYTHONUNBUFFERED=1
Environment=KRONOS_DEVICE=cuda
ExecStart=%h/codes/trading/.venv-kronos/bin/python scripts/kronos_weekly_finetune.py
TimeoutStartSec=12h
KillMode=control-group
Restart=no
```

The weekly entrypoint loads existing DB configuration normally, without copying
credentials into unit files or editing `.env`. Do not automatically stop other GPU applications;
measure available VRAM and fail cleanly if the configured training cannot fit.
The current GPU lock coordinates Kronos only, so other GPU users need a measured
resource budget. Kronos inference requests will report busy during GPU training.

Validate the schedule before installation:

```bash
systemd-analyze calendar --base-time='2026-11-08 00:00:00 UTC' --iterations=3 \
  'Sat *-*-* 06:00:00 Asia/Manila'
```

Verified during planning: the next three occurrences are **November 14, 21, and
28, 2026, all at 06:00 PHT**. The first is November 13 at 22:00 UTC.

After implementation and a successful manual rehearsal, install only the new
host user units, verify them with `systemd-analyze --user verify`, reload the user
manager, and enable the timer. Ensure that account's user manager runs while
logged out, using an existing lingering setup or a host-level provisioning step.
Inspect `systemctl --user list-timers` and
`journalctl --user -u kronos-weekly-finetune.service`. Do not modify existing
`deploy/` units or `scripts/*_daily.sh` cron blocks.

## 8. Implementation work and verification

The following implementation work is complete; production activation still
requires the full-data rehearsal below.

- [x] **Dataset:** add explicit start/end bounds, read-only snapshot extraction,
  `bar_date` stamps, quality reports, split target masks, and per-market counts
  in `learn/kronos_weekly_dataset.py`. Fingerprint all dates, filters, split/sampling
  settings, and actual source content. Build a fresh corpus every week.
- [x] **Trainer:** parameterize output/checkpoint roots in
  `core/kronos_finetune.py` and its CLI. Implement usable short validation splits
  and balanced sampling. Refuse active-output writes. Bind resume to the same
  run ID, corpus/config/base hashes, stage, and tokenizer identity; never resume
  across weeks. Allow an interrupted run to resume only before activation.
- [x] **Evaluation/loading:** accept explicit model roots and frozen snapshot
  inputs, score both markets and horizons, and enforce predetermined acceptance
  thresholds. Load component pairs and manifests from one resolved root.
- [x] **Lifecycle:** add `core/kronos_weekly.py` for scheduling bounds, freshness,
  locks, manifests, atomic activation, migration, cleanup, and crash recovery;
  add `scripts/kronos_weekly_finetune.py` as the orchestrator. Provide read-only
  `--dry-run` planning and `--run-date YYYY-MM-DD` for a named Saturday. Write
  the last-success timestamp atomically as the final successful-run step.
- [x] **Runtime:** update model resolution/provenance and scanner cache identity
  handling, including canonical PH symbol smoke coverage and replay eligibility.
- [x] **Operations:** provide an installer for the two new user units without
  changing protected deployment/cron files. Document enable, disable, manual run,
  logs, and cleanup-failure recovery commands. Enabling happens after rehearsal.

Tests are implemented in `core/test_kronos_weekly.py`, alongside existing
`core/test_kronos_finetune.py` and affected worker/cache tests:

1. No default lower date bound, first/last included bar dates, older/delisted
   training symbols, both market memberships, `.PS` keys, session-date handling,
   and SQL bounded only by the exclusive Saturday end. Explicitly bounded
   dataset-builder calls remain supported for source audits and older fixtures.
2. Freshness behavior around US daylight saving time, market holidays, suspended
   stocks, partial imports, and timeout while either market is stale.
3. Full-history synthetic corpora with lookback 256 produce validation/evaluation
   windows for both markets. Training never touches later-split bars; metrics
   exclude reused context and normalization never uses future targets.
4. No window crosses symbol/market boundaries; deterministic sampling includes
   both markets, and a missing/sparse PH corpus blocks promotion.
5. Output isolation and resume fingerprint mismatches, including a changed
   tokenizer, week, corpus, or training configuration. A same-week legacy
   five-year release must retrain once under `history_scope=all_available`;
   repeated successful all-history runs for that week remain idempotent.
6. A reader never sees mixed component versions. Cover legacy migration,
   interrupted activation, failed smoke checks, deletion path guards, and
   cleanup recovery without deleting the active model or original base weights.
7. Successful replacement leaves exactly one fine-tuned pair and no previous
   weights/corpora/resume states. Pre-activation failures preserve the old model;
   post-activation cleanup failures remain visible and recoverable.
8. Cache/worker responses change model identity on replacement. Valid historical
   provenance remains enforced, including live-versus-replay date semantics.
9. Verify the timer's next three Saturday 06:00 PHT occurrences and confirm that
   duplicate/manual starts cannot overlap scheduled training.
10. Successful scheduled/manual runs overwrite the marker with one completion
    timestamp in PHT; failures, rejection, cleanup failure, skips, and dry runs
    leave it unchanged. Test atomic writes, write-failure recovery, first-run
    absence, checkout-root resolution, and Git ignore rules for both marker and
    temporary files. Confirm the job cannot exit successfully before the write.

Run focused app-venv tests first, then affected Kronos integration/web tests.
Use synthetic corpora and temporary model roots for destructive lifecycle
tests. Perform one `.venv-kronos` GPU smoke rehearsal, then a full rehearsal
against the real read-only snapshot into staging, before enabling promotion and
the timer. Measure extraction time, training time, evaluation time, peak VRAM,
disk usage, and cleanup time. The 2026-10-15 campaign's roughly 56-minute result
is historical context, not a weekly SLA; set the operational budget from the
new rehearsal and keep the 12-hour service timeout as a hard ceiling.

## 9. Completion criteria

- [ ] Every scheduled start is Saturday 06:00 PHT regardless of host timezone.
- [ ] Each run uses all available `stocks_history.daily_bars` history before the
  intended Saturday, with fresh and usable US **and** PH data.
- [ ] One shared tokenizer/predictor pair trains from the original base weights
  and passes separate US/PH evaluation with verifiable provenance.
- [ ] A passing candidate automatically replaces the active pair atomically,
  and runtime consumers/caches use the new model identity.
- [ ] After success, all previous fine-tuned versions and temporary training
  data are deleted; only the new active pair and small audit reports remain.
- [ ] Every successful job overwrites the gitignored root
  `kronos-finetuned-at.txt` with its actual completion timestamp in PHT; failed
  jobs preserve the prior timestamp.
- [ ] Failed training/evaluation leaves the prior active pair intact; interrupted
  promotion/cleanup recovers to one complete active pair with explicit status.
- [ ] No source history, `.env`, immutable pattern registry, existing deployment
  units, existing daily cron blocks, or unrelated application changes are modified.


## 10. Operator commands and deployment verification

Run from the checkout root. Inspection and staging:

```bash
.venv/bin/python scripts/kronos_weekly_finetune.py --dry-run --run-date 2026-11-14
.venv-kronos/bin/python scripts/kronos_weekly_finetune.py --preflight --run-date 2026-11-07
.venv-kronos/bin/python scripts/kronos_weekly_finetune.py --rehearsal --run-date 2026-11-07
```

`--smoke` runs a two-step staging-only experiment; it cannot qualify the timer
for activation. A full rehearsal uses the normal production settings. Every
rehearsal preserves the active model and timestamp. `--resume RUN_ID` resumes
only an interrupted candidate with identical source, base weights, corpus,
window dates, configuration, and tokenizer identity. Reuse `--rehearsal` when
resuming a rehearsal. `--recover` completes an interrupted activation/cleanup
without retraining or requiring CUDA.

Once a full rehearsal passes, use its recorded manifest to enable the timer:

```bash
.venv/bin/python scripts/install_kronos_weekly.py --install --enable \
  --rehearsal-manifest ~/Kronos/weekly/runs/RUN_ID/run_manifest.json
systemctl --user list-timers kronos-weekly-finetune.timer
journalctl --user -u kronos-weekly-finetune.service -f
```

The installer rejects smoke receipts, changed source, a failed rehearsal,
settings different from the scheduled production defaults, or a user account
without lingering. `--install` alone verifies and installs units without
enabling the timer. Manual production runs use the weekly CLI without
`--rehearsal`; the same freshness/evaluation/replacement checks apply. To stop
future scheduled starts:

```bash
systemctl --user disable --now kronos-weekly-finetune.timer
```

Legacy one-off training now requires an isolated `--output-dir`, and defaults
its dataset to that output directory. It refuses active-tree and original base
weight writes. Use the weekly CLI for automated activation; the historical
campaign wrapper also writes isolated outputs and evaluates that explicit pair;
use the weekly workflow for scheduled production replacement.

Observed on 2026-11-08:

- Focused regression suite: **198 tests passed**; the weekly lifecycle suite also
  passed all 30 tests after adding a positive live-provenance check.
- NVIDIA RTX 5080 and cached weights are available on the host. Sandbox access
  does not expose CUDA; real model checks ran outside the sandbox.
- Both real model stages passed tiny synthetic CPU and GPU staging runs.
  The GPU run also verified the selected pair's manifest and completed US/PH
  smoke forecasts using the production model loader. Temporary models were
  removed; production model files and the timestamp were untouched.
- Systemd verified and installed the two new user units. The unrelated existing
  `spice-vdagent` unit emits a warning; the Kronos units passed verification.
- The full-data rehearsal for Saturday 2026-11-07 **stopped at readiness**:
  US has 9,382 / 17,201 reference symbols (54.54%) and PH has 200 / 245 (81.63%)
  with the expected November 6 bar. Both must reach 95%. No candidate was
  trained or activated by that rehearsal, and a small `data_not_ready` report
  was written under `~/Kronos/weekly/reports`.
- The three explicitly inventoried legacy directories are
  `finetuned_campaign`, `finetuned_expA_champion`, and `finetuned_run1`.
  A successful activation deletes those along with superseded managed releases.
  Unexpected `finetuned_*` entries block activation for inventory review.
- **Timer activation remains pending the normal history import and a successful
  full-data rehearsal.** The job never changes source history to satisfy its
  readiness check.


## 11. Manual run and review behavior (2026-11-09)

The uncapped daily refresh checked 8,612 stale symbols and added 7,531 bars.
Direct provider audits of the remaining 295 recent US gaps and 45 PH gaps found
no retrievable November 6 bars; all available provider dates are already stored.
The audit is `docs/verification/kronos-daily-update-2026-11-09.json`.

The user approved a one-off snapshot through October 30 (`--run-date 2026-10-31`)
and `--freshness 0.8`. Weekly defaults remain unchanged. The first full run
completed both stages but failed the zero-invalid-forecast gate for both base
and candidate models. Its rejected candidate was removed by the original
failure path; the active model and last-success marker were preserved.

Failure handling now persists training metrics and complete evaluation results
before rejection. Complete rejected pairs and their frozen snapshots remain in
managed runs for review and reevaluation; incomplete failed training outputs
are still removed. A later successful activation deletes every superseded
managed release, including these review candidates. No failed/rejected run
updates `kronos-finetuned-at.txt`.

A second full run is staging-only. The user approved the capped relative
invalid-rate policy for this one-off run; the saved candidate will be reevaluated
under that explicit policy and activated only if all selected checks pass.
The zero-invalid rule remains the default. An optional explicit one-off setting,
`--invalid-policy relative --max-invalid-rate 0.01`, requires the candidate's
invalid forecast rate to be no greater than base and at most 1%, retaining
coverage, finite-metric, direction-accuracy, and return-error checks. Runtime
credibility filtering remains active regardless of evaluation policy.

A retained complete candidate can be reevaluated without repeating training:

```bash
.venv-kronos/bin/python scripts/kronos_weekly_finetune.py --evaluate RUN_ID --rehearsal
```

Omit `--rehearsal` only for an authorized activation attempt; all selected
acceptance checks and US/PH smoke forecasts must pass. Evaluation policy
changes are recorded alongside previous results in `evaluation_history`.


## 12. Successful one-off activation (2026-11-09)

The user explicitly approved a 2% invalid-rate cap for this run only. The saved
pair passed the capped relative check, direction/error checks, and US/PH
production-loader smoke forecasts, then activated at **2026-11-09 09:25:07 PHT**.

- Active release: `~/Kronos/weekly/runs/2026-10-31-f949b6a3e7b2`.
- Active pointer: `~/Kronos/finetuned`.
- Model identity: `4c3b719f99059a0ce7f9e2deee605b570f218624c0fe98c2c98abb13226ec0f3`.
- Corpus: 18,599,288 bars; 15,232 US stocks and 203 PH stocks; the approved
  five-year interval through October 30, 2026.
- Exactly one managed release remains. All previous managed releases and the
  three inventoried legacy versions were deleted. Dataset and resume/optimizer
  files were removed from the active release. Original base-weight hashes match.
- `kronos-finetuned-at.txt` contains `2026-11-09T09:25:07+08:00` plus a newline,
  and its Git ignore rule was verified.
- The original smoke sampler could select a PH stock with no legal windows.
  It now uses contexts that had usable forecasts in the full evaluation; this
  reload check does not change the aggregate acceptance results or hide their
  recorded invalid forecasts. The repaired GPU checks and 34 lifecycle tests
  passed before the successful activation retry.
- Weekly defaults remain freshness 95% and the zero-invalid policy. The timer
  remains disabled; the approved 80% freshness and 2% invalid-rate settings were
  one-off overrides, recorded in the active release manifest.

Verification: `docs/verification/kronos-finetune-activation-2026-11-09.json`.
The complete evaluation and policy history remain in the active release.

## 13. Invalid-forecast root cause and correction (2026-11-09)

The original candidate's 17 invalid US anchors (both horizons) were all GTIJF.
Its anchor close was approximately $0.0001, while the 256-bar context still
contained prices near $4.90. The native predictor normalizes the entire context
with its mean and standard deviation. After this collapse, small absolute model
errors became enormous close-to-close returns at the new price scale. Raising
the acceptance cap treated the symptom; it did not repair this input mismatch.

`core/kronos_input.py` now applies a shared, past-only context policy to weekly
evaluation, confirmation workers, legacy evaluation and web forecasts. It starts
at the latest observed tenfold downward close discontinuity whose preceding
price is still over ten times the anchor. It retains at least 60 post-collapse
bars, or abstains with an explicit context error. Upward reverse splits do not
trigger this collapse-specific guard. Prices and forecast returns are unchanged;
there is no GTIJF exclusion, return clipping, relaxed credibility bound, padding,
or future-label inspection. Worker evidence records the policy, original/used
bar counts and selected regime start; the confirmation cache includes the policy
version. Malformed source OHLC ranges still cause an explicit data error.

The exact original snapshot was reproduced before the database repair:
`7de6d1b20f1433433f7a4ca2464142b345acb4fa1a9f52f090d043ec5a99ef32`.
Using the unchanged active tokenizer/predictor pair and the original evaluation
anchors with `post-price-collapse-v1`:

| Market | Horizon | Forecasts | Candidate invalid |
|---|---|---:|---:|
| US | 1 day | 864 | 0 |
| US | 3 days | 864 | 0 |
| PH | 1 day | 786 | 0 |
| PH | 3 days | 786 | 0 |

Direction and return-error gates also pass. Evaluation now compares skill on
identical usable base/candidate anchors while retaining each model's full
invalid counters. The default zero-invalid requirement applies to the deployable
candidate; baseline failures do not veto a flawless candidate, but insufficient
usable comparison coverage still fails. The one-off 2% activation remains in the
historical manifest. This inference-only correction does not rewrite weights,
selection provenance, or `kronos-finetuned-at.txt`.

Full rows and metrics: `docs/verification/kronos-invalid-regression-2026-11-09.json`.

## 14. Source verification and daily-history repair

The old updater fetched recent history but appended only beyond each symbol's
newest timestamp, leaving internal gaps and old bad values untouched. It now
merges every valid returned session, including interior holes and provider
corrections, while preserving existing timestamps for each session. It excludes
unclosed bars and uses the market holiday calendar for the last closed session.

For a full five-year source check, use:

```bash
.venv/bin/python scripts/repair_daily_bars.py --repair --start 2021-10-31 --workers 8
.venv/bin/python scripts/repair_daily_bars.py --secondary-us --deduplicate --start 2021-10-31
```

Both commands take the existing update lock. Primary verification bypasses caches
and requests bounded Yahoo histories and yearly PSE Edge histories for every
inventoried symbol. Each source session is checked for finite positive OHLC,
valid high/low envelopes and nonnegative volume. A missing calendar date alone
is not evidence of a missing trade and never creates a synthetic candle.
Provider failures, empty histories, malformed source bars and no-source calendar
gaps are reported separately.

The optional secondary pass uses configured Alpaca SIP daily bars with complete
pagination. It fills only missing or invalid primary observations when at least
three nearby valid overlapping closes agree on the raw price basis; incompatible
or unverified overlaps are reported instead of spliced. Existing good primary
bars remain authoritative. The current account does not permit OTC data, so SIP
cannot repair malformed OTC observations. Duplicate cleanup removes only valid
identical OHLCV copies for a session, retaining the newest timestamp and refreshing
symbol metadata. Conflicting observations remain flagged for source review.

Detailed runtime reports are gitignored under `logs/daily-bars-*.json`. The final
verification report must distinguish successful import of all available source
bars from unresolved source errors; a high freshness percentage alone cannot
establish historical completeness.

The maintenance command supports `--resume` for the same dates and scope. It
retains completed source checks and retries unverified/unfinished symbols; new
full runs prioritize PH and pace Yahoo request starts. To apply explicitly
reviewed exchange-report corrections together with the final pass:

```bash
.venv/bin/python scripts/repair_daily_bars.py --secondary-us --deduplicate \
  --start 2021-10-31 \
  --corrections docs/verification/pse-daily-bar-corrections-2026-11-09.json
```

The corrections file records expected old values, replacement full bars, official
report URLs, PDF hashes and pages. The exchange rows were checked in both text
and rendered-page views. Applying a correction fails if the stored row changed
since review, and succeeds idempotently if it already matches the replacement.
The normal updater rejects the malformed primary responses, so those responses
cannot overwrite these valid corrections. Source PDFs are retained under
`docs/verification/sources/`.

The independent November 6 PSE Daily Quotation Report exposed the FMETF source
lookup failure: the same fund changed its symbol to AETF effective October 12,
2026 (PSE notice C07269-2026). `resolve_ids` now resolves the legacy historical
key through AETF's current security ID, preserving continuity. RRHI's absence
from the current report is explained by its August 31, 2026 delisting. These
notices are recorded in the verification reports; stale inventory entries are
not assigned fabricated current bars.

The final focused suite passed **256 tests**, including context causality,
validity checks, common-anchor scoring, interior-gap insertion, source repair,
secondary pagination/price-basis checks, resumability, duplicate preservation,
reviewed-correction guards and the ETF alias. Actual production confirmation
workers also returned three valid simulations each for AAPL (US) and BDO (PH)
using the unchanged active model and the new policy.

## 15. Final repair verification (2026-11-09)

The completed repair and its explicit source limitations are recorded in
`docs/verification/kronos-data-repair-2026-11-09.md` and the companion
`daily-bars-completeness-2026-11-09.json`. The final focused suite passed 272 tests.
All 201 latest-session traded entries in the tracked PH universe match the exchange report, with no
malformed PH rows, duplicate sessions or unresolved PH source lookups. US history
still has explicitly listed malformed/source-unverified and conflicting records;
it is not certified fully complete. No forecast-validity or freshness threshold
was relaxed to conceal these exceptions.
