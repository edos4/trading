# Plan: Collect-first, rank-by-R:R, open top-4

## Goal

Add an opt-in mode to the scanner where a full scan pass **collects chart-pattern signals without opening anything**, then **ranks them by reward:risk ratio (R:R)**, keeps the **top N (default 4)**, and only those get queued for the existing one-bar deferred fill. Expensive gates (Kronos/volume/vision) run only on the winners.

## Current behavior (reference)

`MarketScanner._scan_all` (core/scanner.py:434) → per-symbol `pattern.analyze()` → `_process_signal` (core/scanner.py:718) runs gates inline and immediately queues into `_pending_entries` via `_finish_signal` (core/scanner.py:880). Fill happens on the next new bar (core/scanner.py:575-594). R:R is computed inside `describe_risk_gate_rejection` (core/backtester.py:1090-1107) *after* stop backstops mutate `stop_loss`.

## Design decisions (confirmed)

1. **Ranking metric** = R:R: `reward = |take_profit − price|`, `risk = |price − stop_loss|`, `rr = reward/risk`. Computed *after* the risk-gate stop backstops finalize `stop_loss`.
2. **Opt-in** = new `.env` settings, default off, so existing runs are unchanged.
3. **Scope** = chart-pattern signals only (`not is_kronos_rank_signal(signal)`); the Kronos rank sleeve keeps its own top-K path.

## Changes

### 1. `config.py` — new settings

```python
collect_first_enabled: bool = False   # collect-then-rank-then-top-N (chart patterns)
collect_first_top_n: int = 4          # how many winners to open per scan
```

Add a validator clamping `collect_first_top_n` to `>= 1`. Document in `.env.example` (`COLLECT_FIRST_ENABLED`, `COLLECT_FIRST_TOP_N`).

### 2. `core/engine_defaults.py` — R:R helper

Add a pure helper (shared money-path concept):

```python
def signal_reward_risk(signal: TradeSignal) -> float | None:
    """reward/risk from price/stop/target; None if not computable."""
```

Returns `None` when `price <= 0`, `stop_loss is None`, `take_profit is None`, or `risk <= 0`.

### 3. `core/scanner.py` — the core change

- `__init__` params: `collect_first: bool | None = None`, `collect_first_top_n: int | None = None` → resolve from `settings` when `None`; store `self._collect_first`, `self._collect_first_top_n`.
- New state `self._collect_pool: list[tuple[TradeSignal, BasePattern | None, OHLCVCandle]]`, reset at top of each `_scan_all`.
- **`_process_signal`**: when `self._collect_first` and `not is_kronos_rank_signal(signal)`, after the cheap gates (min-price, cooldown, confidence, regime, long-only) **and** the risk-gate step (which mutates `stop_loss`), append `(signal, pattern, candle)` to `_collect_pool` and return — do **not** run Kronos/volume/vision/queue. The non-collect-first path (and rank-sleeve signals) stays exactly as today.
- **`_flush_collect_first()`** (new), called in `_scan_all` right after `asyncio.gather(*workers)` (core/scanner.py:631), before the `_kronos_batch` flush:
  1. Dedupe the pool by `(symbol, timeframe)`, keeping the highest R:R signal per key.
  2. Sort by `(-rr, -confidence, symbol)`, `None` rr sorting last.
  3. Take top `self._collect_first_top_n`.
  4. For each winner, run the Kronos 3d gate (if enabled) then `_finish_signal` (volume → vision → `_pending_entries`), reusing the existing code path (factor the tail of `_process_signal` at core/scanner.py:831-842 into a small helper `_kronos_then_finish` used by both paths).
  5. Log each non-winner as `status="rejected"` with reason `collect-first: R:R ranked below top {N}`; increment `signals_rejected` and `rejection_by_gate["collect_first"]`.
  6. Add a `"collect_first_selected"` / `"collect_first_ranked"` counter to `stats`.

### 4. `main.py` — CLI wiring

- `run_scanner` / `run_paper`: pass `collect_first=settings.collect_first_enabled`, `collect_first_top_n=settings.collect_first_top_n` into `MarketScanner(...)`.
- Add `--collect-first` (store_true) and `--collect-first-top-n N` (int) flags; thread them into `run_paper`/`run_scanner`. Log the mode in the startup banner.

### 5. Web / UI wiring (match existing `kronos_batch` pattern)

- `web/app.py` payload + `web/services.py` + `web/jobs.py`: add `collect_first` (bool) and `collect_first_top_n` (int) passthrough to `MarketScanner`.
- `ui/paper_dashboard.py` + `ui/backtest_dialog.py`: add a "Collect-first (top-N)" checkbox + spin (default off; disabled unless a real pattern path is active).

### 6. Tests (`core/test_scanner_collect_first.py`)

- `signal_reward_risk`: valid, `None` stop/target, zero risk → `None`.
- With `collect_first=True` and N patterns firing for multiple symbols: only the top-4 by R:R are queued into `_pending_entries`; the rest are logged rejected.
- Dedupe: two patterns firing the same `(symbol, timeframe)` → only the higher-R:R one survives.
- Cheap gates still run inline (a below-min-price signal never reaches the pool).
- Non-collect-first (`collect_first=False`) behaves exactly as before (existing `test_scanner_deferred_entry.py` still passes).

## Non-goals / out of scope

- Does **not** change backtest behavior or the Kronos rank sleeve.
- Does **not** remove the one-bar deferred fill (`_pending_entries`) — winners still fill on the next new bar.
- Does **not** change default behavior (off unless enabled).

## Verification

- `pytest core/test_scanner_collect_first.py core/test_scanner_deferred_entry.py -q`
- `python main.py --paper 50 --collect-first --collect-first-top-n 4` (paper stream) to confirm only ≤4 opens per scan and logs show ranking.
