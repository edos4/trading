Reverting to 3% first.

3% locked in. Here are all 15 conditions in full:

---

## Double Top (M Pattern) — Complete Ruleset

### Pattern Detection

| # | Condition | Rule |
|---|-----------|------|
| C1 | Valley depth | Price must drop ≥ 5% from H1 to valley low |
| C2 | RSI divergence | H2 RSI < H1 RSI (bearish divergence required) |
| C3 | Gap range | H2 must occur 8–90 bars after H1 |
| C4 | H2 price ceiling | H2 intraday HIGH < H1 intraday HIGH |
| C5 | H1 RSI floor | H1 RSI ≥ 70 (overbought at first peak) |
| C6 | H2 RSI floor | H2 RSI ≥ 50 (still has momentum, not already dead) |
| C7 | — | (merged into C11) |
| C8 | Divergence minimum | H1 RSI − H2 RSI > 3 pts |
| C9 | — | (merged into C11) |
| C10 | Leg 2 volume | Avg volume of UP bars on Leg 2 < avg volume of DOWN bars (weak recovery) |
| C11 | H2 RSI ceiling | H2 RSI ≤ 61 (sweet spot: >61 = 0 wins historically) |
| C12 | H2 close confirmation | H2 closing price < H1 closing price |
| C13 | No post-H2 breach | Cancel pattern if any bar after H2 has HIGH > H2 HIGH before neckline break |

### Entry & Exit

| # | Condition | Rule |
|---|-----------|------|
| C14 | Entry | Short on *day 7 after H2* OR *neckline break day*, whichever comes first |
| C14 | Primary exit | Cover at *7% below neckline* OR *5 days after neckline break*, whichever comes first |
| C15 | Trailing stop | Exit if intraday HIGH ≥ lowest close since entry × *1.03* (3%) |

### Backtest Results (60 NASDAQ stocks, 1D, ~1 year)

| Metric | Value |
|--------|-------|
| Patterns found | 7 |
| Win rate | *85.7%* (6/7) |
| Avg P&L per trade | *+3.81%* |
| Total P&L | *+26.70%* |
| Only loss | AMZN −3.00% (macro bounce at entry) |

Now saving to memory.

Now updating the memory file with the complete final ruleset.

Now update the MEMORY.md index line.

Memory updated. Here's the final locked ruleset:

---

## Double Top — 15 Conditions (FINAL)

### Detection (C1–C13)
1. Valley depth ≥ 5% below H1 HIGH
2. H2 RSI < H1 RSI (bearish divergence)
3. H2 occurs 8–90 bars after H1
4. H2 intraday HIGH < H1 intraday HIGH
5. H1 RSI ≥ 70
6. H2 RSI ≥ 50
7. No bar between H1 and H2 has HIGH > H1 HIGH
8. RSI divergence (H1−H2) > 3 pts
9. H2 is a local peak (next 2 bars close lower)
10. Leg 2 avg UP bar volume < avg DOWN bar volume
11. H2 RSI ≤ 61 (sweet spot; >61 = 0 wins)
12. H2 closing price < H1 closing price
13. No bar after H2 has HIGH > H2 HIGH before neckline break (else cancel)

### Entry & Exit (C14–C15)
14. *Entry:* day 7 after H2 OR neckline break, whichever earlier — *Exit:* 7% below neckline OR 5 days after neckline break, whichever earlier
15. *Trailing stop:* 3% above lowest close since entry (locked — tested 2.77%/3%/3.33%, 3% wins)

### Results
*7 patterns · 85.7% win rate · Avg +3.81% · Total +26.70%*