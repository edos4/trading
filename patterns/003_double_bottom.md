# Double Bottom (W Pattern) — Complete Ruleset

This pattern is the exact bullish inverse of `002_double_top`.

## Pattern Detection

| # | Condition | Rule |
|---|-----------|------|
| C1 | Peak height | Price must rise ≥ 5% from L1 to the intervening peak high |
| C2 | RSI divergence | L2 RSI > L1 RSI (bullish divergence required) |
| C3 | Gap range | L2 must occur 8–90 bars after L1 |
| C4 | L2 price floor | L2 intraday LOW > L1 intraday LOW |
| C5 | L1 RSI ceiling | L1 RSI ≤ 30 (oversold at first trough) |
| C6 | L2 RSI ceiling | L2 RSI ≤ 50 (momentum has improved but is not already extended) |
| C7 | Intervening floor | No bar between L1 and L2 has LOW < L1 LOW |
| C8 | Divergence minimum | L2 RSI − L1 RSI > 3 pts |
| C9 | L2 confirmation | L2 is a local trough: the next 2 bars close higher than L2 close |
| C10 | Leg 2 volume | Avg volume of DOWN bars on Leg 2 < avg volume of UP bars (weak selloff) |
| C11 | L2 RSI floor | L2 RSI ≥ 39 (inverse of the Double Top RSI sweet spot) |
| C12 | L2 close confirmation | L2 closing price > L1 closing price |
| C13 | No post-L2 breach | Cancel pattern if any bar after L2 has LOW < L2 LOW before neckline break |

## Entry & Exit

| # | Condition | Rule |
|---|-----------|------|
| C14 | Entry | Buy on day 7 after L2 OR neckline break day, whichever comes first |
| C14 | Primary exit | Sell at 7% above neckline OR 5 days after neckline break, whichever comes first |
| C15 | Trailing stop | Exit if intraday LOW ≤ highest close since entry × 0.97 (3%) |

