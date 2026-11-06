# Profitability audit of the V2 / Kronos paper replay

Reviewed November 6, 2026 (Asia/Manila), against working tree based on
`a552f4748ac698250df5038ca426114e3357defd`, including the existing uncommitted
V2 detector changes. This is a command-path and execution audit, not a new
profitability backtest. No trading rules were changed for this review.

```bash
VOLUME_GATE_ENABLED=false .venv/bin/python main.py --paper --patterns-v2 --paper-reset \
  --market us --symbols=2000 --stream=2025-11-03 --kronos-gate-mode confirm \
  --export-trades-log=pt.json
```

**Verdict: this command cannot currently establish deployable portfolio
profitability.** It runs an uncapped independent-trade research ledger, selects
today's universe for historical dates, overlaps the fine-tuning validation
period, and has reproducible execution/data-handling defects. A positive
export would not resolve those problems. Several findings can also suppress
good opportunities, but their actual profit impact requires a new controlled
replay; the synthetic examples below are correctness evidence, not estimated
strategy returns.

The reset command was **not executed**. It deletes the shared US paper ledger
and truncates its signal log, and the stream startup can terminate a process
using its port. Existing `pt.json`, paper state, protected paths, and pre-existing
source edits were preserved. Probes used in-memory accounts and temporary
logs under `/tmp`. A small allowlist of effective, non-secret settings was
read through the application's configuration loader; no `.env` contents were
printed or edited.

## Progress tracker

Updated November 6, 2026. The audit below is the original evidence snapshot;
resolution notes supersede descriptions of old behavior. **Done** requires a
shipped correction and passing relevant checks. **Partial** means code improved
but stated acceptance criteria or external validation remain. Each resolved
issue is committed separately; find its commit with `git log --grep=Fxx`.

| Issue | Status | Resolution / remaining work |
| --- | --- | --- |
| F01 | **Done** | Funded admission, collateral and deterministic opening phase; 104 tests passed |
| F02 | **Done** | Reject malformed execution observations; 67 focused tests passed |
| F03 | **Done** | Cost-aware target and opening forecast checks; 86 focused tests passed |
| F04 | **Partial** | Combined policy and binding order exposed (56 tests); independent evaluation remains |
| F05 | **Partial** | Configured slippage fixed (85 tests); borrow/financing/partial-fill modelling remains |
| F06 | **Partial** | Dated universe input and bias manifest (15 tests); verified historical membership still required |
| F07 | **Partial** | Checkpoint identity/selection guards (77 tests); authentic training manifest unavailable |
| F08 | **Done** | Persist separate decision completion; retry failures without replaying exits; 152 tests passed |
| F09 | **Done** | Persist uncertified intervals; retain held names and stop after bounded retries; 65 tests passed |
| F10 | **Done** | Advance over all loaded tapes; 30 replay tests passed |
| F11 | **Partial** | Exact causal scanner context and deterministic budget (92 tests); capacity/deferred-work policy remains |
| F12 | **Partial** | Active controls, causal participation and strict screener floor (89 tests); execution realism remains |
| F13 | **Done** | Full journal, source/run identities, causal contexts, totals and atomic export; 67 tests passed |
| F14 | **Partial** | Inclusive cutoff and net liquidation accounting (34 tests); corporate-action data unavailable |
| F15 | **Done** | Cross-process ledger/cursor leases, safe port refusal and export backup; 48 tests passed |

## What the command actually does

1. `main._parse_args` parses bare `--paper` as 50; `_resolve_n_symbols` overrides
   that with **2,000**. `--stream` is normalized to **2025-11-03**.
2. `main.run_paper` deletes the US account if present, resets its signal log,
   then loads a fresh `PaperAccount`. It starts/replaces the shared stream
   server, fetches a cached **current** TradingView universe, and constructs
   `MarketScanner` with retrospective session fills enabled.
3. The stream preloads daily tapes. Each cycle pins a common date, feeds only
   history through that date, and processes pending entries and existing
   positions before looking for new signals. V2 uses daily bars only, with a
   minimum of 60 bars, and converts current-bar detections into trade signals.
4. The active signal path checks short eligibility and geometry, records DII
   in shadow, and skips the disabled volume gate. With current settings,
   vision, visual-rule filters, Laya, and collect-first are absent. Several
   confidence/regime/cooldown/risk functions return unconditional passes.
5. Surviving signals wait for the end-of-scan Kronos batch. Three one-day
   draws vote relative to the decision close; at least two directional votes
   are needed. No-opinion and errors reject entries in confirm mode.
6. A confirmed entry queues for the next observed bar's open. Geometry,
   engine identity, saved confirmation, and a stop-distance gap guard are
   rechecked. Each V2 entry normally receives about **$10,000**, rounded down
   to whole shares, without account affordability checks.
7. Entries receive both the pattern stop/ATR cap and an independent
   cost-aware paper stop/trail. Completed bars check existing orders before
   using their closes to ratchet tomorrow's protection. Kronos can also queue
   an exit for the next open.
8. Equity is marked, the replay advances, and the ledger is saved. With no
   duration/end date, replay stops at the available tape end. `finally` saves
   and exports; it does not liquidate remaining positions.

Trace anchors: [CLI dispatch](/home/engine-33ai/codes/trading/main.py:879),
[paper startup](/home/engine-33ai/codes/trading/main.py:206),
[scan cycle](/home/engine-33ai/codes/trading/core/scanner.py:932),
[bar application](/home/engine-33ai/codes/trading/core/scanner.py:1166),
[V2 conversion](/home/engine-33ai/codes/trading/patterns_v2/signals.py:288),
[next-open fills](/home/engine-33ai/codes/trading/core/paper_trader.py:713),
[exit ordering](/home/engine-33ai/codes/trading/core/backtester.py:655).

Effective settings sampled for this command in this workspace:

| Setting | Effective behavior |
| --- | --- |
| Initial capital / notional / risk sizing | $100,000 / $10,000 / `TRADING_RISK_PCT=0` |
| Volume / DII / vision / collect-first | OFF / shadow / OFF / OFF |
| Shorts / minimum US signal price | enabled / no price floor |
| Maximum positions / per-pattern setting | `0` / `2`; neither supplies a V2 portfolio cap |
| Configured daily loss / slippage | $1,500 / 0.0005; neither is applied as expected below |
| Actual fresh-account fees / slippage | 0.001 per side / **0** |
| Kronos model / entry vote | finetuned / confirm, 3 draws, minimum 2 agreeing |
| Kronos exits | **exit**, minimum 2 opposing; defer within 0.5 ATR of entry |
| Kronos band | clamp(0.35 × ATR%, 0.25%, 3%) |
| Kronos budget / timeout | 100 uncached contexts per flush / 180 seconds per worker call |
| Replay pace / initial history | scan-paced / 420 bars |
| Target cap | disabled (`target_max_r=0`) |

Other machines or later configuration changes can differ. The command pins
neither most of these settings nor an end date, universe snapshot, or checkpoint.

## Findings requiring attention

### F01 — P1: unlimited, concentrated, unfunded exposure — DONE

**Resolution:** fresh/reset books default to `PAPER_ACCOUNT_MODEL=funded`.
They enforce cash including fees, reserved short proceeds plus 100% entry
notional collateral, gross exposure (default 100%), configured total and
per-pattern limits, a default one-position-per-symbol limit, and session
realized-loss admission. Funded replay gathers observations, sorts entries
by symbol/timeframe/setup, fills all openings before any intrabar exits,
then manages full bars and detects signals. Future exit proceeds and feed
arrival order cannot choose/fund opening seats. Summaries and exports identify
the model/limits. Old ledgers and explicit `independent-fixed-notional` books
retain labelled research semantics. No broker margin/borrow certification is
implied. Validation: **104 passed**, including cash/collateral, daily reset,
restart and reversed-feed-order examples plus existing paper integration.


**Confirmed research-account behavior; dangerous if interpreted as a funded
portfolio.** `_open_position_locked` explicitly removes portfolio, gross
exposure, daily-loss, cash and R:R constraints. V2 bypasses catalog
`MAX_OPEN_PER_SYMBOL`, and only indicator books receive a seat limit.
Different V2 anchors can pile up on the same symbol; different patterns or
opposite sides can overlap too. Consumed-setup deduplication is not a symbol
or portfolio risk cap.

**Reproduction:** twelve distinct V2 setups for one symbol all queued and
filled at $100 in a $100,000 account: **12 positions, cash −$20,120,
equity $99,880, gross exposure 120.144%**. The isolated probe used default
`max_open_positions=4` and `max_open_per_pattern=2`; both were ineffective.
The actual workspace currently sets maximum positions to zero, so it
explicitly requests unlimited seats as well. A second probe lost $300 with
an account daily limit of $1, then accepted another entry on that session.

Consequences include correlated losses, unpriced financing, misleading
account return/drawdown, and many nominally independent bets on one event.
The 3% **per-trade-notional** stop does not cap portfolio loss: twenty $10,000
bets stopping ordinarily cost about $6,000, before gaps.

**Action:** enforce funded-book buying power, total/per-symbol/per-pattern
exposure and daily loss at fill time, with deterministic allocation across
same-session candidates. Keep independent-trade statistics explicitly
separate. Setting existing cap variables alone does not fix this path.

Sources: [uncapped entry path](/home/engine-33ai/codes/trading/core/paper_trader.py:789),
[ignored daily loss](/home/engine-33ai/codes/trading/core/paper_trader.py:324).

### F02 — P1: malformed OHLC can bypass or fabricate protective exits — DONE

**Resolution:** shared execution-candle validation now checks finite positive
OHLC, enclosing ranges, finite nonnegative volume and aware timestamps.
The scanner rejects invalid observations before management/watermarks; the
replay client validates its current candle before mutating the store; direct
paper queue/fill/management paths also reject invalid candles. Invalid pending
observations remain retryable and cannot consume the order. Point-price live
quotes retain their observed-price semantics. Regression examples cover
impossible ranges, nonfinite fields, invalid volume/timestamps and an intact
store/book after rejection. Validation: **67 passed** across candle quality,
scanner recovery, paper protection and live quote exits.


**Confirmed defect.** Scanner and ledger admission check finite positive
prices, but not `low <= open, close <= high` or `low <= high`. The stricter
Kronos context validator runs later and does not protect existing positions
from bad exit candles. V2's HLC checks likewise do not validate the ledger's
incoming OHLC order execution.

**Reproduction:** long entry $100, resting paper stop **$97.197197**, then
`O=100, H=101, L=99, C=80`. The impossible close is accepted, the position
remains open because the reported low never touched the stop, and equity
falls to **$97,990** for the one-position account. Scanner counts zero
snapshot errors and records the malformed bar as processed. An erroneous
range extreme can cause the reverse problem: a fictional stop/target fill.

**Action:** validate full OHLC, timestamp and volume before changing the
store or ledger. Quarantine invalid observations and distinguish missing
execution evidence from a certified surviving bar. Do not repair the range
by inventing highs/lows or silently certify an exit through an unknown bar.

Sources: [scanner validation](/home/engine-33ai/codes/trading/core/scanner.py:1169),
[ledger validation](/home/engine-33ai/codes/trading/core/paper_trader.py:1115),
[worker validation](/home/engine-33ai/codes/trading/scripts/kronos_confirm_worker.py:81).

### F03 — P1: next-open fills can have negative economics despite confirmation — DONE

**Resolution:** paper fills now reject targets that cannot earn positive net
reward after both fees and exit slippage. Fill-time confirmation uses the
candidate's slipped opening price and rebases saved forecast prices after
costs before applying the existing directional majority/band. It never runs
a new model forecast from the execution candle. Successful fills preserve
an `opening_check` alongside the immutable decision evidence. Both long and
short adverse gaps, costs and restart confirmation are covered. Validation:
**86 passed** across entry economics, Kronos integration, short execution
and paper permissions.


**Confirmed missing execution check.** The gap guard only rejects a crossed
target/stop or too little distance to the planned stop. It does not require
positive net reward after costs. The fill-time Kronos check authenticates
the old vote but does not translate it from the decision close to the actual
opening price.

Two reproduced examples:

| Decision / next open | Current outcome |
| --- | --- |
| Close $100; three draws predict $104; next open $105 | Confirmation survives and entry fills, although the old forecast now implies **−0.9524%** from the fill, before fees |
| Close $100; fixed target $130; next open $129.90 | Entry fills 76 shares; hitting $130 is labeled `take_profit` but nets **−$12.1524 (−0.123095%)** after both 0.1% fees |

The second example shows a losing target outcome even with perfect simulated
execution. It does not imply every other exit or an extreme favorable gap
must lose. SELL entries have the corresponding price/cost problem.

**Action:** calculate executable net target reward, actual protective risk,
and forecast return from the observed open before filling. Use only the
saved forecast and opening observation; rerunning from that day's completed
candle would introduce lookahead. Reject economically invalid entries;
choose any additional R:R/forecast-margin threshold on independent data.

Sources: [opening checks](/home/engine-33ai/codes/trading/core/execution.py:111),
[confirmation recheck](/home/engine-33ai/codes/trading/core/scanner.py:2039).

### F04 — P1: the 3% paper overlay largely overrides the advertised ATR ladder — PARTIAL

**Shipped correction:** execution metadata now explicitly describes both
competing exit policies and their precedence. New completed-bar and quote
exit evidence identifies `trigger_source` (paper protection, pattern stop,
pattern trail/target or completed-close/Kronos decision), so safety-overlay
exits cannot be attributed to the ATR ladder. No stop was widened or removed.
Validation: **56 passed** across binding-order attribution, exit evidence,
protection and quote settlement. **Remaining:** independent combined-policy
evaluation on frozen holdout data; these engineering tests do not measure edge.


**Confirmed policy interaction, not a reversed P&L formula.** V2 advertises
a 3–8% ATR cap and a 2R arm/2R giveback pattern trail. Every new paper trade
also receives `net-loss-trail-v1`: 3% net loss on entry notional and a trail
three percentage points behind its best positive net surviving close, with
no 2R activation requirement. The nearer order executes first.

**Reproduction:** entry $100, structural stop $70, ATR cap 8% gives a pattern
hard stop of **$92**. The independent order is **$97.197197** with 0.1% fees
per side. A low of $96 exits at the paper stop for **−3% net**, while the
pattern trail never arms. Raising the ATR cap cannot widen that safety order.

The same overlay can cut winners before their pattern trail/target. This is
especially material for volatile names, the original motivation for wider
ATR stops. Existing V2 parameter sweeps that did not install this paper
overlay do not validate the command's combined strategy.

**Action:** report and evaluate the combined exit policy, including which
order actually closed each trade. Keep the safety order visible. Do not
remove protection or tune it from one losing run merely to make the report
green; compare distinct, predeclared policies on held-out data.

Sources: [V2 ladder](/home/engine-33ai/codes/trading/patterns_v2/signals.py:59),
[independent policy](/home/engine-33ai/codes/trading/core/position_protection.py:38),
[ratchet](/home/engine-33ai/codes/trading/core/position_protection.py:88),
[order precedence](/home/engine-33ai/codes/trading/core/backtester.py:701).

### F05 — P1: cost realism is weaker than the configuration suggests — PARTIAL

**Shipped correction:** new paper books honour `paper_slippage_pct`; an
explicit zero still supports parity research, and saved/legacy ledger costs
retain their historical values. Long/short tests verify exactly one entry
and exit slippage charge and net cash reconciliation. Exports identify actual
fees/slippage and explicitly list unmodelled costs; no executable-profitability
certification is inferred. Validation: **85 passed**. **Remaining:** borrow
availability/fees/recalls, financing, dividend cash flows and realistic
participation/partial fills require broker/security data and further modelling.
This section is deliberately not marked done.


**Confirmed configuration disconnect and simulation omissions.** A reset
account defaults to zero slippage even though `PAPER_SLIPPAGE_PCT=0.0005`
is configured. `run_paper` does not pass that setting to `PaperAccount`.
The US profile does charge **0.1% on each side**; saying this run is entirely
fee-free would be wrong. The offline `ENGINE.txn_cost_pct=0` is not the paper
fee rate.

There is no bid/ask, size participation, queue/partial-fill model, borrow
availability, stock-loan fee, margin financing, recall handling, or explicit
dividend cash-flow settlement in this execution path. Short proceeds are
credited to cash without reserving collateral. Consequently the simulated
short book can contain trades that could not have been borrowed, and its
returns omit relevant costs. The SEC describes borrowing and margin as
part of short selling: [SEC Regulation SHO overview](https://www.sec.gov/investor/pubs/regsho.htm).

**Action:** connect the slippage setting and export effective costs. Stress
both sides at realistic friction and add borrow/financing/participation
constraints before treating research profit as executable. A $10,000 trade
with 5 basis points of extra cost on each leg loses roughly another $10;
small edges are particularly exposed. This estimate is a cost stress, not
a simulated alternative execution path.

Source: [actual constructor defaults](/home/engine-33ai/codes/trading/core/paper_trader.py:314).

### F06 — P1: historical universe selection uses future information — PARTIAL

**Shipped correction:** `--paper-universe=PATH` bypasses today's screener
using a dated JSON snapshot. It requires a matching market, date no later
than the explicit stream start, unique symbols and stable security IDs. The
run manifest records the exact bytes' hash, members, source and actual count.
Without a file, exports explicitly identify present-day selection bias;
a supplied file is not automatically certified as authentic point-in-time
data. Schema: `{"schema_version":1,"market":"us","as_of":"2025-10-31",
"source":"provider/reference","members":[{"symbol":"AAPL","exchange":
"NASDAQ","security_id":"provider-stable-id"}]}`. Validation: **15 passed**
across input, future-date rejection, CLI and export checks. **Remaining:** a
verified historical membership/listing/delisting snapshot and independent
liquidity evidence. No historical membership was fabricated.


**Confirmed evaluation bias.** `fetch_universe_cached(2000, 'us')` fetches
or reuses a present-day TradingView list ranked by `value`, with a $20M
current-turnover filter. It receives no historical as-of date. The six-hour
cache is an API optimization, not a November 2025 constituent snapshot.

This conditions historical selection on later liquidity/survival, can
exclude delisted losers, and includes later IPOs only once they have tape.
Calling `value` an ADV floor does not make it an average of trailing daily
turnover. Fewer than 2,000 rows can survive filtering; the requested count
is not evidence that 2,000 symbols were evaluated successfully.

**Action:** freeze dated universe membership, security identity and listing
history; apply liquidity using only bars before each decision. Report
requested, returned, tradable and evaluated counts separately. Current-name
historical research can still be useful, but needs that explicit limitation.

Sources: [universe choice](/home/engine-33ai/codes/trading/data/tv_client.py:528),
[cache](/home/engine-33ai/codes/trading/data/tv_client.py:587),
[ranking query](/home/engine-33ai/codes/trading/data/tv_client.py:428).

### F07 — P1: November–December 2025 overlaps model-selection validation — PARTIAL

**Shipped correction:** worker evidence explicitly records unknown holdout
status when no checkpoint-linked manifest exists. A supplied
`~/Kronos/finetuned/selection_manifest.json` binds every predictor/tokenizer
file hash, corpus hashes, `selection_data_end` and `eligible_from`; changed
weights, incomplete identities or replay before eligibility are refused.
`KRONOS_REQUIRE_PROVENANCE=true` additionally refuses missing metadata rather
than permitting labelled uncertified research. Manifest schema is exercised
in `core/test_profitability_model_provenance.py`; identity checking does not
independently prove a declared training history. Validation: **77 passed**.
**Remaining:** the operator confirmed that no authentic selection manifest
is available. Existing weights were neither retrained nor retroactively
certified, and November–December 2025 remains unsuitable as a clean holdout.


**Confirmed local corpus overlap; checkpoint provenance remains incomplete.**
The local Kronos corpus reports cutoff **2026-01-01**. Read-only inspection
of `symbols.json` and `days.i32` found **zero symbols whose training segment
reaches November 3, 2025**, but **14,922 whose validation segment does**.
For example, AAPL training ends May 9, 2024 and validation ends December 31,
2025; CGON training ends October 21, 2025 and validation runs October 22
through December 31. It would be inaccurate to call these November bars
confirmed gradient-training leakage.

However, tokenizer and predictor training select `best_model` using
validation loss. November–December replay therefore falls inside model
selection if these weights came from this corpus. The gate only checks
that checkpoint directories exist; it does not verify training/validation
boundaries or associate an immutable corpus manifest with the loaded weights.
Its correct `--as-of` SQL cutoff prevents future **input candles**, not
future information embedded in model selection.

**Action:** attach checkpoint and corpus hashes plus training, validation
and selection dates. Evaluate after all selection boundaries with an
appropriate horizon embargo, or use historical walk-forward checkpoints.
Do not relabel the entire November 2025 onward run as out-of-sample.

Sources: [dataset cutoff](/home/engine-33ai/codes/trading/learn/kronos_dataset.py:53),
[checkpoint selection](/home/engine-33ai/codes/trading/core/kronos_finetune.py:591),
[model loader](/home/engine-33ai/codes/trading/scripts/kronos_confirm_worker.py:163).

### F08 — P1: a failed detector can permanently consume its decision bar — DONE

**Resolution:** management and decision completion now have independent
persisted watermarks. V2 decisions commit only after detection and the gate
batch finish; failed analysis marks the scan incomplete and retries the same
session, including after restart, without repeating position management or
bar counters. Strict scan detection propagates per-family failures rather
than certifying an empty result. Old ledgers inherit their historical
management watermark as completed because past missing decisions cannot be
reconstructed reliably. Validation: **152 passed** across failure/restart,
scanner recovery, fast-scan parity and V2 detector/causality tests.


**Confirmed defect.** `_apply_bar` updates the persisted processed identity
before returning the snapshot for analysis. V2 detection exceptions are
logged and converted into a continue/empty result without a snapshot-error
or analysis-completeness failure. Per-family `_safe` also turns exceptions
into empty detections.

**Reproduction:** supply a ready 60-bar store, apply the completed bar, inject
one actual `detect_raw` exception, then retry the same bar. First analysis
was scheduled; retry analysis was **not** scheduled; snapshot errors stayed
**zero**, and the ledger watermark already contained that date. This can
silently omit a breakout and still let the scan advance/save. A crash between
bar admission and analysis/confirmation has a related partial-work risk.

**Action:** track ledger management and signal-decision completion separately.
Commit the decision watermark only after successful detection and a recorded
terminal gate outcome; retry failed work idempotently without replaying exits
or double-filling entries. Surface detector failures in scan completeness.

Sources: [early watermark](/home/engine-33ai/codes/trading/core/scanner.py:1253),
[caught detector failure](/home/engine-33ai/codes/trading/core/scanner.py:1529),
[family suppression](/home/engine-33ai/codes/trading/patterns_v2/patterns/__init__.py:45).

### F09 — P1: missing held-name bars leave unverified stops and stale equity — DONE

**Resolution:** missing/invalid held-name observations invalidate scan
completeness and record persistent unresolved execution intervals. Held names
are never silently marked dead. Historical replay retries the date and stops
incomplete after three failed observations instead of advancing on stale
prices indefinitely. Coverage, last observed timestamps and mark semantics
are exported in `execution_context.execution_coverage`; a later valid bar or
restart does not erase the unknown interval. No exit is invented. Source
remediation remains an operator task. Validation: **65 passed** across coverage,
candle rejection, recovery, exit evidence and paper protection.


**Confirmed behavior with material reporting risk.** `asof_mismatch` only
increments a skip counter; it does not make the scan incomplete. `no_data`
permanently drops that symbol for the run. Repeated invalid prices are skipped
after one retry. The ledger retains open positions and their last prices.

**Reproduction:** a held symbol has `asof_mismatch` while another symbol
prints the next session. The scanner reports **scan_complete=true**, advances
one session, leaves the held trade open and valued at its previous **$100**.
It cannot know whether an unobserved stop was hit. End-of-tape does not force
liquidation or flag every stale holding as uncertified.

**Action:** report per-position price age and unresolved execution intervals,
retrieve missing execution bars where possible, and make portfolio validity
depend on held-name coverage. A verified trading halt and an ingestion gap
need different treatment. Do not fabricate stop fills or silently value a
delisted/unpriced position as an ordinary surviving trade.

Sources: [skip handling](/home/engine-33ai/codes/trading/core/scanner.py:786),
[scan completeness](/home/engine-33ai/codes/trading/core/scanner.py:1127),
[last-price marks](/home/engine-33ai/codes/trading/core/paper_trader.py:511).

### F10 — P2: sparse tapes can make the shared replay skip a valid date — DONE

**Resolution:** pinned replay now chooses the earliest later date across all
loaded tapes, including symbols missing the current date. End-of-tape is
reported only when none has a later bar. Tests cover interleaved missing
sessions, future-only tapes, exhausted reference tapes and monotonic terminal
behavior. Validation: **30 passed** with the existing replay synchronization
suite. This fixes date omission; it does not certify the source's exchange
calendar or missing-price coverage (F09/F14).


**Confirmed calendar-control defect.** `StreamServer.advance` only considers
future dates from tapes that have a bar on the **current** as-of date.

**Reproduction:** tape A has February 2 and 4; tape B has February 3 and 4.
At February 2, advancing chooses **February 4**, skipping B's available
February 3 bar. If no current-date tape has a later bar, replay can also
declare the end despite future bars on other tapes. Dense reference tapes
normally mask this; sparse/partial/stale histories expose it.

**Action:** drive replay from a verified exchange-session calendar or a
validated common date index across all loaded tapes, independent of which
symbols happened to print on the preceding date.

Source: [advance algorithm](/home/engine-33ai/codes/trading/data/stream_server.py:539).

### F11 — P2: Kronos capacity, contention and data sources select the trades — PARTIAL

**Shipped correction:** daily model calls now receive the scanner's exact
causal, last-400-bar context through a temporary content-addressed payload.
The worker verifies its hash/cutoff and returns that identity; changed contexts
cannot reuse cached votes. Daily replay no longer substitutes a different
local PostgreSQL history. Exit and entry scheduling is deterministic within
its priority classes; evidence distinguishes capacity/context/infrastructure
failures from model votes. Temporary inputs are removed after the worker.
Validation: **92 passed** across context, worker, gate and scanner integration.
**Remaining:** the explicit per-flush capacity ceiling still rejects overflow;
a benchmarked deferred-work policy and live GPU timeout/load evaluation remain.
The cap was not silently removed or increased. Non-daily legacy contexts are
explicitly tagged provider history and require separate daily-source support.


**Confirmed selection mechanism; realized impact not measured here.** The
budget is the first **100 uncached contexts**, with open-position exits before
entries, not the 100 best opportunities. Pending order follows concurrent
scan arrival. In confirm mode, overflow becomes `no_opinion` and permanently
rejects that decision; it is not deferred for a later budget slot. With 100
uncached held-symbol exit contexts, a new symbol's entry received **“Kronos
check cap reached”** in the probe. This is conditional; it does not prove the
actual run reaches the cap.

Busy GPU locks, missing weights/venv, worker timeout, malformed history and
anchor disagreements also reject entries. Failing closed is appropriate;
the issue is confusing infrastructure/data rejection with a bearish model
decision or a measured strategy edge. The worker reloads weights per batch,
and its sequential per-symbol inference must fit the 180-second deadline.

The replay tape comes through the history facade/cache, often the remote
history API; `load_symbol_df` instead always queries **local PostgreSQL**.
Equal final date/close does not prove the two full contexts agree. A laptop
can replay remote bars yet have unavailable, stale, or different local model
history. One bad bar anywhere in the worker's last 400 bars invalidates that
symbol's context.

**Action:** use one frozen history source for detection, execution and model
context, and record hashes. Separate forecast rejection, unavailable context,
budget exclusion and worker failure. Make inference scheduling deterministic
and preserve resting stop management. Do not bypass confirmation on errors
or simply increase its budget without capacity measurements.

Sources: [budget priority](/home/engine-33ai/codes/trading/core/kronos_confirm.py:428),
[worker database](/home/engine-33ai/codes/trading/scripts/kronos_confirm_worker.py:42),
[GPU lock/timeout](/home/engine-33ai/codes/trading/core/kronos_process.py:50).

### F12 — P2: strategy controls are dead, hidden, or not liquidity controls — PARTIAL

**Shipped correction:** run manifests enumerate active controls and explicitly
identify inactive confidence/regime/cooldown/R:R compatibility gates. CLI
`--kronos-exit-mode=off|shadow|exit` makes exits independent of entry confirmation
in both runtime and exported settings. The US screener never falls back to
unfiltered symbols when its turnover floor removes everything. Funded fills
check a configurable maximum participation against 20 prior dated volume
observations, excluding execution-session future volume; missing history is
saved as unverified and `PAPER_REQUIRE_LIQUIDITY_HISTORY=true` rejects it.
Validation: **89 passed**. **Remaining:** default permissive missing-history
research is not an execution-liquidity certificate; spreads, partial fills,
borrow and independently validated strategy-gate choices remain unresolved.
Unvalidated signal filters were not re-enabled simply to reduce losses.


**Confirmed configuration/expectation gap.** Omitting `--pattern-only` does
not restore confidence, SMA200 regime, R:R or post-loss cooldown checks:
the imported scanner functions are local no-ops. Recording an exit in
`_cooldown_tracker` does not enforce a cooldown. V2 structural rules still
apply; this is not an absence of pattern validation.

`--kronos-gate-mode confirm` also leaves **Kronos exits ON** in this workspace.
That is an extra one-day exit policy, including profitable positions beyond
the 0.5-ATR noise band, not merely entry confirmation. DII currently shadows;
an environment override to filter can restore narrow veto bands. Neither
adding gates nor removing them has established positive expectancy here.

With volume confirmation OFF, V2's `tradable` checks HLC movement/range and
gaps, not traded volume, spread or dollar participation. The universe query
also deliberately retains unfiltered rows if its dollar-turnover floor would
remove everything. Thus “top 2,000 liquid symbols” is not a reliable execution
liquidity guarantee. Turning the RVOL gate ON would not by itself fix that.

**Action:** expose active controls and the combined strategy in the run
manifest. Implement hard execution-liquidity constraints separately from
optional signal-volume hypotheses, and stop silently relaxing the universe
floor. Evaluate entry and exit model policies separately before combining.

Sources: [no-op gates](/home/engine-33ai/codes/trading/core/scanner.py:56),
[exit policy](/home/engine-33ai/codes/trading/core/scanner.py:2099),
[V2 tradability](/home/engine-33ai/codes/trading/patterns_v2/patterns/common.py:256),
[floor fallback](/home/engine-33ai/codes/trading/data/tv_client.py:459).

### F13 — P2: the export loses evidence needed to audit profitability — DONE

**Resolution:** the durable journal no longer compacts away old decisions;
UI reads remain bounded while CLI and web exports read every retained event.
Exports include health/coverage counters and cumulative scan activity totals.
Signals have stable decision identities, causal context through their decision
bar (never execution-session future closes), and a run ID linked to source
hashes including V2 code. The manifest survives ledger saves. Persistence
failure prevents decision completion. CLI export is atomic and its status
records actual stop reason rather than the startup Running label. Old
truncated events cannot be recovered. Checkpoint/data certification and
future rejected-candidate outcome studies remain F06/F07/F14 requirements.
Validation: **67 passed** across journal, export, CLI, restart, coverage and
paper-book integration tests.


**Confirmed reporting limitations.** `_scan_stats` drops snapshot errors,
as-of skips, dead symbols, coverage and skew, even though these determine
whether orders/signals were observed. `patterns_found`, `signals_rejected`
and `rejection_by_gate` are reset each scan: they are not whole-run totals.
Signal persistence compacts after 2,000 rows, and export loads only the last
**1,000** events. The note that it holds “every detected signal” is therefore
false for sufficiently long/busy runs.

**Reproduction:** exporting counters containing five snapshot errors,
20 as-of skips, 50 dead symbols, and only 1,600 covered of 2,000 names kept
only `patterns_found=0` among the supplied fields. Such a file can resemble
“no opportunities” while hiding coverage failure.

V2 rules contain generic `provenance=patternsv2`, but no immutable detector
source snapshot. Engine hashing covers execution helpers, not detector
changes. Model evidence records `finetuned`, not checkpoint content hashes
or corpus/selection dates. The CLI also passes its startup `Running …`
status to the final export, so that string does not certify successful
completion. Final export uses a direct overwrite, making interruption a
possible source of a truncated file.

**Action:** add immutable run IDs/manifests, complete durable decision
events, cumulative funnel totals, health/coverage, actual termination cause,
and atomic exports. Archive rejected-candidate contexts and subsequent
outcomes separately for gate evaluation, rather than inferring their returns
from the accepted trades alone.

Sources: [discarded counters](/home/engine-33ai/codes/trading/utils/trade_export.py:148),
[bounded event log](/home/engine-33ai/codes/trading/core/signal_log_store.py:20),
[engine hash scope](/home/engine-33ai/codes/trading/core/pattern_provenance.py:14),
[overwrite](/home/engine-33ai/codes/trading/main.py:140).

### F14 — P2: corporate actions and terminal outcomes are not certified — PARTIAL

**Shipped correction:** `--stream-end=YYYY-MM-DD` supplies an inclusive
session cutoff checked before fills, management or detection. Exports retain
that cutoff and the terminal stop reason. Open books report a hypothetical
net liquidation value after configured exit fees/slippage, explicitly an
accounting stress with no invented fills. Corporate-action/adjustment and
terminal-event verification remain explicitly false. **Remaining:** the
operator confirmed that verified corporate-action/adjustment metadata is
not available; no split/dividend events or terminal prices were fabricated.
Independent source verification is required before certification. Validation:
**34 passed** across cutoff, net liquidation, funded accounting, CLI and exports.


**Execution-model limitation; no specific bad split fill is alleged here.**
The path consumes OHLC/volume without a split/dividend event ledger or
adjustment-basis manifest. V2 rejects large pre-entry close jumps as suspected
splits/bad prints, but that is a heuristic, not position-level corporate-action
accounting. It cannot adjust an already held quantity, cash entitlement,
stop and target if a raw tape splits. Fully adjusted bars need consistent
volume/price treatment and care around dividend cash flows too.

No fixed replay end date is given. New data/cache revisions can change the
endpoint and opportunities; final open marks lack hypothetical exit costs,
and closed-trade profit factor excludes open positions. The two statistics
can tell different stories without a calculation bug.

**Action:** freeze the data revision/adjustment basis and an explicit cutoff;
verify corporate actions and terminal coverage. Report closed outcomes,
open net liquidation value, exposure and stale marks separately. Do not
reinterpret a split as a trading loss or count unliquidated gross marks as
realized profit.

Sources: [jump heuristic](/home/engine-33ai/codes/trading/patterns_v2/patterns/common.py:192),
[tape payload](/home/engine-33ai/codes/trading/data/history.py:313),
[open marking](/home/engine-33ai/codes/trading/core/paper_trader.py:1288),
[closed result](/home/engine-33ai/codes/trading/core/paper_trader.py:1300).

### F15 — P2: reset and replay control are shared with other paper sessions — DONE

**Resolution:** CLI runs acquire OS leases for the ledger and shared replay
cursor before reset/load, retaining ownership through final save/export. UI
threads and reset use the same protocol, including same-process competitors.
An occupied incompatible/unverified stream port is refused, never killed.
Reset runs preserve an existing requested export to a unique `.bak` first.
A conflicting controller receives an explicit ownership error without mutating
the book. Different books can still run independently when they do not share
a cursor. Validation: **48 passed** across lease, port safety, CLI and book
flows. Standalone custom stream clients must cooperate with this ownership
protocol; this is not authentication for arbitrary localhost software.


**Confirmed operational hazard.** Reset deletes
`data/cache/paper_account.json` and truncates `logs/paper_signals_us.jsonl`.
There is no run-specific CLI ledger namespace or interprocess account lock.
A running web/CLI book can later save its old in-memory account over the
reset run's account. The stream port is shared as well: `_ensure_stream_server`
can terminate the process occupying it, and clients share the same replay
cursor/advance action. Independent clients can interfere with dates and
history; an in-process `RLock` does not prevent this.

**Action:** isolate each research run's ledger, log, export and replay server,
or enforce single ownership of the shared book/cursor. Preserve the previous
export before reset. Do not run this reset concurrently with an operator book.

Sources: [reset](/home/engine-33ai/codes/trading/main.py:229),
[shared stream startup](/home/engine-33ai/codes/trading/core/paper_books.py:943),
[signal reset](/home/engine-33ai/codes/trading/core/signal_log_store.py:81).

## What the existing pt.json does and does not prove

The reviewed file's SHA-256 is
`b386c26ae8bdcc25b073034782cebee045d710a2396c8cde06c0b21754db9d5f`.
It was exported November 6, 2026 at **09:45:55 Manila** and records a US run
starting **2026-09-01**, ending at simulated November 5, with **one closed
CGON short, zero open positions, net −$6.6343**. Its PH book is empty.
It is **not** an output of the requested November 3, 2025 start.

The 63 saved events contain 62 distinct entry decisions: 49 DII rejects,
five no-majority decisions, five reported date mismatches, two opposing
Kronos decisions and one fill. The queue/fill pair is one decision. Current
code defaults DII to shadow and preserves per-symbol worker errors; that
older funnel must not be projected onto a fresh run unchanged. One completed
loss, and no longs, establish neither side's expected profitability.

Prior reports also analyze other files named `pt.json` and other exit policies.
Their gains, target-cap experiments and parameter sweeps are not fresh
results for this command. The V2 geometric score is explicitly uncalibrated;
three draws from one model do not constitute three independently validated
predictors. Policy choices motivated by earlier runs require independent
evaluation after those choices are frozen.

## Behaviors checked that should not be mislabeled as defects

- Completed-close decisions fill at the next observed open; the worker's SQL
  cuts inputs at the decision date. The reviewed path does not intentionally
  feed the next day's close into the fill-time vote.
- Resting stops and targets are checked before that close ratchets tomorrow's
  trails. Open-through gaps fill at the open, and ambiguous intrabar stop/target
  touches choose protection conservatively. Daily OHLC cannot establish the
  true intrabar order; making every tie target-first would inflate profit.
- Correct stop-gap losses can exceed 3%. The stop is a trigger, not a
  guaranteed fill price; [SEC/Investor.gov order guidance](https://www.investor.gov/introduction-investing/investing-basics/how-stock-markets-work/types-orders)
  supports that distinction. This differs from F02's impossible input range.
- BUY/SELL cash and net-P&L signs reconcile in the focused tests. A short
  covered below entry can still lose after both fees; a small net losing
  trail is not automatically an accounting inversion.
- Simulated orders are scanner-managed, with IBKR execution disabled. This
  command supplies no evidence of broker-hosted protective orders or live fills.

## Work order before trusting a profit claim

1. Fix invalid execution-candle admission, negative-net-target fills and
   decision completion/retry semantics (F02/F03/F08). Add adverse examples
   as regression expectations of the corrected behavior.
2. Make funded portfolio limits and shared-state ownership real (F01/F15).
   Any capped replay must use deterministic session-wide allocation, not
   whichever feed worker finishes first.
3. Freeze a point-in-time universe, verified adjusted tape, endpoint and
   checkpoint/corpus manifests. Repair coverage/calendar/source divergence
   and retain every gate outcome (F06/F07/F09–F14).
4. Evaluate the **combined** protection/entry/exit policy with fees, slippage,
   borrow and financing. Compare model entry confirmation and model exits
   separately with a fixed candidate/data basis, then run complete portfolio
   variants because different exits change subsequent opportunities.
5. Use an untouched period after all model and rule selection. Report net
   expectancy, profit factor, uncertainty, long/short and pattern cohorts,
   worst gaps, portfolio drawdown, peak exposure, liquidity participation,
   and sensitivity to costs/large winners. Do not tune the 3% stop, DII bands,
   ATR constants or model-vote threshold to rescue this one export.

These are proposed corrections and validation requirements. No claim is made
that restoring a particular signal filter, widening a stop, removing shorts,
or increasing trade count will improve profit without outcome evidence.

## Validation record

**273 focused existing regressions passed**, covering CLI parsing, Kronos
votes/worker context, scanner confirmation/exits/recovery, short entry policy,
V2 detectors/exits, independent protection, exit evidence and accounting.
The only reported warning was Loguru's existing
`asyncio.iscoroutinefunction` deprecation. Tests used synthetic settings and
temporary signal logs. Restricted local event-loop sockets stalled the first
sandbox attempt; it was stopped/cleaned up, then the focused suite passed
with local-socket access. No full suite, live GPU forecast, remote-history
replay or requested reset run is claimed.

Separate deterministic assertion probes reproduced F01–F04, F08–F11 and
the export-counter loss in F13 against current functions. The isolated
12-position example deliberately used default caps of four/two to prove
those settings are ignored; the workspace's effective maximum is zero.
Probe artifacts currently available for inspection:

- [Probe script](/tmp/profitability_trace_probe.py)
- [Probe evidence](/tmp/profitability_trace_evidence.json)
- [Focused test runner](/tmp/profitability_run_tests.py)
- [Focused test output](/tmp/profitability_trace_tests.txt)

They are temporary artifacts; the essential inputs, outputs and limitations
are preserved in this document. Run the probe from `/tmp` with synthetic
configuration to avoid loading operator settings:

```bash
cd /tmp
WATCHLIST=SPY TV_SCREENER=america TV_EXCHANGE=NASDAQ \
  VOLUME_GATE_ENABLED=false MPLCONFIGDIR=/tmp/profitability-mpl \
  /home/engine-33ai/codes/trading/.venv/bin/python /tmp/profitability_trace_probe.py
```

Selected source SHA-256 values bind this audit to the mixed working tree:

| File | SHA-256 |
| --- | --- |
| `main.py` | `4181203e194e763cddd7631c109126acedea3f6b18fb34d27e6b9074e26cdd81` |
| `core/scanner.py` | `01decf640588ac073396f9d8c9cffe7a1d85c2b84e13246b12b122df13a7443c` |
| `core/paper_trader.py` | `cad9c3f1e0ed8020c22d8387e74bd898ef962fbdadadbc4390763b32ea616698` |
| `core/execution.py` | `30e05946360e4edd4581295f73ef62746b3c3e2c4da0744b59367fd9a0ffcf30` |
| `core/position_protection.py` | `2fb3b0454d18318095cb1aa6c7b90bbace237d738d9d6b3e5dd0b23335dd5837` |
| `core/kronos_confirm.py` | `05ef7e0ab244593e7597351ffa8c38cc1a68fa6f567c8b9eb2051ad9418f7768` |
| `scripts/kronos_confirm_worker.py` | `984ee15d906c931af1df07ecf00e8168c631989dc3df993a49d89625b50ed83e` |
| `patterns_v2/signals.py` | `17e795c5dee9944cfb05b327bc8350cee92058dcbc80451675a42d3517f0a606` |
| `patterns_v2/patterns/__init__.py` | `01df5cd4607d1998fce638eaf0450f2b451ce9fcc18b8b91f83dbc3fef32343a` |
| `patterns_v2/patterns/common.py` | `d8ea4a73aec2eae74d54c742dc952a0ebb3eb3f49db79abe8bfbfa79bba0a8c8` |
| `patterns_v2/patterns/double_bottom.py` | `65cbd9b6afe87c372bc6359cd422cdcf7d2e3865da7f8f9b24d248bc06f1abf3` |
| `data/stream_server.py` | `407b4fab653590f1dac6f1fa59999058265b9a2bff28509f84cdc40aa582129c` |
| `data/tv_client.py` | `17e7d6afb41c764271e972cfac49c7bc5dcc1bc4c1285529e49a8dad8aa2dbf6` |
| `utils/trade_export.py` | `77f96df042be0752959cb6c8cd688bfb0da41fea41fa4d80a9a62c154d31af79` |
