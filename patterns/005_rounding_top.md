# Rounding Top Trading System — Locked Conditions

This pattern is the exact bearish inverse of `004_rounding_bottom`.

## Gate 1 — CANCELLED

Market regime filtering is not part of this pattern.

## Gate 2 — Downside Filter

| Parameter | Rule |
|-----------|------|
| Target | entry − 80% × (entry − neckline) |
| Minimum downside | ≥ 23% |
| Downside formula | (entry − target) / entry × 100 |

## Pattern Conditions

### C1 — Dome Height

- Measured from neckline to dome top close
- Must be between 15% and 50%
- Below 15% = too shallow
- Above 50% = too extended to be a stable rounding top

### C2 — RSI Overbought at Top

- Wilder RSI-14 on daily closes
- RSI at the dome top must be > 55

### C3 — Price Lower Highs on Decline

- Price must make Lower Highs from the dome top toward the entry trigger
- Baked into the 2-day entry trigger (LL+LH requirement)

### C4 — RSI Lower Highs on Decline

- RSI must make Lower Highs from the dome top toward the entry trigger
- Baked into the 2-day entry trigger (RSI falling requirement)

### C5 — Parabolic Shape Fit

- Window: symmetric ±60 bars centered on the dome top
- Method: least-squares quadratic regression on daily closes
- Requirement 1: coefficient `a < 0` (curve opens downward — concave down / inverted U-shape)
- Requirement 2: ≥ 70% of closes fall within 5% of the fitted curve

### C6 — RSI Bearish Divergence or Downtrend

Primary — Classic divergence (any pair in the dome):  
Scan all consecutive RSI local-high pairs within the dome for price making a higher high while RSI makes a lower high.

Fallback A — Overall dome comparison:  
If price at top > price at dome start, but RSI at top < RSI at dome start → pass.

Fallback B — 70% RSI downtrend during decline:  
If ≥ 70% of bars from the dome top to the entry trigger show RSI falling bar-over-bar → pass.

## Entry Trigger — 2-Day LL+LH Confirmation

After the dome top is identified, scan forward (up to 120 bars) for 2 consecutive days that both satisfy:

| Condition | Day 1 | Day 2 |
|-----------|-------|-------|
| Price High | < previous High | < Day 1 High |
| Price Low | < previous Low | < Day 1 Low |
| RSI | < previous RSI | < Day 1 RSI |

Enter short on the close of Day 2.  
Reset and rescan if the sequence breaks on any day.

## Trade Management

| Parameter | Rule |
|-----------|------|
| Position size | $10,000 per trade |
| Initial stop | 5% above entry price |
| Trailing stop | 15% above the lowest low since entry |
| Active stop | min(initial stop, trailing stop) |
| Target | entry − 80% × (entry − neckline) |
| Exit — profit | Close when price ≤ target |
| Exit — loss | Close when close ≥ active stop |
| Intra-trade rules | Hold until stop or target only — no mid-trade exits |

