# Kronos 3-prediction, 1-day entry confirmation

Status: implemented. Last revised 2026-11-02.

Every trade entry is preceded by **3 independent one-day Kronos forecasts**. The trade
opens only when a majority of those forecasts point the signal's way: Kronos is a
confirmation step made before the entry, not a post-hoc filter and not a pivot-only
tie-breaker.

## 1. What this adds

A new optional scanner gate, `kronos_gate_mode = off | shadow | confirm`:

1. A pattern signal reaches `_finish_signal` on a completed daily bar and clears the
   existing entry gates (volume, rule filter, vision, Laya).
2. Before the entry is queued — and therefore before the next session's open — `.venv-kronos`
   runs **3 independent 1-day forecasts** for that symbol.
3. Each simulated close is classified `up`, `down`, or `sideways` against an ATR-scaled band.
4. The vote is compared with the signal's direction. In `shadow` mode it is recorded on the
   signal and never blocks. In `confirm` mode the entry is queued **only when the vote
   confirms**; both `oppose` and `no_opinion` block it.
5. Blocked signals go to the signal log with a `kronos` rejection bucket. Confirmed signals
   carry the evidence dict.

The gate applies to **every surviving entry signal**. There is no pivot, sector, regime, or
confidence precondition that skips the prediction; the only thing that skips it is an earlier
gate rejecting the signal before it reaches this step.

## 2. Why one day at a time

Kronos already has three horizons in this repo, and each answers a different question:

| Path | Horizon | Question it answers |
|---|---|---|
| `scripts/kronos_web_forecast.py` (`--days`) | 1–60 days | "What does the path look like?" |
| `core/kronos_experiment.py` (`HORIZONS = (1, 5)`) | 1 and 5 days | "How close is the predicted close?" |
| `core/kronos_eval.py` (`GATE_HORIZON_BARS = 3`) | 3 days | "Is the regime rule's 3-day call right?" |
| **this gate** | **1 day** | **"Does the next session confirm the setup before entry?"** |

A swing entry is decided on the next session's open and judged on the next session's close. A
3-day forecast folds in drift the trade will never see before its first stop or target level
is touched, so it mixes two decisions. One simulation = one bar = one vote.

## 3. Direction classes and the sideways band

Raw Sunday-night noise must not count as a direction, so "sideways" is not a zero-return
test — it is a volatility-scaled dead zone:

```
band_pct = clamp(k * ATR%(14), min_pct, max_pct)
ret      = (predicted_close / anchor_close) - 1
```

| `ret` | Class |
|---|---|
| `ret >= +band` | `up` |
| `ret <= -band` | `down` |
| otherwise | `flat` (sideways) |

Defaults: `k = 0.35`, `min_pct = 0.25`, `max_pct = 3.0`, ATR period 14, computed from the
same completed bars the signal is judged on (`indicators/_math.wilder_atr`, divided by the
anchor close).

Worked example — ATR% = 1.60, anchor close 100.00:

```
band = clamp(0.35 * 1.60, 0.25, 3.00) = 0.56%
up     if predicted >= 100.56
down   if predicted <=  99.44
flat   between
```

The clamps exist for the two pathological ends: a 0.05% ATR name would otherwise call every
tick a direction, and a 9% ATR name would call a 2% move sideways. ATR-normalising means the
same `k` is meaningful on a PHP bank and on a US mega-cap.

## 4. Confirmation rule

Module: `core/kronos_confirm.py` — pure functions plus a thin wrapper, in the shape
of `core/kronos_regime_gate.py` (no torch at import, verdict injectable in tests).

```python
@dataclass(frozen=True)
class ConfirmSummary:
    anchor_close: float
    band_pct: float
    returns: tuple[float, ...]      # one 1-day return per simulation
    up: int
    down: int
    flat: int
    mean_return: float

@dataclass(frozen=True)
class ConfirmVerdict:
    decision: Literal["confirm", "oppose", "no_opinion"]
    reason: str

def evaluate(summary, action, *, min_agree: int = 2) -> ConfirmVerdict
```

An entry opens only when `decision == "confirm"`. Decision rules (`min_agree = 2` with the
default 3 samples, BUY shown; SELL mirrors it):

| Votes (BUY signal) | Decision | `confirm` mode |
|---|---|---|
| `up, up, down` | `confirm` | enter |
| `up, up, flat` | `confirm` | enter |
| `down, down, up` | `oppose` | **block** |
| `down, down, flat` | `oppose` | **block** |
| `up, flat, down` | `no_opinion` | **block** |
| `up, flat, flat` | `no_opinion` | **block** |
| `flat, flat, flat` | `no_opinion` | **block** |

- `confirm` iff votes for the signal's direction ≥ `min_agree`.
- `oppose` iff votes against it ≥ `min_agree`.
- Everything else is `no_opinion`.
- In `confirm` mode both `oppose` and `no_opinion` block. They are counted separately so the
  report can distinguish "the model disagreed" from "the model had nothing to say".
- In `shadow` mode nothing blocks; the decision and evidence are stamped and the entry
  proceeds.

This is intentionally stricter than `core/kronos_regime_gate.py` §13 ("absence of edge is not
evidence — never veto on it"). That rule was written for an optional veto filter. Here the
model is the confirmation the trade needs, so an unconfirmed trade is not taken. The
distinction matters and should not be copied from the old gate by accident.

A small sample cannot estimate a probability. With 3 simulations the honest output is
"confirms / disagrees / nothing to say", not "62% confident".

## 5. Runtime path

The app venv is torch-free by design, so the model is reached exactly the way the Kronos tab
reaches it: a subprocess in `.venv-kronos`.

| File | Role |
|---|---|
| `scripts/kronos_confirm_worker.py` | GPU worker: load **one** model, run N × `predict(pred_len=1, sample_count=1)` for the requested symbols |
| `core/kronos_confirm.py` | band, classification, verdict, pending batch, TTL cache, subprocess call, evidence dict |
| `core/kronos_process.py` | shared `VENV_PYTHON`, `available()`, `run_worker()`, and the **GPU lock** — extracted from `web/kronos.py` so the tab and the scanner share one lock |
| `core/scanner.py` | collect pending entries in `_finish_signal`; one `_flush_kronos_confirm()` per scan |
| `scripts/kronos_confirm_report.py` | shadow-mode measurement |

Order inside `_finish_signal` (after the Laya check, immediately before the paper entry block
at `core/scanner.py:1647`):

```
mode == off                      -> queue the entry directly (no import, no cost)
append (signal, candle) to _kronos_pending, deduped by (symbol, anchor_date)
...
scan end / batch full            -> _flush_kronos_confirm()
  resolve cache hits
  one subprocess for the remaining unique symbols
  shadow:  stamp evidence, queue every pending entry
  confirm: queue only decision == "confirm"; log-reject the rest under "kronos"
```

Deferring the entry to the flush does not delay the trade: paper entries already fill at the
next session's open (`core/paper_books.py`), not on the signal bar. The flush point is the
same place `_flush_collect_first` already runs, so the scanner gains one more end-of-scan
phase rather than a per-signal pause.

**Batching is the design, not an optimisation.** Because every entry is confirmed, a
per-signal subprocess would reload the model tens of times per scan. One worker call per
flush loads the model once and runs all pending symbols in that process.

Cost controls, in order:

1. **Late placement first.** Only signals that cleared volume, rule, vision, and Laya reach
   the pending buffer. Rejected signals never pay for a prediction.
2. **One batched worker call per flush.** The model load dominates the per-call cost
   (seconds on the GPU host, tens of seconds on CPU), not the N predictions or the number of
   symbols in the batch.
3. **TTL cache** keyed on `(symbol, anchor_date, model, samples)`, default 30 minutes. Several
   patterns firing on the same symbol bar share one run.
4. **`kronos_gate_max_checks_per_scan`** (default 100 unique symbols) — a hard circuit breaker
   so a signal-heavy replay cannot stall the scan loop. Symbols over the cap get
   `no_opinion` and therefore block in `confirm` mode.

Sample independence: `predict()` returns a single aggregated DataFrame even when
`sample_count > 1`, so N votes require N calls. Each call is seeded from
`(symbol, anchor_date, sample_index)` via `torch.manual_seed` so a rerun is reproducible and
the cache is meaningful. If the upstream predictor exposes its own seed kwarg, prefer it; the
design does not depend on which.

The worker loads `finetuned` by default and never both models — that is why this is a
separate script rather than a flag on `scripts/kronos_web_forecast.py`, which always loads
base **and** fine-tuned.

### Device selection

`KRONOS_DEVICE` (`auto` by default) resolves once per worker, in
`core.kronos_eval.select_device`:

| Value | Behaviour |
|---|---|
| `auto` | CUDA when it is available **and** has ≥ 2 GiB free, otherwise CPU |
| `cuda` | Always CUDA; a load failure surfaces as a worker error |
| `cpu` | Always CPU |

`torch.cuda.is_available()` alone is not enough: this host runs a local
`llama.cpp` server that can hold ~15 GB of a 16 GB card, so a "CUDA is
available" check loads the model straight into an out-of-memory error and the
gate would then block every entry (fail-closed). `auto` inspects
`torch.cuda.mem_get_info()` first and, as a second line of defence, retries the
load on CPU if CUDA still OOMs. The device actually used is logged and written
to the worker payload; `kronos_evidence["device"]` records it on the signal.

`core/kronos_process.run_worker` forwards `settings.kronos_device` to the
subprocess as `KRONOS_DEVICE`, because the worker does not read `.env` itself.

### Failure behaviour

| Condition | Result in `shadow` | Result in `confirm` |
|---|---|---|
| Mode `off` | Gate absent. Zero imports, zero subprocesses. | same |
| `.venv-kronos` or worker missing | `no_opinion`, entry still opens | `no_opinion`, **entry blocked** |
| GPU lock busy (web forecast / cron running) | `no_opinion`, entry still opens | `no_opinion`, **entry blocked** |
| Timeout (`kronos_gate_timeout_s`) | `no_opinion`, entry still opens | `no_opinion`, **entry blocked** |
| Worker error / malformed payload | `no_opinion`, entry still opens | `no_opinion`, **entry blocked** |
| Fewer usable simulations than `min_agree` | `no_opinion`, entry still opens | `no_opinion`, **entry blocked** |
| Over the per-scan symbol cap | `no_opinion`, entry still opens | `no_opinion`, **entry blocked** |

The gate **fails closed in `confirm` mode**: no confirmation means no trade. That is the
direct consequence of treating Kronos as a required confirmation, and it is why `shadow` is
the only safe first rollout — a broken worker cannot stop trading there. Position management
and `CLOSE`/exit handling are unaffected in every mode.

## 6. Configuration

| Setting | Env var | Default | Meaning |
|---|---|---|---|
| `kronos_gate_mode` | `KRONOS_GATE_MODE` | `off` | `off` / `shadow` / `confirm` |
| `kronos_gate_samples` | `KRONOS_GATE_SAMPLES` | `3` | Simulations per symbol (odd, 1–9) |
| `kronos_gate_model` | `KRONOS_GATE_MODEL` | `finetuned` | `base` or `finetuned` |
| `kronos_device` | `KRONOS_DEVICE` | `auto` | `auto` / `cuda` / `cpu` — see §5 |
| `kronos_gate_sideways_atr` | `KRONOS_GATE_SIDEWAYS_ATR` | `0.35` | `k` in `band = k × ATR%` |
| `kronos_gate_sideways_min_pct` | `KRONOS_GATE_SIDEWAYS_MIN_PCT` | `0.25` | Floor for the sideways band (%) |
| `kronos_gate_sideways_max_pct` | `KRONOS_GATE_SIDEWAYS_MAX_PCT` | `3.0` | Cap for the sideways band (%) |
| `kronos_gate_min_agree` | `KRONOS_GATE_MIN_AGREE` | `2` | Votes needed to confirm or oppose |
| `kronos_gate_cache_minutes` | `KRONOS_GATE_CACHE_MINUTES` | `30` | TTL of the per-symbol vote cache |
| `kronos_gate_timeout_s` | `KRONOS_GATE_TIMEOUT_S` | `180` | Subprocess timeout |
| `kronos_gate_max_checks_per_scan` | `KRONOS_GATE_MAX_CHECKS_PER_SCAN` | `100` | Unique symbols per flush |

The earlier draft's `kronos_gate_pivot_atr` and `kronos_gate_veto_sideways` are gone: there is
no pivot precondition any more, and `no_opinion` already blocks in `confirm` mode.

CLI: `--kronos-gate-mode {off,shadow,confirm}` and `--kronos-gate-samples N`, wired into both
`MarketScanner(...)` constructions in `main.py`. Web paper scans pass it through
`web/app.py::PaperStartRequest` → `core/paper_books.py` → `MarketScanner`.

Existing behaviour is untouched: the default is `off`, and `shadow` is the intended first
rollout.

## 7. Evidence and measurement

The verdict is stamped on the signal as a new `TradeSignal.kronos_evidence` field, next to
`laya_assessment` and `rule_evidence`:

```json
{
  "anchor_close": 128.1, "atr_pct": 1.6, "band_pct": 0.56,
  "returns": [0.0104, -0.0088, 0.0016],
  "votes": {"up": 1, "down": 1, "flat": 1},
  "decision": "no_opinion",
  "reason": "no majority of 2 in either direction",
  "mode": "confirm",
  "model": "finetuned",
  "samples": 3,
  "cached": false
}
```

`core/execution.py::opening_candidate` builds the fill with `replace(signal, price=price)`, so
the field travels to the paper position and export without extra plumbing; only
`utils/trade_export.py` needs the same additive lines it already has for `laya_assessment`.

`_append_signal_log` also copies it into the row (the JSONL log is schemaless), and the
rejection classifier gets a `kronos` bucket so `rejection_by_gate` stays readable.

`scripts/kronos_confirm_report.py` reads `logs/paper_signals_{us,ph}.jsonl` and groups rows
that carry `kronos_evidence` by decision, printing counts, win rate, and average R per
bucket — the same column set as `analysis/price_volume.ab_metrics_from_result`. In shadow
mode every row has an outcome; in `confirm` mode the blocked buckets have no fills, so the
report must use shadow data for calibration.

What to look for before promoting shadow to confirm:

- `confirm` and non-`confirm` buckets each have enough rows to mean something (dozens, not
  two).
- Confirmed entries win materially more often than the blocked ones, or lose R faster when
  blocked.
- The split between `oppose` and `no_opinion` is not hiding the result: if only `oppose` is
  predictive and `no_opinion` is a coin flip, a confirmation gate that blocks both is
  throwing away trades for nothing.
- The gate is not concentrating on one sector or one market.

If the buckets look identical, set `kronos_gate_mode = off` and stop — the model has no
1-day edge worth requiring, which is a legitimate outcome and cheaper than blocking every
trade the model is unsure about.

## 8. What this does not do

- **No backtest.** `core/backtest_params.py:220` rejects `kronos_gate` and the rejection
  stays. Historical gating would require a walk-forward predictor at each asof
  (`core/kronos_eval.run_kronos_test` shape), which is a research job, not a per-bar feature.
  Shadow-mode paper is the measurement path.
- **No conditional scoping.** Every entry is confirmed. Pivot levels, regime, sector, and
  pattern confidence are not preconditions; the previous pivot pre-test is removed. Cost is
  managed by batching, not by deciding which entries deserve a prediction.
- **No 3-day or 5-day horizon.** The gate is 1 day by definition; the 3-day regime rule in
  `core/kronos_regime_gate.py` remains separate and unused.
- **No per-sample path API.** `sample_count` is not exposed as N paths here, and this design
  does not change the upstream predictor.
- **No probability, no sizing.** The gate can block; it cannot enlarge or shrink a position.
- **No entry verdict on `CLOSE` signals.** `CLOSE` is not an entry; open-position
  protection is a separate exit vote (§11).

## 9. Tests

Pure logic and wiring, no GPU and no database:

```bash
.venv/bin/pytest core/test_kronos_confirm.py -q
.venv/bin/pytest core/test_scanner_kronos_confirm.py -q
.venv/bin/pytest -m "not slow" -q
```

- `core/test_kronos_confirm.py` — band clamps at both ends; `classify` at exact boundaries;
  vote tallies; the full 3-vote truth table including `min_agree` ties; cache hit/miss;
  unavailable → `no_opinion`.
- `core/test_scanner_kronos_confirm.py` — modelled on `core/test_scanner_dii_gate.py`:
  mocked store, `votes_fn` injected. Asserts shadow never blocks and stamps evidence;
  `confirm` queues only `confirm`; `oppose` and `no_opinion` both block; unavailable and
  timeout block in `confirm` but not in `shadow`; `off` never constructs the gate; N pending
  signals produce one worker call.

GPU worker, on the GPU host:

```bash
.venv-kronos/bin/python scripts/kronos_confirm_worker.py \
    --symbols AAPL MSFT --samples 3 --model finetuned --out /tmp/confirm.json
```

The three values in each symbol's `predicted` must differ. If they are identical, the
predictor is deterministic at `sample_count=1` and the seeding has to change before the gate
means anything.

## 10. Limitations and next steps

- **N = 3 is a small sample.** Raising to 5 costs ~1.7× predict time and no extra model load.
  Do not read a 2–1 split as an edge; read it as a weak confirmation.
- **One batch per scan.** A signal confirmed at the flush is confirmed on the frame available
  at request time; no extra bars are fetched. If a scan produces signals over several minutes,
  they are confirmed together at the end, not one by one.
- **Batching adds a scan-end phase.** If the flush is slow, entries queue later but still fill
  at the next open. Keep the worker timeout below the scan's cadence.
- **Cache is in-process.** A restart mid-session re-runs the gate for signals seen before it.
  Correct, just not free.
- **A broken worker stops entries in `confirm` mode.** Monitor `kronos` rejections; keep a
  health check (`available()`) or run `shadow` if the GPU host is shared with the web
  forecast and cron jobs.
- **Not wired into the web Scanner form defaults.** The API accepts the mode, but the form
  keeps `off` until shadow results justify exposing it.

## 11. Open-position protection

Open positions are predicted by the same one-day vote on every new bar:
`kronos_exit_mode = off | shadow | exit` (`KRONOS_EXIT_MODE`, default `exit`).

1. After a symbol's new bar is applied and its exits checked, `_queue_kronos_exits` adds the
   symbol's open positions to the same pending batch as any entry signals.
2. The flush is shared: one worker call and one TTL cache entry cover both a new entry and an
   open position on the same `(symbol, anchor_date)`.
3. A `BUY` position is opposed when a majority predicts `down`; a `SELL` position is opposed
   when a majority predicts `up`. `confirm` and `no_opinion` hold.
4. In `exit` mode an opposed position gets `pending_exit_reason = "kronos_exit"`, so the next
   observed bar fills the close at its open — the same next-open contract as a pending entry.
   In `shadow` mode the verdict is stamped on `position.kronos_exit_evidence` and nothing
   changes. Worker failure or the per-scan cap behaves as `no_opinion`: hold.

The exit vote uses `KRONOS_EXIT_MIN_AGREE` (default 2) and the same samples, model, sideways
band, cache, timeout, and GPU lock as the entry gate. `confirm`/`no_opinion` never force an
exit; only an active disagreement does.
