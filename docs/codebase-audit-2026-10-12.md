# Codebase audit — 12 October 2026

Reviewed the scanner, legacy and Patternsv2 detectors, shared execution engine,
paper accounts, replay, market rules, persistence, registry validation, and
desktop/web integration. Assumed one application process with separate US/PH
paper threads and concurrent scan workers. Existing uncommitted Patternsv2,
paper-session and dashboard work was included with the operator's approval.

## Correctness changes

- **Live patterns:** channels and double tops no longer reserve future outcome
  bars that make detection on the latest bar impossible. Live channel caches
  cannot reuse another symbol's prices or a previous bar's missing breakout;
  historical channel caches distinguish upward and downward formations.
- **Patternsv2:** measured objectives use the pole, bowl or full formation
  width, independently of the protective stop. Real cup breakouts now survive
  signal conversion. The existing minimum 1R admission rule remains: setups
  with a full-depth stop can still be rejected. Removed duplicate ZigZag work
  and calls to disabled detectors.
- **Entries:** position caps are checked for every drained signal. Signals
  without a setup key cannot open eight identical positions on one bar.
  Offline and stream backtests respect PH long-only rules and use the historical
  session date for board-lot rounding.
- **Exits:** an early-ending replay force-closes at the last observed candle,
  never the dataset's future final candle. The final equity mark includes exit
  costs. Indicator exits and data-end exits record their bar indices.
- **Accounting:** replay applies configured slippage; paper slippage survives
  restart and entry equity reflects the actual market mark. R multiples use
  initial saved risk, including stop caps, rather than a later trailing stop.
- **Invalid data:** nonfinite/nonpositive trade prices and invalid cost settings
  are rejected. A malformed latest candle cannot consume the scanner watermark
  or advance replay; the corrected candle remains eligible for analysis.
- **Persistence:** unreadable/corrupt existing ledgers fail closed instead of
  being replaced by a fresh account. Concurrent saves cannot write an older
  snapshot after a newer balance.
- **Session lifecycle:** startup exceptions release the paper book. Stop during
  symbol loading prevents scanner startup. Simultaneous US/PH starts cannot
  claim the shared replay server together. Empty books can rewind their scan
  clocks using the supplied start date.
- **Recovery:** incomplete replay snapshots hold the current session. Broken
  transports trigger reconnection after pending analyses and collected signals
  have been handled. Workers cannot outlive a cancelled scan. One failed signal
  is logged without discarding later signals.
- **Browser/export payloads:** undefined metrics become JSON null consistently,
  including infinite profit factor in a winning-only account. Dashboard polling
  continues after transient request failures.
- **Validation infrastructure:** sandbox test doubles share one subprocess
  implementation with explicit fixture settings and no inherited credentials.
  Lifecycle fixtures replay two sessions; the end-to-end candidate test covers
  four sessions with an actual pattern entry and exit. Production isolation
  and positive-detection assertions remain intact.

## Performance

Offline median of three runs against commit
`89240d9a9a36b5b5a91084f56430e78a73fbb237`:

| Isolated workload | Before | After | Speedup |
| --- | ---: | ---: | ---: |
| Replay lookups: 200 sessions on 420 bars | 0.1630 s | 0.0010 s | 168.5× |
| 50 DataFrame rebuilds of 420 bars | 0.1870 s | 0.0562 s | 3.33× |
| Synthetic provider: 20 symbols, four workers, two 25 ms calls each | 1.0109 s | 0.2589 s | 3.90× |

Replay lookups use cached session keys and binary search. DataFrame construction
avoids deep-copying each candle. Blocking TradingView calls run off the asyncio
event loop, while the existing provider throttle remains in force. These are
microbenchmarks, not a measured improvement in complete live scan throughput.

Reproduce without network calls:

```sh
.venv/bin/python scripts/benchmark_scanner.py --baseline 89240d9
```

## Validation and operational limits

All **611 Python tests passed**, split into 556 general tests and 55 candidate
pipeline / desktop / web tests. Each group used a disposable PostgreSQL
instance. Coverage includes registry validation, candidate execution, shared
execution, replay, API contracts and historical golden numbers. The desktop
group emitted three Tkinter variable-finalizer warnings during cleanup. This
preexisting desktop teardown issue remains: pending worker callbacks can retain
a closed dialog and release its Tk variables outside the main thread.

All three JavaScript test scripts passed (`web/test_chart_draw.cjs`,
`web/test_patterns_client.cjs`, `web/test_replay_client.cjs`). Shell scripts
passed `bash -n`; `git diff --check` passed. The local Graphify index was
refreshed.

Live broker execution is disabled in this repository. No real orders, live
provider requests, production database migrations, or deployment were performed.
Desktop/browser behavior was checked through automated contracts and JavaScript
tests, without a manual visual pass.

Legacy offline backtests deliberately retain the full-history `.cjs` selection
rules for historical parity. Their outcome-filtered results are not equivalent
to causal live/stream performance; use causal replay when assessing deployable
signals. The live double-top day-seven entry cannot know whether a later
neckline break will eventually happen.

Existing immutable pattern versions retain their frozen detectors. Baselines
whose imported `data/ohlcv_store.py` or `data/tv_client.py` hashes match the old
files require reviewed revalidation/publication against the updated runtime
before new legacy-pattern scans. Their records were not modified automatically.
Existing positions stamped by the immediately preceding engine can still exit:
that exact engine hash is explicitly allowed because its exit ladder is
unchanged. Unknown engine hashes continue to be rejected.

No profitability or risk-policy change is claimed. Independent flat-notional
pattern trades and existing portfolio-limit behavior remain part of the
strategy's current design.
