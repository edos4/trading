# Paper-stream V2 scanning performance

The largest measured recurring costs are serial V2 detection and rebuilding each symbol's DataFrame on every new bar. Temporary detector prototypes reduced CPU time from 8.56 seconds to 2.27 seconds; adding eight persistent processes reduced it to 0.59 seconds. These are component measurements, not whole-replay speedups. Kronos exit forecasts remain enabled in the requested command and can add a separate delay.

Investigated command:

```bash
python main.py --paper --patterns-v2 --paper-reset \
  --market us --symbols=2000 --stream="2026-01-01" \
  --kronos-gate-mode off --export-trades-log="log.log"
```

The command itself was not executed: it resets the paper ledger and signal log. Measurements used read-only PostgreSQL transactions and temporary scripts. Only this report was added to the repository.

## Execution trace

```text
main.run_paper()
  reset/load paper account and signal log
  PaperBook._ensure_stream_server(start_date)
  TVClient.fetch_universe_cached(2000, "us")
  MarketScanner(... patterns_v2=True, data_feed=StreamClient())
    start()
      _discover_patterns() -> one Patternsv2Scan placeholder, daily timeframe
      _open_analyze_pool() -> explicitly disables pool for V2
    open persistent WebSocket sessions
    _preload_feed()
      StreamServer.preload_symbols()
        memory/disk cache -> bulk DB or HTTP -> per-symbol fallback
    repeat for each simulated session:
      _scan_all()
        pin one replay date
        fetch batches of 50 symbols
          first request: lookback history; later requests: one candle
        _apply_bar(): pending fills, exits, bar identity
        _run_analyzes() -> _analyze_local() -> _analyze_patterns_v2()
          OHLCVStore.get_df()
          detect_trade_signals(latest_bar_only=True)
            ATR
            bullish and mirrored bearish ZigZag
            reversal, flag, pennant, cup, and bounded-pattern searches
          signal handling: DII, volume, other applicable entry checks
        await Kronos exit forecasts when open positions need them
        mark equity, advance replay only after the whole scan completes
      save paper account
  finally: save account and write trade export
```

Entry points: [main.py](/home/r00t/codes/trading_bot/trading_bot_v2_swing_jun22/main.py:304), [scanner loop](/home/r00t/codes/trading_bot/trading_bot_v2_swing_jun22/core/scanner.py:854), [scan batches](/home/r00t/codes/trading_bot/trading_bot_v2_swing_jun22/core/scanner.py:912), [V2 dispatch](/home/r00t/codes/trading_bot/trading_bot_v2_swing_jun22/core/scanner.py:2090).

`2026-01-01` rolls forward to January 2 through the existing holiday handling. With no duration limit, replay continues to the end of the available tapes. The export is written once on exit; its `.log` extension does not change its JSON contents.

Observed effective settings: 15 feed workers, batch size 50, initial history 420 bars, store capacity 512 bars, replay interval 0, volume gate enabled, vision disabled, collect-first disabled, Kronos exit mode `exit`. The 420-bar initial window grows to 512 as replay advances.

## Measurements

Source data was the existing cached 2,000-symbol US universe and PostgreSQL daily bars through June 30, 2026. Of those symbols, 1,676 had at least 60 bars, the detector minimum. The query returned 819,347 bars, capped at 512 per symbol, in 2.28 seconds including retrieval. The snapshot produced no current-bar signals, so timings primarily describe the detector's rejection path.

Detector timings below are medians of three runs on this machine, which reports 32 logical CPUs. NumPy/OpenMP thread limits were one for the comparison runs. Process measurements include sending compact OHLCV arrays and returning results, and exclude pool startup and DataFrame construction.

| Detector variant, same 1,676 symbols | Seconds | Relative to baseline |
| --- | ---: | ---: |
| Current raw detector, serial | 8.56 | 1.00x |
| Prune flag/pennant poles that cannot signal on the latest bar | 4.65 | 1.84x |
| Precompute candle-validity mask for ZigZag | 6.11 | 1.40x |
| Both detector changes, serial | 2.27 | 3.77x |
| Current detector, four spawned processes | 2.65 | 3.23x |
| Current detector, eight spawned processes | 1.81 | 4.73x |
| Both detector changes, four spawned processes | 0.78 | 11.02x |
| Both detector changes, eight spawned processes | 0.59 | 14.46x |

Pool startup plus a small warm-up took about 1.2 seconds with four workers and 1.6 seconds with eight in the current-detector experiment. Keeping a pool alive across replay dates amortizes that cost. Timing varies with host load; an earlier combined eight-worker run took 0.51 seconds.

A separate call through `detect_trade_signals()` on the same frames took 9.47 seconds once. A cProfile sample of 250 symbols took 2.15 seconds and attributed these inclusive times:

| Function | Seconds | Share of profiled time |
| --- | ---: | ---: |
| Flag/pennant shared detector | 1.03 | 48% |
| Scalar `bar_sane()` calls | 0.96 | 45% |
| ZigZag | 0.74 | 35% |
| Bounded-pattern detector | 0.24 | 11% |

Inclusive times overlap: `bar_sane()` is called inside the other functions. The existing comments about line fitting once dominating CPU do not describe this profile; closed-form fitting and latest-bar pruning for bounded patterns are already implemented.

Rebuilding all 1,676 frames through `OHLCVStore.get_df()` took another 2.04 seconds. Reading those frames again from its existing cache took 0.00048 seconds. The cache helps repeated reads within one bar, but `apply_candle()` invalidates it when the next replay candle arrives.

## Recommended changes

1. **Pass the latest-bar restriction into flag/pennant detection.** The aggregator currently passes `latest` only into the bounded-pattern detector. Flags and pennants scan the whole window four times: two shapes in each direction. A pole ending at `i1` can signal only between `i1 + min_c` and `i1 + max_c`. Skip poles whose intervals exclude the requested bar. Preserve the earlier candidate checks within each retained pole so an earlier breakout or cancellation still wins. This was the largest small detector improvement measured. The same idea can later be applied to double bottoms/tops and cups using their own confirmation/wait rules.

   Relevant code: [aggregator](/home/r00t/codes/trading_bot/trading_bot_v2_swing_jun22/patterns_v2/patterns/__init__.py:87), [shared flag/pennant search](/home/r00t/codes/trading_bot/trading_bot_v2_swing_jun22/patterns_v2/patterns/_pole_consolidation.py:88).

2. **Compute the candle-validity mask once per ZigZag invocation.** Two ZigZag walks repeatedly call scalar NumPy finiteness checks and calculate the same tolerances for every bar. A vectorized mask preserves the current validity conditions while making each loop iteration read one boolean. Avoid introducing a broad global cache: inputs and their mirrored arrays change per evaluation.

   Relevant code: [bar validation and ZigZag](/home/r00t/codes/trading_bot/trading_bot_v2_swing_jun22/patterns_v2/patterns/common.py:47).

3. **Add a V2-specific process-pool job path.** V2 is explicitly excluded from the existing pool, so `SCANNER_ANALYZE_WORKERS=8` currently has no effect on this command. Raising `SCANNER_CONCURRENCY` overlaps feed requests but leaves detector calls serial on the event loop. Use persistent spawned processes and batches of compact numeric arrays; keep the paper ledger, gates, signal logging, and replay advancement in the parent. Preserve input ordering when collecting results. Removing the exclusion alone is insufficient: existing worker initialization loads pinned V1 patterns and `analyze_batch()` runs those patterns.

   Relevant code: [pool exclusion](/home/r00t/codes/trading_bot/trading_bot_v2_swing_jun22/core/scanner.py:645), [analysis dispatch](/home/r00t/codes/trading_bot/trading_bot_v2_swing_jun22/core/scanner.py:1419), [existing worker initialization](/home/r00t/codes/trading_bot/trading_bot_v2_swing_jun22/core/pattern_jobs.py:48).

4. **Reuse prepared replay arrays and dates.** Once detection is faster, the measured 2.04-second frame rebuild becomes the larger recurring component. Prepare numeric columns and normalized dates once when a tape loads, then select the causal window ending at the current replay index. The stream is already fully loaded into memory. An in-process replay feed could eventually remove WebSocket JSON and candle-object conversion too, but batching and delta delivery already keep warm transport small. Measure that transport before replacing it.

   Preserve the existing initial 420-bar window, growth to 512, session timezone, and bar identity. Precomputing a full-history ATR or ZigZag and slicing it can change results because the current rolling-window calculations restart their state at the window boundary.

   Relevant code: [frame construction](/home/r00t/codes/trading_bot/trading_bot_v2_swing_jun22/data/ohlcv_store.py:120), [tape snapshots](/home/r00t/codes/trading_bot/trading_bot_v2_swing_jun22/data/stream_server.py:157).

5. **Use the available database for preload, then optimize the bulk query.** A working database connection alone does not select the local path. Here `stocks_history_owner=False`, and the stream process defaults to the remote history facade. A newly launched stream server can use the existing direct-DB path with the environment prefix `STOCKS_HISTORY_OWNER=true STOCKS_HISTORY_URL=""`; this needs no `.env` edit. A server already running on the stream port may be reused with its prior settings.

   The current bulk query ranks all qualifying bars with `ROW_NUMBER()`. A per-symbol `LATERAL` subquery with descending timestamp order and `LIMIT` can stop earlier using the existing `(symbol, ts)` primary-key index. Read-only `EXPLAIN ANALYZE` runs for the actual replay preload bounds returned 1,219,015 rows in both cases. Median SQL execution fell from 2.68 seconds to 1.28 seconds, about 2.1x. This is a preload improvement; warm scans already read memory and do not query PostgreSQL per symbol/date.

   A further startup improvement is to construct tapes directly from the bulk result. Currently the cold path writes each result to JSON cache and then reads/parses it again during `_tape_lookup()`. That additional improvement was identified by inspection and was not timed.

   Relevant code: [history routing](/home/r00t/codes/trading_bot/trading_bot_v2_swing_jun22/data/history.py:86), [bulk SQL](/home/r00t/codes/trading_bot/trading_bot_v2_swing_jun22/data/history_bulk.py:47), [bulk cache fill](/home/r00t/codes/trading_bot/trading_bot_v2_swing_jun22/data/stream_server.py:362).

6. **Account for Kronos exit work separately.** This command turns off entry confirmation, while the effective `KRONOS_EXIT_MODE=exit` still constructs a gate. Open positions are queued for forecasts and the scan awaits the result before advancing. Each uncached batch launches a new worker, loads its model, and reads symbol history; the configured deadline is 180 seconds. This cost was not benchmarked and may dominate sessions with open positions. A persistent model worker and batched, bounded history reads would address that work. Setting `KRONOS_EXIT_MODE=off` removes it but changes the strategy's exits, so it is a separate behavioral choice.

   The inspected worker also reads the latest database history without a replay-date cutoff, and its caller supplies symbols without the historical anchor date. If exit forecasts are retained, pass the replay cutoff before building a persistent forecast cache or service; the current code can feed future bars to a historical replay.

   Relevant code: [gate construction](/home/r00t/codes/trading_bot/trading_bot_v2_swing_jun22/core/scanner.py:370), [awaited flush](/home/r00t/codes/trading_bot/trading_bot_v2_swing_jun22/core/scanner.py:1912), [worker arguments](/home/r00t/codes/trading_bot/trading_bot_v2_swing_jun22/core/kronos_confirm.py:345), [history query](/home/r00t/codes/trading_bot/trading_bot_v2_swing_jun22/scripts/kronos_confirm_worker.py:35), [model loading](/home/r00t/codes/trading_bot/trading_bot_v2_swing_jun22/scripts/kronos_confirm_worker.py:159).

Replay pacing is already zero, frames already have a version cache, tape date lookup already uses binary search, snapshots already batch 50 names, and warm snapshots already send deltas. These existing optimizations should be preserved. DII and volume checks run after detection and were outside the detector benchmark.

## Validation and limits

Temporary variants matched the baseline's current-bar raw results across all 1,676 measured symbols. Additional comparisons covered 1,340 historical prefixes and 167 existing textbook prefixes, with two positive textbook signals. New synthetic flag/pennant fixtures covered 312 prefixes, including eight positive signals across bullish and bearish flags and pennants. The ZigZag mask matched on 200 random series containing invalid, nonfinite, zero-range, and out-of-range-close candles. Process results were compared in input order.

This establishes sampled detector parity, not full trade parity. Before shipping, compare isolated full replays with identical tapes, symbols, ordering, settings, and seeds. Compare detections, simulated fill dates/prices, quantities, exit reasons, positions, and equity; exclude real timestamps and scan duration from equality. Keep the all-symbol scan barrier before advancing the simulated date. Full replay timing, GPU forecasts, transport timing, and the full test suite were not run.

Temporary reproduction artifacts: [input/profile script](/tmp/profile_v2_scan.py), [detector/process comparisons](/tmp/bench_v2_options.py), [combined process benchmark](/tmp/bench_v2_combined_pool.py), [SQL/frame measurements](/tmp/profile_v2_support.py), [positive/invalid-bar checks](/tmp/check_v2_prototypes.py), and [cProfile data](/tmp/v2_scan_profile.pstats). Run the Python scripts from the repository root with `.venv/bin/python`, `PYTHONDONTWRITEBYTECODE=1`, and `OPENBLAS_NUM_THREADS=1`; the process comparison also used `OMP_NUM_THREADS=1`. Run the input/profile script before the other benchmarks. All database connections in these scripts use read-only transactions.

## Implemented

The recommendations above are in the tree now.

1. **Latest-bar restriction in flag/pennant detection.** `_pole_consolidation.detect()` takes `latest=` and skips a pole whose `[i1 + min_c, i1 + max_c]` interval does not contain it. Retained poles still walk from `i1 + min_c`, so an earlier breakout or cancellation in the pause still wins. `patterns/__init__.py` passes it to `bull_flag` and `bull_pennant`.
2. **One candle-validity mask per ZigZag.** `common.sane_mask()` vectorizes the scalar `bar_sane()` conditions; `zigzag()` reads the boolean array and a precomputed `np.isnan(a)` array.
3. **V2 process pool.** `core/pattern_jobs.py` gains `init_v2_worker`, `make_v2_pool`, and `detect_v2_batch`. `MarketScanner._open_analyze_pool()` opens this pool for `--patterns-v2` instead of forcing workers to 1, and `_run_analyzes_v2()` sends only `(symbol, timeframe, high, low, close)` arrays and collects results in input order. Ledger, gates, signal logging, and replay advancement stay in the parent.
4. **Array fast path instead of a per-bar DataFrame.** `OHLCVStore.detect_arrays()` caches `(high, low, close)` per store version. `signals.detect_raw()` and `signals.signals_from_raw()` split detection from conversion, so `_analyze_patterns_v2()` builds a DataFrame only when a detection survives to become a signal.
5. **Per-symbol bulk preload SQL.** The limited path in `data/history_bulk.py` is now `unnest(%s::text[]) CROSS JOIN LATERAL (... ORDER BY ts DESC LIMIT n)`, which stops on the existing `(symbol, ts)` index instead of ranking each symbol's whole history.
6. **Kronos replay cutoff.** The gate groups a flush by anchor date and passes `--as-of` to `scripts/kronos_confirm_worker.py`; `load_symbol_df()` filters `bar_date <= as_of`, so a historical replay no longer feeds the model future bars.

### Verification on this checkout

This host reports 32 logical CPUs. Measurements are with 512-bar synthetic series, on the same machine, not the original report host.

- The vectorized mask matched `bar_sane()` on every bar of 8 series containing flat, non-finite, out-of-range, and inverted prints.
- Current-bar detections with `latest=` matched the unfiltered scan on 12 such series (300 in an out-of-tree check).
- `detect_raw()` + `signals_from_raw()` matched `detect_trade_signals()` across seeds; the spawn-batch entry matched inline detection.
- 400 series (512 bars): full-history detection 3.86 s, live-bar detection 0.48 s.
- 1,600 series: inline live-bar detection 2.16 s, eight-worker batches 1.16 s.
- Rebuilding 500 stored symbols: `get_df()` 504 ms, `detect_arrays()` 22 ms.
- The LATERAL query returned identical rows to the old `ROW_NUMBER()` query at limits 60, 420, and 512, with and without `after_ts`.
- `pytest -m "not slow"`: 2199 passed. The single failure, `test_snapshot_reports_the_persisted_scan_clock_without_a_scanner`, reads the checked-in `data/cache/paper_account.json` clock (2840.81 s + 10 s); it is not related to these changes.

Still open: the report's own ship gate, an isolated full replay compared on trades/fills/equity, and applying `latest` to cup/double-bottom searches (listed as a follow-up above).

