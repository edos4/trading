*Head & Shoulders — 17 Conditions (LOCKED 2026-06-29)*

| ID | Condition | Value |
|----|-----------|-------|
| C1 | HEAD is local close maximum | ±4 bars, strict inequality |
| C2 | LS is local close maximum below HEAD | Best peak in [HD−80, HD−10] bars |
| C3 | LN depth (LS→LN) ≥ 5% | (LS − LN) / LS ≥ 0.05 |
| C4 | HEAD ≥ 10% above neckline | (HD − neck) / neck ≥ 0.10 |
| C5 | Neckline slope | Skew ≤ +10%; abs(skew) ≤ 30% |
| C5b | RN depth (HD→RN) ≥ 5% | (HD − RN) / HD ≥ 0.05 |
| C6 | RS forms 3–50 bars after RN | MIN_RS_AFTER_RN = 3 |
| C6b | RS ≥ 5% above neckline | (RS − neck) / neck ≥ 0.05 |
| C7 | RS close < LS close | Right shoulder lower than left |
| C8 | RSI divergence LS→HEAD ≥ 2 pts | Wilder RSI(14): lsRSI − hdRSI ≥ 2 |
| C9 | RSI at RS < RSI at HEAD | Any amount |
| C10 | RS RSI ≤ 60 | Hard cap — overbought RS rejected |
| C11 | Pattern span LS→RS: 20–120 bars | RS side ≤ 2.5× LS side |
| C12 | 2 closes after RS both below RS close | closes[rs+1] < rs AND closes[rs+2] < rs |
| C13 | No close above HEAD after RS | Hard invalidation — cancels pattern |
| *C14* | *LOCKED* Entry: 2nd consecutive close below neckline OR day 7 (earlier) | entryIdx = min(day7, consecBreakIdx) |
| C15 | Measured target | neckline − (HEAD − neckline) |
| C16 | Exit timer: 10 days after entry | Hard close at day 10 |
| *C17* | *LOCKED* Trailing stop: 3% on CLOSE above running low | Close-based only — no intraday trigger |