# Paper exit audit — 2026-11-05

Source: `pt.json`, confirmed by the operator (the requested `tr.json` is absent).
Exported 2026-11-05 12:25:15 UTC / 20:25:15 Manila. Input SHA-256:
`097df198350a529d5297866ae34a739b7d029379448974fd5ef6ab938a651476`.
Machine-readable reproduction: `paper-exit-audit-2026-11-05.json` in this directory.

## Findings

The US book contains **65 closed trades, no open positions and four pending
entries**. The PH book has no trades. USD figures below apply only to US.
Pending orders are not trades and are excluded from the exit comparison.

| Cohort | Trades | Wins | Net P&L | Profit factor |
|---|---:|---:|---:|---:|
| All | 65 | 23 (35.4%) | −$995.59 | 0.926 |
| BUY | 38 | 19 (50.0%) | +$3,853.69 | 1.591 |
| SELL | 27 | 4 (14.8%) | −$4,849.29 | 0.296 |

There are **42 hard stops, 23 targets, zero trailing stops, zero time exits
and zero Kronos exits**. Gross price-movement P&L is +$309.84; $1,305.44 in
fees makes it negative. Average winner is $539.78 versus an average loss of
$319.30. That payoff requires approximately **37.17% wins**, versus the
observed 35.38%; a nominal 2:1 gross target/stop ratio obscures costs and gaps.

Six trades retain a last Kronos `oppose` vote, yet none exited via Kronos.
All six saved vote anchors are favorable to their BUY entries. The current
Kronos exit handler defers an opposing vote until the position is adverse by
its configured ATR floor; this explains why a bearish vote is not a close.
The export does not snapshot that floor or a separate deferral outcome, so
this explanation is an inference from the saved anchors and current handler,
not a reconstructed historical model run. VITL was slightly profitable at
its saved opposing vote and subsequently gapped through its hard stop.

The same saved ladder reproduces **all 65 exit dates, reasons, prices, bar
counts and net P&Ls** to export precision against local OHLC tapes. This is
evidence that the recorded closes follow the saved rules, not evidence that
those rules have an edge. Today's detector files differ from the detectors
that produced these entries; no entry rescanning or detector validation is
claimed here.

## How a position actually closes

1. The scanner replaces the detector's structural stop with its configured
   fixed stop, currently 3% of the signal price. This also replaces the
   detector's ATR-based trailing geometry. The adapter's ATR defaults therefore
   do **not** describe this export's executed policy.
2. A signal is queued on a completed daily bar. It fills at the next observed
   open, subject to gap/expiry checks. The fixed signal stop stays anchored;
   an entry-relative 3% cap may tighten it further. Adverse entry gaps can
   shrink actual risk: this export's executed risk is **1.56–3.00%**.
3. At that open, the trail is rescaled to actual risk: activate at **1.75R of
   favorable closing movement**, trail by a price fraction equal to **1R**.
   The saved target was also shortened to at most **2R** of executed risk.
4. On each matching-timeframe bar, a queued close-confirmed exit fills at the
   open first. Otherwise, resting opening orders precede intrabar touches;
   the nearer protective stop is checked before a target when OHLC cannot
   establish which intrabar touch came first.
5. Only after a position survives those checks can that bar's close arm or
   ratchet the next bar's trail. A long trail is `best_close × (1 − trail_pct)`;
   a short trail is `lowest_close × (1 + trail_pct)`. Stops never loosen.
6. Exits apply directional slippage and both entry/exit fees. The paper
   ledger's cash reconciles with the resulting net trade P&L.

The cap and activation are working against each other: a **2R intraday target
can close the trade before its close establishes the 1.75R trail**. In this
export, **no position ever armed a trailing stop**. Disabling the trail alone
reproduces precisely the same results. Lowering its giveback width would
therefore change nothing for these saved trades unless activation or targets
also changed.

**30/65 trades closed on the entry session** (21 stops and nine targets).
The median entry-day high–low range was **2.10R**. Entry-day stop checks are
valid for next-open entries; skipping them would invent survival. The tight
fixed stop may be vulnerable to ordinary daily ranges, but this audit does
not justify replacing it with another tuned width.

## Gap fills and ambiguous bars

Three stop exits filled through their trigger at the open:

| Trade | Effective stop | Opening fill | Net P&L |
|---|---:|---:|---:|
| VITL BUY | 33.2710 | 30.6000 | −$1,095.59 |
| NKE BUY | 45.9489 | 45.5200 | −$356.72 |
| WMT SELL | 118.1719 | 119.9000 | −$470.20 |

These are correctly modeled gaps, not failures to enforce a guaranteed loss
limit. A stop price does not guarantee its execution price.
[SEC investor bulletin](https://www.investor.gov/introduction-investing/general-resources/news-alerts/alerts-bulletins/investor-bulletins-15).
NPCE and SPCE received favorable target gap fills at the open.

MDAI on January 27 and DORM on May 5 touched both stop and target without
an executable opening order establishing precedence. The engine chose the
stop conservatively. Daily OHLC cannot determine the actual sequence; no
change to target-first fills is warranted.

## Change made

**Disable the default generic target cap (`target_max_r=0`).** New fills keep
their own pattern's measured target, giving the already configured trailing
stop room to participate. A positive explicit `TARGET_MAX_R` still requests
a cap. Existing open positions retain their saved targets and stop rules;
neither ledgers nor the input export were rewritten. Restart a running
process to load the changed default. Pending entries resolve policy when
they fill; this change does not reconstruct their historical fills.

This removes a generic target override; it does not introduce a newly fitted
price threshold, remove take-profit orders, widen stops, or block shorts.
The change reaches both paper and historical-stream fills through their
shared `opening_candidate` path. The exit runtime files and engine hash are
unchanged, so saved positions do not trip the runtime-drift guard.

Exit-only comparisons keep the **same 65 entries, quantities, initial stops,
trailing geometry, 0.1% fees per side and zero slippage**:

| Exit policy | Closed / open at cutoff | Net P&L | Profit factor | Exit mix |
|---|---:|---:|---:|---|
| Saved 2R target cap | 65 / 0 | −$995.59 | 0.926 | 42 stops, 23 targets |
| Disable trailing only | 65 / 0 | −$995.59 | 0.926 | 42 stops, 23 targets |
| **Keep saved pre-cap measured targets** | **65 / 0** | **+$2,109.86** | **1.154** | **43 stops, 18 trails, 4 targets** |
| Remove targets entirely, diagnostic only | 65 / 0 | +$2,601.76 | 1.190 | 43 stops, 22 trails |

The shipped policy corresponds to preserving measured targets, **not** to
removing them. Fourteen paired trades improve, nine worsen and 42 are
unchanged. IRTC changes from +$573.22 to −$316.29. AIRO and VANI contribute
about **90% of the aggregate improvement**, making the result sensitive to
a few large winners. Shorts remain negative under the measured-target
comparison (−$4,290.88); exit changes do not repair poor short entries.

This is an in-sample policy comparison, not independent validation or a
full portfolio rescan. Different exits can change cooldowns, deduplication
and later entry eligibility. No holdout or parameter sweep was used to
claim calibrated settings. Forward validation with costs and an independent
period remains necessary; the earlier detector repairs also require a fresh
run before interpreting a combined result.

## Reporting and audit fixes

- Paper summary holding times now use simulated dates. The source summary
  said `0.0d` because it averaged the replay process's real execution time;
  exported simulated rows average **3.0 calendar days / 3.09 bars**.
- Exports add `exit_state`: effective capped hard stop, separate active
  trailing stop, confirmation flags, target and pending exit reason. For
  example, REZI's exported `stop=32.8733` differed from its effective
  `hard_stop=33.1643`. The original `stop` field stays available.
- New session marks save `exit_check`: original OHLC, orders before the bar,
  raw fill, slipped fill, reason, round-trip fees and net R on a close. This
  distinguishes the closing bar's true close from the fill placed in the
  final mark's `close` field. Capturing orders before the check prevents a
  newly armed trail from appearing to have protected that same bar.
- Closed rows add `net_r`; the existing `r` remains gross movement divided
  by initial risk. Reading exit state works on a copy and cannot activate a
  position's trailing stop. Old daily marks have no fabricated evidence.
- Export notes now distinguish a Kronos opposing vote from an executed
  Kronos exit; an adverse-move guard can defer the vote.

Reproduce the audit with:

```sh
.venv/bin/python -m analysis.paper_exit_audit pt.json \
  > /tmp/paper-exit-audit.json
```

The audit reads local files only. Multiple tape revisions must agree on
overlapping OHLC after matching entry, recorded survivor closes and baseline
exit. The fullest verified coverage is selected, and input/tape SHA-256
hashes are recorded. Rows after export time are excluded. Baseline mismatch
or conflicting revisions stop the audit. Counterfactual open remainders,
if any, include hypothetical liquidation fees/slippage instead of being
omitted from the comparison. This version requires causal-v1, fully closed
baseline books; it refuses open baseline positions or a changed engine.

Validation: **167 passed, 28 skipped** across paper account/book/lifecycle,
exit evidence, scanner stop/Kronos, causal execution, historical-stream,
paper/backtest parity, web paper API and export/audit suites. Local socket
access was needed for WebSocket and FastAPI TestClient checks. An existing
scan-clock test read the operator's persisted clock and expected an empty
account; it now persists and reloads a temporary ledger instead. The audit
JSON was reproduced byte-for-byte, the export SHA-256 is unchanged, and
`git diff --check` passed.
