---
name: upward-channel-conditions
description: "SHORT CHANNEL BREAK — bearish RSI divergence short strategy, locked C1-C24, results on NASDAQ-60 and Dow/NYSE-200 universes"
metadata: 
  node_type: memory
  type: project
  originSessionId: 4b4ee925-b5b8-40fb-93bf-32b1e81800ab
---

**Strategy name: SHORT CHANNEL BREAK** (named 2026-07-06, formally separated from the sibling long strategy [[long-upward-channel-conditions]]). Bearish RSI divergence short on channel breakdown, locked ruleset C1-C24.

**Pattern (C1–C12):** channel start = lowest low 200 bars pre-SH1; uptrend ≥15% (C1); valley > channel start (C2); ceiling intact (C3); SH1 RSI ≥55 (C4); SH2 ≥ SH1×1.02 (C5); RSI divergence SH2<SH1 (C6) by ≥5pts (C7); SH2 RSI 35–75 (C8); gap 20–180 bars (C9/C10); valley depth 2–25% (C11/C12).

**Entry (C13–C15 + v7):** 2 consecutive closes below the RISING lower channel line (valley low + channel slope), not the static valley; cancel if close > SH2 before break (C14); enter short at close of 2nd confirming bar (C15); dual RSI gate: RSI(break) < RSI(SH2).

**Exits (C16–C20):** stop = SH2×1.01 close-based; measured-move target = entry − channel width; 7% fixed cap; 15-bar time stop; trail activates at 4% gain, trails 2.5%.

**v9 earnings blackout:** skip trade if SEC EDGAR 8-K item 2.02 filing date falls within [entry, bar15+1d]. Yahoo Finance API is crumb-blocked; EDGAR works (User-Agent header required). Filter flipped NASDAQ results from −$810 to +$3,018.

**C21 reclaim exit (added 2026-07-03, v10):** after entry, exit at close when close > rising lower channel line AND bar makes higher high + higher low vs prior bar. Checked after stop/targets, before trail/time. Tested 2-bar confirmation (v11) — worse (net −$812 vs 1-bar): waiting for confirmation costs more on real breakdowns than it saves on head-fakes. 1-bar stays locked.

**C22 + C23 entry filters (LOCKED 2026-07-03, v12):** skip the trade entirely (no position taken) if either fails at entry time:
- **C22 freshness**: daysToBreak (SH2 → channel-break bar) > 20 → skip. Momentum from the divergence peak has a shelf life; slow drifts to the break fail more often.
- **C23 don't-chase**: dropFromSH2 (% price already fallen from SH2 high to entry price) > 15% → skip. Entries after a >15% slide are chasing an already-crowded short.
Both derived from comparing winner vs loser feature medians at entry time (daysToBreak: win median 7 vs loss median 10; dropFromSH2: win median 9.4% vs loss median 7.7% but with a fat >15% loss tail). Re-verified on cached bars split by original universe:
- NASDAQ-60: v10 27 trades $5,394 → v12 18 trades **$6,031** (worst loss −$1,377 → −$809)
- Dow/NYSE-200: v10 75 trades $9,223 → v12 63 trades **$10,345**
- Combined: v10 102 trades 52.0% WR $14,617 → v12 81 trades **55.6% WR $16,376** (worst loss capped at −$809)
Caveat: thresholds (20d / 15%) were tuned on the same 102-trade sample they're scored on — directionally sound (fresh breaks > stale drifts; don't chase >15% moves) but treat exact cutoffs as approximate, not sacred.

**Results ($10k/trade, 260 daily bars):**
- v10 A/B on identical cached data, 245/253 symbols (backtest_uc_v10.cjs): COMBINED v9 57.8% WR +$7,945 → v10 52.0% WR +$14,617 (Δ +$6,672). C21 fired 32×: 21 saves (+$11,434, incl. INTC +$2,277, LRCX +$1,572, FCX +$1,196) vs 11 gives-back (−$4,762, worst GS −$1,081 which would have hit 7% target). Win rate drops but avg P&L 0.77%→1.44% — cuts catastrophes, converts some winners to small losses.
- Earlier v9-only runs: NASDAQ-60 26 trades 65.4% WR +$3,018 (backtest_uc_v9.cjs); Dow/NYSE-200 72 trades 56.9% WR +$9,838 (backtest_uc_v9_dow200.cjs + _retry.cjs)

**C24 dual stop (LOCKED 2026-07-06, v14):** exit at `min(SH2×1.01, entry×1.05)`, whichever is hit first (checked at each day's close, same as all other exits). Added after WDC (new S&P-extension symbol) took a −$2,746 loss on a 26.8% overnight gap that the dynamic SH2×1.01 stop couldn't react to fast enough — WDC and Micron both surged that week on an AI memory-chip shortage rally, a sector catalyst no earnings filter or channel condition is designed to catch. Root-caused via SEC EDGAR direct query: confirmed no 8-K was filed near the gap date, so it wasn't an earnings-blackout gap, and C22/C23 both legitimately passed (fresh 2-day break, only 8.9% drop from SH2) — the trade looked clean at entry by every existing filter.
Tested 3%, 4%, and 5% fixed legs offline on the same 127-trade sample (backtest_uc_v13.cjs, patched per variant):
| Fixed leg | Win rate | Total P&L | Worst loss | Winners clipped |
|---|---|---|---|---|
| none (v12) | 55.9% | $21,216 | −$2,746 | — |
| 3% | 52.8% | $21,820 | −$524 | 5 (−$3,942), incl. INTC, HII |
| 4% | 54.3% | $22,429 | −$774 | 3 (−$1,739) |
| **5%** | **55.9%** | **$23,717** | **−$774** | **1 (−$125, trivial)** |
5% dominates: same win rate as no-fixed-stop baseline, highest total P&L of all variants, and only one near-zero-impact winner touched (VZ, same target hit 3 days earlier). 3%/4% clipped real winners (INTC +$729→−$365 at 3%) because entry sits well below SH2, so a tight % stop is usually the binding constraint — 5% is loose enough to let normal pullback-then-recover volatility play out while still capping violent gap-reversals (WDC −$2,746→−$524, TXT −$861→−$568, KLAC −$809→−$687).

**Bar cache:** barcache/{TICKER}.json holds 260 daily bars per symbol (366 cached: 245 original + 121 S&P-500 extension added 2026-07-06); earnings_cache.json holds EDGAR results — condition tweaks re-run fully offline in seconds via sim_one.cjs (single symbol), analyze_entries.cjs (feature/threshold scan), or backtest_uc_v14.cjs (locked ruleset, full sweep). sweep_sp500_new.cjs extends the cache to new symbols only (skips already-cached tickers).

**Current locked ruleset = v14**: C1-C20 (pattern+entry+exits) + C21 (1-bar reclaim) + C22 (freshness ≤20d) + C23 (don't-chase ≤15% drop) + C24 (dual stop: dynamic SH2×1.01 OR fixed entry×1.05, whichever first). Script: backtest_uc_v14.cjs. Latest full run (366 symbols): **127 trades, 55.9% WR, +$23,717, worst loss −$774**.

**Known weaknesses:** intraday/overnight gaps beyond ~5% still cause losses if the gap itself exceeds the fixed stop's distance (C24 caps damage, doesn't eliminate it); time exits dominate on slow NYSE names; steep-channel stocks (LRCX) reclaim quickly; sector-wide momentum surges (e.g. AI memory-chip rally hitting WDC+MU together) aren't detectable from single-symbol pattern/earnings data — would need a cross-symbol sector-correlation signal to flag in advance.

**Known gap (not yet fixed):** earnings_cache.json is populated incrementally per-script and isn't auto-refreshed for newly-added symbols — the 121 S&P-extension tickers had empty earnings entries until analyze_entries.cjs/backtest_uc_v11+ backfilled them via getEarningsMap's fetch-if-missing logic. backtest_uc_v14.cjs reads the cache as-is (no fetch), so any future newly-cached symbol needs an earnings-cache warm-up pass before its blackout filter is reliable.

**Data-integrity lessons:** TradingView resolves NYSE symbols to BATS: prefix — verify with EXACT ticker match on state.symbol (substring matching false-positives on short tickers like T/F/V); always fingerprint OHLCV against previous symbol AND re-check state after data settles; cross-check final results for byte-identical trades across different symbols (caught DHR/HAL, MS/DOW, JNJ/FI clones).

Scripts at C:\Users\dell\tradingview-mcp\. Related: [[upward-channel-drawing-feedback]].