# Kronos `predict_batch` — collect patterns, then batch forecast

**Verdict: yes, this is faster.** Official Kronos (`~/Kronos`, [shiyu-coder/Kronos](https://github.com/shiyu-coder/Kronos)) already has `KronosPredictor.predict_batch`. This bot never calls it. Every gate/sleeve/eval path uses single-series `predict()`.

Do not batch the UI/web one-symbol chart (`core/kronos_forecast.py`). That path is already one series.

---

## Why it is faster

Kronos decode is **autoregressive in time, batched across series**.

From `auto_regressive_inference` in `~/Kronos/model/kronos.py`:

1. Input `x` is `(B, T, F)`.
2. It is repeated by `sample_count` → effective GPU batch = `B * sample_count`.
3. Then a Python loop of `pred_len` transformer forwards (gate uses `GATE_HORIZON_BARS = 3`).

Today:

| Call | `B` (series) | `sample_count` | GPU batch | AR steps |
|------|--------------|----------------|-----------|----------|
| `predict()` per symbol | 1 | 3 | **3** | 3 |
| `predict_batch(N)` | N | 3 | **3N** | 3 |

The expensive work is those 3 transformer steps, not “number of tickers.” At GPU batch 3, Kronos-base (102M, context 400) is idle. One batched call of N series still does **3** AR steps, just with a fat batch.

Rough expectation on a CUDA box (Kronos-base, lookback 400, pred_len 3, sample_count 3):

| Workload | Today | After batch |
|----------|-------|-------------|
| Scanner gate, ~20 unique pattern hits / scan | 20 × ~0.5–2s, serialized under `kronos_infer_lock` | 1–2 chunked calls, ~3–8s total |
| Rank sleeve, 500–1500 symbols | 8–30+ min sequential | chunked `predict_batch`, tens of seconds |
| Pattern backtest | per-signal `predict()` inside per-symbol workers | **not the first target** (see below) |

CPU-only still benefits (fewer Python round-trips, better BLAS occupancy) but the big win is GPU.

Two extra wins that are not GPU-batch but belong in the same change:

- **Dedupe by symbol.** Gate forecast is per `(symbol, asof)`, not per pattern. AAPL double-bottom + hammer today can run Kronos twice. Batch path must forecast once and reuse.
- **Scan-then-gate.** Scanner currently calls `kronos_gate_check` inside `_process_signal` while other workers are still scanning. GPU is locked per hit. Collecting first lets pattern eval stay concurrent and Kronos run once at the end.

Tradeoff: first paper/live fill waits until the full symbol pass finishes. That is already true for rank sleeve, and matches the user’s requested flow. Document it; do not keep a “gate mid-scan for lower latency” split.

---

## Official batch contract (must honor)

`predict_batch(df_list, x_timestamp_list, y_timestamp_list, pred_len, ...)`:

- All series **same historical length** (lookback). Mixed lengths raise `ValueError`.
- All series **same `pred_len`**. Gate/sleeve already share `WEEK_AHEAD = 3`.
- Per-series mean/std normalization (independent). Same semantics as `predict()`.
- `volume` / `amount` optional; we already materialize amount via `with_amount()`.
- Local clone at `~/Kronos` already has `predict_batch` (line 562 of `model/kronos.py`). No extra install.

Implementation must **group by lookback length**, then **chunk** each group so `chunk * kronos_sample_count` does not OOM.

`use = min(lookback, len(df), MAX_CONTEXT)` is today’s per-symbol length. Do not silently left-pad. Bucket by `use` (typical buckets: 400 for liquid names, shorter for thin history). Drop `< 60` bars as today.

---

## Current bottlenecks (what to change)

```
scanner._process_signal
  → kronos_gate_check (one predict() under _INFER_LOCK)

kronos_rank_sleeve.forecast_universe / forecast_from_frames
  → for symbol in universe: predict_1w_return()   # worst sequential loop

backtester._backtest_symbol (ProcessPool per symbol)
  → kronos_gate_check per pattern hit              # cannot share one GPU batch across workers

kronos_eval._run_symbol / --kronos-test
  → predict() per walk-forward window              # secondary
```

Shared helper today: `predict_1w_return()` in `core/kronos_eval.py` — always `predictor.predict()`.

---

## Target flow

Collect-then-batch **only if "Batch Kronos" is checked** (default off).
Unchecked = today's per-signal `predict()` / `KronosGate.check()`.

The checkbox is hidden unless Kronos 3d gate (explorer) or gate/rank (paper, backtest) is on.

### A. Scanner confirm gate (primary — the requested design)

```
for each symbol (concurrent, unchanged):
    fetch snapshot, run patterns
    cheap gates: confidence, min price, regime, cooldown, long-only, risk
    if still alive and kronos_gate and not pattern_kronos_rank:
        append to pending_kronos[]   # do NOT call GPU yet
    else:
        continue existing pipeline (volume → vision → paper)

after all symbols drained:
    unique symbols in pending_kronos
    predict_1w_return_batch(...)
    for each signal: apply KronosGateResult (pass / veto / notes)
    survivors continue volume → vision → paper as today
```

Keep volume/vision **after** Kronos so we still do not burn Claude on vetoed forecasts.

Skip Kronos for `CLOSE`, non-`1d`, and `pattern_kronos_rank` (same as `KronosGate.check`).

### B. Rank sleeve (same primitive, larger speedup)

`forecast_universe` / `forecast_from_frames`: build the frame list, one `predict_1w_return_batch`, map back to `ForecastRow`. Rank/emit unchanged.

`backtest_rank_sleeve` already date-aligns the universe and calls `forecast_from_frames` every `rebalance` bars. That loop is the rank-sleeve BT cost. Batching it is in scope.

### C. Pattern backtest gate (defer)

Workers are `ProcessPoolExecutor` + spawn. Each worker that called Kronos would load its own copy of Kronos-base onto the GPU. Batching **across symbols** requires lifting Kronos out of the worker:

- workers emit un-gated pattern hits, **or**
- rewrite pattern BT as a date-aligned cross-section (like the rank sleeve).

That is a larger architecture change and can change BT wall-clock without changing live scan. **Out of v1.** Keep `kronos_gate_check` sequential in `_backtest_symbol`. Note in README that pattern-BT + Kronos gate is still the slow path.

### D. `--kronos-test` / eval (optional follow-up)

Same helper can batch windows that share lookback. Not required for the scanner win.

---

## API sketch

### `core/kronos_eval.py`

```python
def predict_1w_return_batch(
    predictor,
    frames: list[pd.DataFrame],
    *,
    sample_count: int = 1,
    lookback: int = LOOKBACK,
    batch_size: int | None = None,
) -> list[tuple[float, float] | None]:
    """Aligned with predict_1w_return. One output per input frame.

    Groups by seq_len, chunks by batch_size, calls predictor.predict_batch.
    Returns None for short/NaN/failed frames (fail-closed caller decides).
    """
```

Keep `predict_1w_return` as a thin wrapper (`batch` of one) so existing unit tests and single-symbol UI stay valid.

Fallback: if `predictor` has no `predict_batch` (old Kronos clone), loop `predict()`. Log once.

### `core/kronos_gate.py`

```python
def check_many(
    self,
    signals: list[TradeSignal],
    store: OHLCVStore,
    *,
    adjust_exits: bool | None = None,
) -> list[KronosGateResult]:
```

- One result per signal, same order.
- Load frames via `_facade_daily_df` / store (same as `check`).
- Dedupe GPU work by `symbol` (same asof in one scan).
- Single `with _INFER_LOCK` around the batch, not per symbol.
- `check()` stays sequential (`predict_1w_return`). Collect-then-batch is `check_many` only.

### `config.py` / `.env.example`

```
kronos_batch_enabled: bool = False   # UI/web "Batch Kronos" checkbox
kronos_batch_size: int = 16
```

GPU batch = `kronos_batch_size * kronos_sample_count` (default 48). 16 is conservative for Kronos-base @ context 400. Raise to 32/64 if VRAM allows.

Env: `KRONOS_BATCH_ENABLED`, `KRONOS_BATCH_SIZE`.

### Scanner

Split `_process_signal`: cheap gates stay inline. Sequential `kronos_gate_check` stays the default. Collect-then-batch + `_finish_signal` runs **only when Batch Kronos is on**.

Do not hold the GPU lock during pattern eval.

### Rank sleeve

Replace the `for symbol` / `for df` loops in `forecast_universe` and `forecast_from_frames` with one batch call **when `use_batch` is True**. Preserve skip-on-short-history behavior. Unchecked = sequential `predict_1w_return`.

---

## Correctness constraints

- **Same numbers as sequential** (up to sampling noise). `sample_count>1` is stochastic (`T=1.0`, `top_p=0.9`). Tests must use a fake predictor, not assert bit-identical GPU draws.
- **Fail-closed** unchanged: missing weights, predict exception, short history → reject unless `kronos_gate_fail_open`.
- **Lookback grouping** must not clip a 400-bar name down to a 80-bar neighbor in the same batch. Separate groups.
- **Empty batch:** `check_many([])` → `[]`. Rank sleeve with zero eligible frames → `[]` (today’s warning).
- **OOM:** catch CUDA OOM, split chunk in half, retry; if chunk size 1 still fails, that symbol is fail-closed. Log it.
- **Infer lock:** one lock around the whole batch. US + PH threads still must not overlap.

---

## Tests (no real weights)

Extend existing fakes (`core/test_kronos_gate.py` `_FakePredictor`):

1. Fake grows `predict_batch`; `check_many` calls it once for 3 symbols, 3 aligned results.
2. Two patterns same symbol → `predict_batch` length 1 (dedupe).
3. Mixed lookbacks (60 vs 400) → two `predict_batch` calls (or one per group).
4. Short frame → `None` slot, fail-closed result, other symbols still run.
5. Rank sleeve `forecast_from_frames` with fake: one batch, ranking unchanged (`test_kronos_rank_sleeve.py`).
6. Scanner unit/integration if one exists for `_process_signal`; otherwise a small helper test that “collect then finish” preserves reject reasons.

Do not require GPU in CI.

---

## Implementation order

1. `predict_1w_return_batch` + fake tests (group, chunk, None slots).
2. `KronosGate.check_many` + dedupe; make `check()` a wrapper.
3. Rank sleeve `forecast_*` → batch (biggest wall-clock win if sleeve is on).
4. Scanner two-phase collect → batch → finish.
5. Config `kronos_batch_size`, README note, `.env.example`.
6. Manual timing on this machine: 20-symbol gate batch vs loop; one rank-sleeve universe pass. Paste numbers into this file when done.

Out of v1: pattern-BT multiprocess Kronos, `--kronos-test` window batching, UI multi-symbol forecast.

---

## Files

| File | Change |
|------|--------|
| `core/kronos_eval.py` | add `predict_1w_return_batch` |
| `core/kronos_gate.py` | `check_many`; `check` wraps it |
| `core/kronos_rank_sleeve.py` | batch `forecast_universe` / `forecast_from_frames` |
| `core/scanner.py` | collect pattern hits, batch gate, then volume/vision |
| `config.py`, `.env.example` | `kronos_batch_size` |
| `core/test_kronos_gate.py` | batch/dedupe/lookback-group tests |
| `core/test_kronos_rank_sleeve.py` | batch forecast fake |
| `README.md` | one paragraph under Kronos confirm gate |

No change to `core/kronos_forecast.py` (single-symbol viewer).
