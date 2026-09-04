# Rounding Bottom Trading System — Locked Conditions

Locked: 2026-06-25  
---

## Gate 1 — CANCELLED

Market regime filter permanently removed.  
Reason: rounding bottoms form during corrections (yellow/red markets), so Gate 1 blocked all meaningful patterns.

---

## Gate 2 — Upside Filter

| Parameter | Rule |
|-----------|------|
| Target | entry + 80% × (neckline − entry) |
| Minimum upside | ≥ 23% |
| Upside formula | (target − entry) / entry × 100 |

---

## Pattern Conditions

### C1 — Cup Depth

- Measured from neckline to cup bottom close
- Must be between 15% and 50%
- Below 15% = too shallow (not a meaningful correction)
- Above 50% = too deep (likely structural breakdown, not a base)

### C2 — RSI Oversold at Bottom

- Wilder RSI-14 on daily closes
- RSI at the cup bottom must be < 45

### C3 — Price Higher Lows on Recovery

- Price must make Higher Lows from the cup bottom toward the entry trigger
- Baked into the 2-day entry trigger (HH+HL requirement)

### C4 — RSI Higher Lows on Recovery

- RSI must make Higher Lows from the cup bottom toward the entry trigger
- Baked into the 2-day entry trigger (RSI rising requirement)

### C5 — Parabolic Shape Fit

- Window: symmetric ±60 bars centered on the cup bottom
- Method: least-squares quadratic regression on daily closes
- Requirement 1: coefficient `a > 0` (curve opens upward — concave up / U-shape)
- Requirement 2: ≥ 70% of closes fall within 5% of the fitted curve
- Fail reasons: `not concave-up` (V-shape or inverted), `< 70% within 5%` (too noisy)

### C6 — RSI Bullish Divergence or Uptrend

Primary — Classic divergence (any pair in the cup):  
Scan all consecutive RSI local-low pairs within the cup for: price making a lower low while RSI makes a higher low.

Fallback A — Overall cup comparison:  
If price at bottom < price at cup start, but RSI at bottom > RSI at cup start → pass.

Fallback B — 70% RSI uptrend during recovery:  
If ≥ 70% of bars from the cup bottom to the entry trigger show RSI rising bar-over-bar → pass.  
(Used for short cups where divergence detection isn't possible due to limited bars.)

---

## Entry Trigger — 2-Day HH+HL Confirmation

After the cup bottom is identified, scan forward (up to 120 bars) for 2 consecutive days that both satisfy:

| Condition | Day 1 | Day 2 |
|-----------|-------|-------|
| Price High | > previous High | > Day 1 High |
| Price Low | > previous Low | > Day 1 Low |
| RSI | > previous RSI | > Day 1 RSI |

Enter on the close of Day 2.  
Reset and rescan if the sequence breaks on any day.

---

## Trade Management

| Parameter | Rule |
|-----------|------|
| Position size | $10,000 per trade |
| Initial stop | 5% below entry price |
| Trailing stop | 15% below the highest high since entry |
| Active stop | max(initial stop, trailing stop) |
| Target | entry + 80% × (neckline − entry) |
| Exit — profit | Close when price ≥ target |
| Exit — loss | Close when close ≤ active stop |
| Intra-trade rules | Hold until stop or target only — no mid-trade exits |

---

## Backtest Results

| Metric | Value |
|--------|-------|
| Universe | Top 20 NASDAQ rounding bottom candidates |
| Trades entered | 2 |
| Wins | 2 |
| Win rate | 100% |
| Capital deployed | $20,000 |
| Total P&L | +$8,943 |
| ROI on deployed | +44.72% |

