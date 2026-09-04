# Descending Channel Trading System — Locked Conditions

This pattern is the exact bullish inverse of `006_upward_channel`.

## Pattern Structure (C1–C12)

| C# | Value | Condition |
|----|-------|-----------|
| C1 | ≥ 15% | Channel start → SL1 downtrend ≥ 15% (channel start = highest high in 200 bars before SL1) |
| C2 | — | Peak high < channel start high (descending ceiling — no round-trip to the top) |
| C3 | — | Floor intact — no bar between SL1 and SL2 closes below SL2 low |
| C4 | RSI ≤ 45 | SL1 RSI ≤ 45 (confirms SL1 is a genuine oversold trough, not noise) |
| C5 | SL2 ≤ SL1 × 0.98 | SL2 price at least 2% below SL1 (lower low — price is still making progress) |
| C6 | — | SL2 RSI > SL1 RSI (bullish divergence — momentum strengthening at new price low) |
| C7 | ≥ 5 pts | RSI divergence gap ≥ 5 points |
| C8 | 25–65 | SL2 RSI in 25–65 range (inverse of the Upward Channel 35–75 band) |
| C9 | ≥ 20 bars | Minimum 20 bars between SL1 and SL2 |
| C10 | ≤ 180 bars | Maximum 180 bars between SL1 and SL2 |
| C11 | ≥ 2% | Peak height ≥ 2% above SL1 |
| C12 | ≤ 25% | Peak height ≤ 25% above SL1 |

## Entry Trigger (C13–C15 + dual RSI)

| C# | Value | Condition |
|----|-------|-----------|
| C13 | 2 consecutive closes | Entry triggered by 2 consecutive closes above the falling upper channel line. Upper line at bar k = peak high + slope × (k − peak idx), where slope = (SL2 − SL1) / (i2 − i1) |
| C14 | cancel | Pattern cancelled if any close falls below SL2 low before the channel break is confirmed |
| C15 | close | Entry at close of the 2nd confirming bar (long entry) |
| dual RSI | RSI(break) > RSI(SL2) | RSI at the break bar must also be above SL2 RSI |

## Trade Management (C16–C20)

| C# | Value | Condition |
|----|-------|-----------|
| C16 | SL2 × 0.99 | Hard stop — exit if price falls below SL2 × 0.99 |
| C17 | entry + width | Measured move target = entry price + channel width |
| C18 | 7% gain cap | Fixed profit cap at 7% from entry — takes first of C17 or C18 (whichever is closer) |
| C19 | 15 bars | Time stop — exit at close of bar 15 if none of the above triggered |
| C20 | 4% → 2.5% trail | Trailing stop activates after 4% gain; trails 2.5% below best close since entry |

## Earnings Blackout

Skip the trade if any SEC EDGAR 8-K item 2.02 earnings date falls within the entry bar through bar 15 window.

