# Kronos-base fine-tuning on stocks_history

Status: proposed. This document plans the work; it does not change runtime behavior.

Decisions locked with the request:

| Decision | Value |
|---|---|
| Base model | `NeoQuasar/Kronos-base` (102.3M params, context 512) + `NeoQuasar/Kronos-Tokenizer-base` |
| Training material | `stocks_history` PostgreSQL — **all ~17,719 symbols (US + PH)**, ~44.6M daily bars |
| Training host | This box, RTX 5080 (16 GB, sm_120), **coexisting with FreeToken's Qwen3.6-35B** |
| Scope | Dataset build → tokenizer + predictor fine-tune → walk-forward evaluation vs. base |
| Out of scope | Restoring the bot's Kronos *runtime* (gate, forecast, `/api/kronos/predict`, UI tab, MCP wiring) |
| Required output | Explicit wall-clock time budget for the fine-tune |

---

## 1. Where we are today

The Kronos **runtime was deliberately removed** from the bot in commit `89240d9`
("Remove the Kronos forecast model from the bot.") — 68 files, 5,147 deletions:

| Deleted | Lines | Role |
|---|---|---|
| `core/kronos_finetune.py` | 328 | The fine-tuner this plan restores |
| `core/kronos_eval.py` | 756 | Walk-forward scorer (`--kronos-test`) |
| `core/kronos_gate.py` | 354 | 3-day / 3% confirm gate |
| `core/kronos_forecast.py` | 190 | Viewer payload for the chart overlay |
| `core/kronos_rank_sleeve.py` | 505 | Cross-sectional top-K sleeve |
| `docs/kronos-batch.md` | 250 | Batch-inference design doc |
| README Kronos section | 219 | Setup, weights, gate, fine-tune docs |

Everything is recoverable from git — `git show 89240d9^:<path>`.

**Verified current state** (checked, not assumed):

- `config.py`, `web/` and `main.py` contain **zero** Kronos references. `web/` has no
  `/api/kronos/predict` route and no `/kronos` page; `config.py` has no `KRONOS_*`
  settings (pydantic `extra: "ignore"` silently drops the `.env` `KRONOS_*` block).
- `mcp_server/tools_kronos.py` still ships `kronos_predict`, proxying
  `POST /api/kronos/predict` — **a dangling route**. The MCP test asserting the `kronos`
  tool group still passes, so this inconsistency is currently invisible.
- `~/Kronos` **does not exist**; `~/.cache/huggingface` holds only Qwen models. No Kronos
  weights are cached anywhere.
- `__pycache__/*.pyc` for the deleted modules remain, but are not a reliable restore
  source — **git history is**.

## 2. What "fine-tuning Kronos-base" actually trains

Kronos is a two-stage model, and fine-tuning means training **both** stages in order:

1. **Tokenizer** (VQ-VAE, `KronosTokenizer`) — quantizes continuous OHLCV bars into
   hierarchical discrete tokens. Fine-tuned first, so the discrete vocabulary matches
   *this* data distribution.
2. **Predictor** (`Kronos`, 102.3M params, decoder-only) — autoregressive next-token
   prediction over those tokens. Fine-tuned second, using the **fine-tuned tokenizer's**
   frozen encodings.

The second stage depends on the first: `_train_predictor` loads `tokenizer_path`, which is
`TOKENIZER_OUT` when available. Skipping the tokenizer stage (`--skip-tokenizer`) is
supported but only meaningful if a fine-tuned tokenizer already exists.

Upstream ships its pipeline under `finetune/` (`qlib_data_preprocess.py`,
`train_tokenizer.py`, `train_predictor.py`, `qlib_test.py`) and expects **Qlib** data plus
**torchrun/DDP**. We deliberately do **not** use that path:

- No Qlib — the upstream `QlibDataset` does `from config import Config`, which would
  shadow *this* repo's top-level `config.py` on `sys.path`.
- No DDP/torchrun — one GPU, nothing to coordinate.
- The training loops are ported faithfully (same loss, same window normalization,
  AdamW + OneCycleLR, checkpoint on best val loss).

## 3. Training material: the `stocks_history` Postgres

**Why local Postgres is the right source here.** `data/history.py::local_history_backfill_enabled()`
returns True when `STOCKS_HISTORY_URL` is empty **and** `owns_local_stocks_history()`. On this
box `.env` sets `STOCKS_HISTORY_OWNER=true`, so `list_history_symbols()` →
`db.all_symbols()` and `load_daily_bars()` → `db.load_daily_ohlcv_rows()` read Postgres
directly, with **no** network fallback and no Yahoo/TV walk. The existing
`learn/dataset.py::iter_ticker_frames()` therefore already reads the right place.

**Schema** (`data/db.py:37-63`) — only two tables matter:

```sql
daily_bars(symbol TEXT, ts BIGINT, bar_date DATE,
           open, high, low, close DOUBLE PRECISION, volume BIGINT,
           PRIMARY KEY (symbol, ts))          -- + index on (bar_date)
symbols(symbol TEXT PRIMARY KEY, letter TEXT, market TEXT DEFAULT 'us',
        last_bar_ts BIGINT, row_count BIGINT, updated_at TIMESTAMPTZ)
```

**Volume** (`logs/check_db.log`, last `--check-db` run):

| Fact | Value |
|---|---|
| Symbols | 17,719 |
| Bars | 44,643,859 |
| Date range | 2010-01-04 → 2026-08-07 |
| DB size | 5.88 GB |
| Avg bars/symbol | ≈2,520 |

Bars are **raw daily OHLCV**, `us` + `ph` (PH tickers carry a `.PS` suffix). No intraday,
no weekly (weekly is resampled on read). `bar_date` is derived from `ts` in the market's
timezone and is reporting-only.

**Currency mixing is a non-issue.** PH bars are PHP, US bars are USD, and the old code
ranked liquidity across both — a real flaw. But the training path z-normalizes every
window from its own lookback (`(x - x_mean) / (x_std + 1e-5)`), so absolute price level
and currency cancel out. We therefore keep US + PH in one corpus and drop the
cross-currency liquidity *ranking* entirely (see §5).

## 4. Architecture

```
stocks_history (Postgres, local socket)
      │  single streaming SELECT ... ORDER BY symbol, ts
      ▼
learn/kronos_dataset.py ──► ~/Kronos/finetuned/dataset/
      │                        features.f32   (N,6) float32, contiguous
      │                        days.i32       (N,)  int32  (date ordinal)
      │                        symbols.json   offsets + market + counts
      ▼
core/kronos_finetune.py
      ├── stage 1: _train_tokenizer  ──► ~/Kronos/finetuned/tokenizer/best_model
      └── stage 2: _train_predictor  ──► ~/Kronos/finetuned/predictor/best_model
      ▼
core/kronos_eval.py  ──► base vs fine-tuned walk-forward report (markdown + JSON)

upstream source:  ~/Kronos/  (git clone)      weights: ~/Kronos/weights/…
separate venv:    .venv-kronos  (torch cu128, einops, huggingface_hub, safetensors)
```

Nothing is written inside the repo: all checkpoints, datasets and run logs live under
`~/Kronos/`, which is never rsynced by `deploy_to_contabo.sh` and never committed.

## 5. Dataset design

The old finetuner ranked the top 1,500 liquid symbols and pickled a
`dict[symbol] -> DataFrame`. At 1,500 symbols that was fine (~200 MB). At **17,719 symbols
it is not** — and this is the main engineering change the plan makes:

| Problem at full universe | Consequence | Fix |
|---|---|---|
| `prepare_dataset` holds every frame in RAM to rank them | ~2.5–3 GB of DataFrames before training starts | Drop ranking; enumerate + stream in one ordered scan |
| `dict[str, DataFrame]` pickle (float64, DatetimeIndex) | ~2.5 GB pickle, doubled while `pickle.load` runs | One contiguous `float32` array + offset index |
| `_WindowDataset.indices` = `list[(symbol, i)]` | ~36M tuples ≈ **2.6–2.9 GB** for train alone | Per-symbol `window_count` + `np.searchsorted` cumulative offsets (~70 KB) |
| `minute`/`hour` columns for **daily** bars | Constant 0, pure waste | Derive `weekday`/`day`/`month` from a stored day-ordinal; drop minute/hour |
| `num_workers=0` + pandas `iloc` per sample | Python-bound step time | Pure-numpy `__getitem__`, raise `num_workers` |

This matters concretely: the box has 63 GB RAM but only **~20 GB available** right now,
because FreeToken's MoE expert offload consumes the rest. The old design would be
OOM-adjacent; the lean design is ~1.3 GB on disk and near-zero index overhead.

**Format** — three parallel artifacts under `~/Kronos/finetuned/dataset/`:

- `features.f32` — `float32[N, 6]`, columns `[open, high, low, close, volume, amount]`,
  all symbols concatenated in symbol order.
- `days.i32` — `int32[N]`, date ordinal per bar.
- `symbols.json` — per symbol: `{name, market, offset, count, val_offset, window_count}`.

**Split — chronological, per symbol, with an explicit cutoff.** The old code took the last
10% of each symbol as validation, which makes validation the *most recent* data and leaves
it ambiguous whether the walk-forward eval overlaps training. Fix: a single
`--train-cutoff` (default `2026-01-01`):

- Train: bars before the cutoff, first 90% of each symbol's pre-cutoff history.
- Validation: the last 10% of each symbol's pre-cutoff history.
- **Held out entirely:** every bar at/after the cutoff. Walk-forward evaluation runs
  strictly after the cutoff, so no scored window can have been in training. The old
  `scripts/kronos_1wk_test.py` carried a warning about exactly this contamination; the
  cutoff makes it structural instead of a comment.

**Filtering.** Keep `min_bars` (default 500) and add optional `--max-stale-days`
(default: off) to drop delisted names. Note that delisted tickers still carry valid
historical patterns, so the default is to keep them — the flag exists for ablation.

## 6. Training configuration

Ported hyperparameters, reconciled against upstream `finetune/config.py`:

| Parameter | This plan | Upstream | Note |
|---|---|---|---|
| `lookback_window` | 256 | 90 | Repo used 256; ≤ `max_context` 512 |
| `predict_window` | 10 | 10 | Trading days predicted per window |
| `clip` | 5.0 | 5.0 | Normalization clamp |
| tokenizer LR | 2e-4 | 2e-4 | AdamW, wd 0.1, OneCycleLR |
| predictor LR | 4e-5 | 4e-5 | AdamW betas (0.9, 0.95), wd 0.1 |
| grad clip | 2.0 / 3.0 | — | Tokenizer / predictor |
| steps per epoch | **2,000** | 2,000 | `n_iter = 2000 × batch_size` |
| val steps | 400 | 400 | `n_iter = 400 × batch_size` |
| `batch_size` | 16 | 50 | 16 sized for co-tenancy with FreeToken |
| `epochs` | 10 (first pass) | 30 | See budget note below |
| `seed` | 42 | 100 | |

**The epoch budget is the real limit, not the universe size.** `steps_per_epoch` is fixed
at 2,000 by `n_iter = 2000 × batch_size` — so enlarging the universe from 1,500 to 17,719
symbols changes *which* windows are sampled, not how many. The available pool grows to
≈40M windows, of which one 10-epoch pass consumes:

```
10 epochs × 2,000 steps × batch 16 = 320,000 window draws  ≈ 0.8% of the 40M pool
```

So "train on all 17.7k symbols" buys **sampling diversity**, and if the first pass looks
promising the lever is `--epochs` (30 → ~2.4% coverage), not the symbol list. This should
be stated plainly in the doc rather than implied by the word "all".

## 7. Time budget

Per-step time is the only unknown; everything else is arithmetic. `steps_per_epoch` is
fixed at 2,000, so **epoch wall clock = 2,000 × per-step seconds**, and total training time
scales linearly with `--epochs`.

Estimated per-step on the RTX 5080 **while FreeToken is serving** (102.3M params,
batch 16, seq ≤267, plus a `no_grad` tokenizer encode per step): **0.10–0.35 s**, i.e.
**3.3–11.7 min/epoch**.

| Stage | Work | Estimate |
|---|---|---|
| **P0** Environment | Dedicated venv, CUDA torch (~3 GB), clone Kronos, fetch weights (~0.5 GB) | 15–45 min (network-bound) |
| **P1** Dataset build | 44.6M bars → stream, frame, split, write ~1.3 GB | 10–30 min |
| **P2** Tokenizer fine-tune | 10 epochs × 2,000 steps | 35 min – 2.0 h |
| **P3** Predictor fine-tune | 10 epochs × 2,000 steps, 102.3M params | 40 min – 2.0 h |
| **P4** Evaluation | Walk-forward, base **and** fine-tuned, 50–200 symbols, `sample_count=3` | 30 min – 1.5 h |
| **First pass total** | P0–P4 | **≈ 2.5 – 6 h** |
| **At upstream's 30 epochs** | P2+P3 ×3 | **≈ 6 – 14 h** |

**Calibrate before committing to the estimate.** P2 begins with a
`--smoke-steps 100` run that measures real per-step time and prints a projected epoch and
total wall clock. The table above is a planning range; the smoke measurement is the number
to publish in the run log. If the measured per-step time is >0.35 s, stop FreeToken for the
training window (it is a 16 GB card and the Qwen MoE holds ~7.8 GiB of it).

**Run detached and resumable.** A multi-hour job must not die with the SSH session:

```bash
systemd-run --user --unit=kronos-finetune --collect \
  .venv-kronos/bin/python scripts/kronos_finetune.py --all-symbols --epochs 10
journalctl --user -u kronos-finetune -f
```

Transient unit (no boot persistence needed for a one-off), survives disconnects.
`--resume` picks up from the last per-epoch checkpoint; the best-on-val checkpointing
already in the ported loops is retained.

## 8. Environment: why a separate venv

`requirements.txt` is deliberately torch-free and `docs/laya.md` states the rule: the web
service must stay torch-free and a routine deploy must not pull gigabytes. Upstream Kronos
also **pins `pandas==2.2.2`**, while this repo's `.venv` runs **pandas 3.0.6** — installing
Kronos deps into the app venv would fight that pin.

So: a dedicated **`.venv-kronos`**, mirroring the existing `.venv-browseruse` precedent.

`requirements-kronos.txt` (new, repo root):

```
# Fine-tuning env only — never installed by install.sh or any deploy script.
torch>=2.10          # from https://download.pytorch.org/whl/cu128
einops==0.8.1        # upstream pins
huggingface_hub==0.33.1
safetensors==0.6.2
tqdm>=4.67.1
# pandas/numpy come from the app's pins; do NOT pin upstream's pandas==2.2.2 here.
```

**Blackwell gotcha.** `sm_120` (RTX 5080) needs a **cu128 or newer** wheel — cu126 and
older produce the classic *"sm_120 is not compatible with the current PyTorch
installation"* failure. And this box runs **Python 3.14.4**, whose CUDA wheels only arrive
with **PyTorch 2.10+**. Install with:

```bash
python3 -m venv .venv-kronos
.venv-kronos/bin/pip install -r requirements-kronos.txt \
  --index-url https://download.pytorch.org/whl/cu128
```

If no `cp314` CUDA wheel is published for the needed torch version, create `.venv-kronos`
on **Python 3.12** instead — the venv is disposable and isolated, so the interpreter
version is free.

## 9. Work packages

- [ ] **P0-01** Add `requirements-kronos.txt`; create `.venv-kronos` on Python 3.14 (or 3.12
      fallback); verify `torch.cuda.is_available()` is `True` and
      `torch.cuda.get_device_capability()` reports `(12, 0)`.
- [ ] **P0-02** `git clone https://github.com/shiyu-coder/Kronos.git ~/Kronos`; record the commit SHA.
- [ ] **P0-03** Fetch and cache weights to `~/Kronos/weights/Kronos-base` and
      `~/Kronos/weights/Kronos-Tokenizer-base` via `from_pretrained(...).save_pretrained(...)`
      (`token=False`); verify by loading both offline.
- [ ] **P1-01** Write `learn/kronos_dataset.py`: single ordered scan of `daily_bars`
      (streamed, not 17,719 round-trips), `amount = volume × mean(OHLC)`, per-symbol
      chronological split at `--train-cutoff`, emit `features.f32` / `days.i32` /
      `symbols.json`.
- [ ] **P1-02** Report the built corpus (symbols kept, bars, US/PH split, train/val sizes)
      to a `build_report.json` next to the dataset.
- [ ] **P2-01** Restore `core/kronos_finetune.py` from `89240d9^`; rewire it to the lean
      dataset, drop `_rank_liquid_symbols` from the default path, add `--all-symbols`,
      `--smoke-steps`, `--resume`.
- [ ] **P2-02** Add `scripts/kronos_finetune.py` — argparse CLI following
      `scripts/build_barcache.py` conventions (`description=__doc__`,
      `raise SystemExit(main())`, repo-root `sys.path` bootstrap). `main.py` is left untouched.
- [ ] **P2-03** Run the 100-step smoke benchmark; record measured per-step time and the
      projected epoch/total wall clock.
- [ ] **P2-04** Train tokenizer → `~/Kronos/finetuned/tokenizer/best_model`.
- [ ] **P3-01** Train predictor → `~/Kronos/finetuned/predictor/best_model`.
- [ ] **P4-01** Restore `core/kronos_eval.py` from `89240d9^`. It needs only **two** settings
      — `settings.kronos_batch_size` (`:192`) and `settings.kronos_min_move_pct` (`:723`) —
      so add just those two to `config.py` and document them in `.env.example`. Keep the
      rest of the runtime surface deleted.
- [ ] **P4-02** Add `scripts/kronos_eval_test.py` (adapted from the deleted
      `scripts/kronos_1wk_test.py`) to score base vs. fine-tuned walk-forward.
- [ ] **P4-03** Run base and fine-tuned scoring over the same windows, strictly after
      `--train-cutoff`; write the comparison report.
- [ ] **P5-01** Restore `core/test_kronos_eval_scoring.py`; add `core/test_kronos_finetune.py`
      covering split correctness, no-lookahead, and normalization.
- [ ] **P6-01** Commit this document once the campaign completes, replacing the estimates
      with measured numbers.

## 10. Verification

Unit tests (CPU only, no GPU, no weights — must pass in CI):

```bash
.venv/bin/python -m pytest -q core/test_kronos_finetune.py core/test_kronos_eval_scoring.py
```

Dataset correctness — the checks that matter:

- **No lookahead.** For every symbol, `max(train date) < min(val date) < min(eval date)`,
  and no window's `predict` span crosses the cutoff.
- **Split ratio.** Val is 10% ±1% of each symbol's pre-cutoff bars.
- **Normalization.** A window's z-scores are computed from the lookback slice only —
  assert that mutating a bar *after* the lookback does not change the returned input.
- **Determinism.** Same seed ⇒ identical `(symbol, start)` draw sequence.

End-to-end acceptance, run on the GPU:

```bash
.venv-kronos/bin/python scripts/kronos_finetune.py --all-symbols --epochs 10 --smoke-steps 100
.venv-kronos/bin/python scripts/kronos_eval_test.py --use-finetuned --symbols 50 --liquid-only
.venv-kronos/bin/python scripts/kronos_eval_test.py --symbols 50 --liquid-only   # base
```

The fine-tune is accepted when, on the same post-cutoff windows, the fine-tuned model
**beats base** on the metrics the scorer already reports — +1d/+3d direction hit rate and
MAE vs. the flat/persistence baseline — with bootstrap CIs that exclude zero difference.
Report the raw numbers either way; a fine-tune that loses to base is a valid, useful result.

Also confirm the app is untouched: `.venv/bin/python -m pytest -q mcp_server core web` must
pass as before, and `pip list` in `.venv` must still show no torch.

## 11. Risks and mitigations

| Risk | Mitigation |
|---|---|
| No `cp314` CUDA wheel for the needed torch | Fall back to a Python 3.12 `.venv-kronos` — it is disposable |
| Older wheel ⇒ `sm_120 is not compatible` | Force `--index-url .../cu128` (or newer); assert `(12, 0)` in P0-01 |
| OOM while FreeToken holds ~40 GB RAM | Lean `float32` corpus (~1.3 GB), offset-based sampler (~70 KB), not the old pickles |
| Contended GPU stretches epochs | Smoke-benchmark first; if >0.35 s/step, stop FreeToken for the run |
| Training dies with the SSH session | `systemd-run --user` transient unit + `--resume` |
| Evaluation leaks training data | Structural `--train-cutoff`; all scored windows strictly after it |
| Upstream `pandas==2.2.2` vs app's 3.0.6 | Separate venv; do not pin upstream's pandas |
| Delisted/OTC tickers pollute the corpus | `--max-stale-days` ablation switch; default keeps them |
| Kronos deps leak into the deploy | `requirements-kronos.txt` referenced by nothing in `install.sh`/deploy scripts |

## 12. Out of scope (named follow-ups)

1. **Runtime restore** — `kronos_forecast.py`, `kronos_gate.py`, `kronos_rank_sleeve.py`,
   the `KRONOS_*` settings block, `main.py --kronos-*` flags, `/api/kronos/predict`,
   the `/kronos` tab, and the `ENABLE_KRONOS` gate.
2. **The dangling MCP tool** — `mcp_server/tools_kronos.py` currently proxies a route that
   no longer exists. Either remove it or restore the route; do not leave it silently
   404-ing. (Found during this audit.)
3. **Using the fine-tune in the bot** — pointing a live gate at
   `~/Kronos/finetuned/predictor/best_model` requires item 1 first.
