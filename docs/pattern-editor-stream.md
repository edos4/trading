# Isolated historical stream backtests (P06)

`core/stream_backtest.py` replays a frozen daily tape one simulated session at a
time for a pinned version set. It exists so an AI edit can be compared against
the base version "using historical streaming behavior shared with paper trading".

## Shared execution path (P06-01)

The replay does not implement trading. Every fill, quantity, exit and fee comes
from the same functions the paper account uses:

| Concern | Function (shared with `PaperAccount`) |
| --- | --- |
| Open a position / rebase stop+target | `core.backtester._open_trade` |
| Exit ladder (stop, target, reclaim, trailing, time) | `core.backtester._check_exit` |
| Close, P&L, cost deduction | `core.backtester._close_trade` |
| Neckline/prev-bar state | `core.backtester._update_neckline_state`, `_update_prev_hl` |
| Flat-notional sizing | `core.backtester._apply_notional_sizing` |
| Board-lot rounding (PH) | `core.market.apply_lot_rounding` |

Ordering per session mirrors `PaperAccount.on_bar` → `open_position`: the open
positions are managed against the bar first, then the bar's fresh signals are
drained and opened. `core/stream_backtest.py` is the only place that decides
*when* those shared functions are called; it never re-implements them.

## Isolation (P06-02)

Each `StreamReplay` owns its own candle stores, open/closed position lists,
simulated clock, dedup registry contents and artifacts. It never opens, reads or
writes a `PaperAccount` ledger and never contacts a stream server — the endpoint
assumption in `data/stream_server.py` is not used at all; the frozen dataset
blob recorded in `backtest_runs.payload` is the only input.

`patterns._dedup` is a process-global registry (the same reason the live scanner
runs detector analysis in spawned workers). Concurrent replays in **one process**
therefore serialize their analysis section behind `_ANALYZE_LOCK`; two
concurrent replays still produce exactly their sequential results, and a live
paper account nearby is untouched. True parallel throughput needs one process
per replay, which the durable job layer (P05) permits. Measured single-symbol
throughput: **260 sessions in 1.34 s (~194 sessions/s)** for one pattern on one
symbol; the cost is dominated by detector work, not the replay driver.

## Causal inputs, warmup and bounds (P06-03)

The candle store grows one bar per session and only ever contains bars at or
before the simulated timestamp, exactly like the live scanner's `copy_candles`
window. Sessions before `start_date` build indicators but cannot open trades.
Only `1d` is accepted; `ReplayWindow` enforces exactly one of end date / session
count, and the resolved window must overlap the frozen history.

## Session advancement, calendars and statuses (P06-04)

`_advance_session` processes every symbol that prints a bar on the current
session before the clock moves on. The calendar is the sorted union of each
symbol's session dates inside the window (warmup bars included). Explicit
statuses are recorded in `signals.events`: `duplicate-bar`, `unordered-bar`,
`history-ends-before-start`, `blocked`, `filtered`, `force-closed`, and
`end-of-data` when the requested end date is past the tape. A symbol with no
bar on a session is simply skipped; unequal histories are normal.

## Effective settings, end policy and metrics (P06-05)

The replay applies the frozen `ExecutionSettings`: capital, fixed-notional
sizing, transaction costs, slippage (through the shared fill math), the version
set, and the volume gate when enabled. `kronos_gate`, `kronos_rank`,
`collect_first` and non-fixed sizing modes are rejected at submission rather
than silently ignored.

End policy is explicit:

* `keep-open` (default, paper behavior) leaves positions open and reports
  realized **and** unrealized P&L, open-position count and per-position
  unrealized USD;
* `force-close` closes everything at the last bar's close with the labeled
  `force_close` exit reason.

Metrics (`trade_count`, win rate, realized/unrealized/net P&L, fees, max
drawdown from the per-session equity curve, open-position count, currency) are
persisted as PostgreSQL artifacts alongside trades, signals, equity curve, open
positions and logs — all referenced by `ContentRef` from the immutable
`BacktestResult`.

## Durable jobs, cancellation and restart (P06-06)

Stream runs use the P05 job store unchanged: transactional claim, lease,
heartbeat (progress reported per simulated session with `unit=sessions`),
cancellation and truthful terminal states. Cancellation is observed between
sessions and inside the progress callback. A cancelled run keeps its frozen
`backtest_runs` row but writes no `results` row, so it cannot be mistaken for a
completed run. Nothing claims resumability: a restart marks the interrupted
lease `interrupted` (P05 `expire_leases`) because no replay clock/account state
is persisted.

## Verification and a known detector limitation (P06-07 / P06-08)

`core/test_stream_backtest.py` covers determinism, future-bar mutation (a
violent move after a fill cannot change that fill), warmup suppression, both end
policies, concurrent-replay + live-paper isolation, cancellation, window bounds,
durable service execution and measured throughput. Evidence: E13.

Important finding recorded during implementation: the ported detectors are
full-history `.cjs` replicas. `_core_backtest_symbol` seeds the store with the
**entire** series and tells patterns their walk bar via `_dedup.set_current`, so
`len(df)`-relative scan bounds can see the future. On a strictly causal,
growing window — what live paper actually sees — most detectors are gated off by
their own trailing-edge bounds. Empirically, on the pinned fixtures only
`pattern_003_double_bottom` (and one `pattern_010_pennant` case) trigger on a
causal window; `pattern_002_double_top` never can, because its own
`h2_cap = n - OUTCOME_WINDOW - 3` plus `entry = h2 + min(7, …)` makes
`entry == len(df) - 1` unsatisfiable.

The stream replay deliberately keeps causal semantics: a backtest that could
see future bars would not describe what paper trading does. The offline
backtester is unchanged and remains the `.cjs` golden-number authority. Making
the detectors themselves walk-forward (or giving the replay a deterministic
"as-of" window) is a detector change outside the P06 scope and is recorded here
as a limitation, not silently worked around.
