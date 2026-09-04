# Bull Flag Pattern — Locked Conditions

**Status:** LOCKED 2026-07-06
**Direction:** Long only (bear flags tested and rejected — see below)
**Script:** `backtest_flag_final.cjs`
**Data:** `flagcache/{TICKER}.json` (500 daily bars per symbol, ~2yr)

## Result

| Metric | Value |
|---|---|
| Trades | 108 |
| Win rate | 43.5% (47W / 61L) |
| Avg return / trade | +1.99% |
| Avg winner | +7.74% |
| Avg loser | -2.44% |
| Profit factor | 2.44 |
| Worst loss | -3.00% (hard-capped by C18) |
| Unique symbols | 38 |
| Max concentration | ~6.5% (no single name dominates) |

## Universe: 60 semiconductor / hardware / datacenter-infrastructure suppliers

Not a generic large-cap NASDAQ list — a deliberately narrow universe built around the AI-capex-buildout thesis, after broader universes were tested and failed (see "How this universe was chosen" below).

- **Semiconductors / semi-equipment:** NVDA, AVGO, AMD, QCOM, INTC, MU, AMAT, LRCX, KLAC, ASML, MRVL, TXN, ON, ARM, ENTG, ONTO, COHR, LSCC, MTSI, POWI, AMKR, SITM, ALGM, NVMI, UCTT, DIOD
- **Hardware / networking / storage / test-equipment:** TER, GLW, WDC, STX, DELL, HPE, HPQ, NTAP, JNPR, ANET, FFIV, KEYS, TDY, APH, TEL, CDW, MCHP, MPWR, SWKS, QRVO, ADI, ETN, CIEN, FN, COHU, SMCI
- **Datacenter power / cooling / generation:** CEG, VRT, NVT, POWL, MOD, GEV, VST, TLN

## Conditions

### Pattern recognition (F1–F7, C17)

| # | Condition |
|---|---|
| F1 + C17 | Flagpole thrust: close-vs-open gain **≥25%** over a **3–40 bar** window |
| F2 | Pole volume expansion: pole avg volume **≥1.15x** the 20-bar baseline before the pole |
| C17 | Flag quality (supersedes F3): flag low sits **10–34% below the pole's high** (Bulkowski high-tight-flag retracement band) |
| F4 | Flag length: **4–15 bars** |
| F5 | Flag drift discipline: flag net drift **≤+6%** (a pause, not a second leg up) |
| F6 | Volume dry-up: flag avg volume **≤0.85x** pole avg volume (participation fades during consolidation) |
| C13 | Pre-existing trend: at pole start, close **≥ 50-day SMA** AND that SMA rising vs. 5 bars earlier |
| F7 | Entry trigger: first close **above flag high** within 20 bars of flag end, with that day's volume **≥ flag average** |
| F10 | Only one open trade per symbol at a time |

### Exit / risk management (F8–F9, C18)

| # | Condition |
|---|---|
| F8 + C18 | Initial stop = **max(flag low, entry × 0.97)** — whichever is tighter. Caps worst-case initial risk at 3% regardless of how wide the flag's geometry is. |
| F9 | Trailing stop: track the highest **close** since entry; stop ratchets up to **3% below that peak**, never loosens back down |

**In plain terms:** a violent, quality-tested rally (the pole) pauses in a tight, low-volume consolidation (the flag) that only retraces 10–34% of the pole's height, confirmed by a 50-day-SMA uptrend already in place. Enter on a volume-confirmed breakout above the flag. Risk is capped at 3% from day one; once the trade is working, the stop trails 3% below the highest close, letting winners run while locking in gains as they build.

## Why C17's pole threshold is +25%, not Bulkowski's +90%

Bulkowski's actual "high-tight-flag" research uses a much more extreme pole (≥90% rise in <42 bars) — but that's calibrated to small/micro-cap momentum stocks. This universe is mega/large-cap, where a 90% move in two months is essentially unheard of. The relaxed +25% threshold was tested empirically for this universe rather than using the literal spec.

## Why C18 (the 3% hard stop) exists

Before C18, the stop was purely geometric (flag low), with no cap on how far that could sit from entry. On a wide flag, that meant unbounded initial risk. Confirmed real losses from this gap: ZS -8.94%, DXCM -6.39% (earlier NASDAQ-generic universe), **POWL -10.24%** (this universe — entered $194.85 Mar 25, stopped out at the flag low of $174.91 the very next day, Mar 26; the trailing stop never got a chance to engage because price never closed higher first).

C18 fixes this: `stop = max(flagLow, entry * 0.97)`. Effect on this universe:

| | Without C18 | With C18 (locked) |
|---|---|---|
| Trades | 105 | 108 |
| Win rate | 47.6% | 43.5% |
| Avg/trade | +2.26% | +1.99% |
| Profit factor | 2.74 | 2.44 |
| Worst loss | -10.24% | **-3.00%** |

Accepted trade-off: ~0.27%/trade average return given up in exchange for eliminating unbounded tail risk on any single trade.

## Rejected conditions (tested, did not survive)

- **C11 — earnings blackout** (skip trades overlapping an SEC EDGAR 8-K item-2.02 filing date): redundant once C13 was in place; C13 already filters out most earnings-driven false poles as a side effect.
- **C12 — 5%-of-entry risk cap as an entry filter** (skip the trade entirely if flag-low risk >5%): mathematically conflicts with F3/C17's geometry — collapsed the sample from 95 to 2 trades. Superseded by C18, which caps the same risk on the **exit** side instead of rejecting the trade outright — that's why C18 works where C12 didn't.
- **C14 — stronger breakout volume** (≥1.5x flag average instead of ≥1.0x): hurt every metric. The flag average is already artificially suppressed by F6, so 1.5x of an already-low number is a stricter bar than intended.
- **Partial exit at half measured-move target**: can only ever cap upside on trades already winning enough to reach the target — never rescues a loser. Diluted the big winners C17 is good at producing.
- **Bear flags (short side)**: rejected outright — 25.5% win rate, net loser. In an uptrending universe, breakdowns tend to get bought rather than continue lower.

## How this universe was chosen

1. **Generic 78 "top NASDAQ names"** (mixed sectors — mega-cap tech, consumer, biotech, staples): 36 trades, 38.9% WR, PF 2.63.
2. **Broadened to 211-ticker S&P 500** (all sectors): net loser, PF 0.71. Energy + materials cyclicals went 0-for-13; tech/semi-hardware names held up fine.
3. **125-ticker tech/healthcare/industrial-only S&P batch**: numbers looked good (PF 2.51) but was 80% concentrated in just 5 hardware names; healthcare and industrials contributed almost nothing. This pointed to the real edge being AI-infrastructure/semiconductor-hardware momentum specifically, not broad tech.
4. **Built this dedicated 60-ticker universe** on that sharper thesis: beat the original 78-name result on every metric with much better diversification (38 unique symbols vs. 19, no concentration problems).
5. **Tested expanding to 119 tickers** (same thesis, more peripheral names): confirmed the 60-ticker scope is correctly sized — the original 60 perform identically whether run alone or embedded in the larger set, while the 59 new additions are barely profitable on their own (PF 1.18 vs. the core's 2.44). Not adopted.

## Known limitation

The stop is still a fixed 3%/geometric hybrid, not volatility-adjusted — it doesn't scale with each stock's actual historical volatility. Untested candidate refinements: an ATR-based/Chandelier trailing stop instead of a flat 3%, or a minimum reward:risk ratio filter (≥2:1 measured-move-to-initial-risk) as a complementary entry-quality screen.