Pattern structure (C1 – C12)
C#	Value	Condition
C1	≥ 15%	Channel start → SH1 uptrend ≥ 15% (channel start = lowest low in 200 bars before SH1)
C2	—	Valley low > channel start low (ascending floor — no round-trip to the bottom)
C3	—	Ceiling intact — no bar between SH1 and SH2 closes above SH2 high
C4	RSI ≥ 55	SH1 RSI ≥ 55 (confirms SH1 is a genuine overbought peak, not noise)
C5	SH2 ≥ SH1 × 1.02	SH2 price at least 2% above SH1 (higher high — price is still making progress)
C6	—	SH2 RSI < SH1 RSI (bearish divergence — momentum weakening at new price high)
C7	≥ 5 pts	RSI divergence gap ≥ 5 points (avoids near-identical RSI prints)
C8	35 – 75	SH2 RSI in 35–75 range (too low = already sold off; too high = still ripping)
C9	≥ 20 bars	Minimum 20 bars between SH1 and SH2 (pattern needs time to develop)
C10	≤ 180 bars	Maximum 180 bars between SH1 and SH2 (prevents stale / multi-year patterns)
C11	≥ 2%	Valley depth ≥ 2% below SH1 (real pullback, not a 1-bar wick)
C12	≤ 25%	Valley depth ≤ 25% below SH1 (pullback must stay within the channel)
Entry trigger (C13 – C15 + dual RSI)
C#	Value	Condition
C13	2 consec. closes	Entry triggered by 2 consecutive closes below the rising lower channel line
lower line at bar k = valley low + slope × (k − valley idx) where slope = (SH2 − SH1) / (i2 − i1)
C14	cancel	Pattern cancelled if any close exceeds SH2 high before the channel break is confirmed
C15	close	Entry at close of the 2nd confirming bar (short entry)
v7+	RSI(break) < RSI(SH2)	Dual RSI gate — RSI at the break bar must also be below SH2 RSI (divergence is still declining at entry)
Trade management (C16 – C20)
C#	Value	Condition
C16	SH2 × 1.01	Hard stop — exit if close exceeds SH2 × 1.01 (pattern invalidated)
C17	entry − width	Measured move target = entry price − channel width (project channel height below break)
C18	7% gain cap	Fixed profit cap at 7% from entry — takes first of C17 or C18 (whichever is closer)
C19	15 bars	Time stop — exit at close of bar 15 if none of the above triggered
C20	4% → 2.5% trail	Trailing stop activates after 4% gain; trails 2.5% above best close since entry
v9 filter
Value	Condition
v9	earnings blackout	Skip trade if any EDGAR 8-K item 2.02 earnings date falls within [entry bar, bar 15]
Source: SEC EDGAR submissions API — filing date of 8-K with item 2.02 (Results of Operations)