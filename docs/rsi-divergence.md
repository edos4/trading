# RSI Divergence in Paper

Reference: [Trading Mode reel](https://www.facebook.com/reel/1695224485293688),
reviewed in the public Facebook player on 13 November 2026. The 12-second
animation shows a higher price high, a lower RSI high, a short bracket above
earlier support, a stop above the latest high, and a subsequent fall to support.
The caption is generic trading advice. The player exposed no downloadable
video URL; downloading the video file was unsuccessful.

Each Paper book has an **RSI Divergence** checkbox, initially off, on its own
row. It is its own strategy. Checking it clears and locks the pattern scan,
Patternsv2, Indicators-only and the Builder strategy. A start that also asks
for patterns, Patternsv2, a Builder strategy or a TradingView stack is refused.
Builder rule filters remain available. RSI supports daily and weekly scans.
Patternsv2 scans daily bars and never shares a book with this strategy.

The video does not state numeric parameters or a precise confirmation rule.
The following are explicit implementation choices, rather than claimed video
instructions or optimized settings:

- Wilder RSI(14). Compare RSI at the same bars as two consecutive strict price
  swing highs (bearish) or lows (bullish). Both price pivots need two candles on
  each side; their separation must be 5–60 candles.
- Bearish: price makes a higher high and RSI a lower high. Bullish is the
  mirrored extension: a lower price low and higher RSI low. No fixed 70/30
  threshold or hidden-divergence rule is added.
- After the second pivot is confirmed, the first close below that pivot
  candle's low confirms a short; above its high confirms a long. Allow five
  completed candles including the pivot-confirmation candle. Emit once per
  pair. A fresh extreme beyond the second pivot invalidates it.
- The stop lies 0.1 ATR(14) beyond the second pivot. The target is the lowest
  intervening low for shorts or highest intervening high for longs. Reject
  setups whose target is already passed or whose rounded bracket is invalid.
- All decisions use only completed observations; fills use the existing next
  opening execution path and revalidate the saved bracket after gaps/slippage.
  The legacy fixed-stop override does not replace this strategy's swing stop.

The existing funded-account limits, fees, slippage, independent paper loss/trail
protection, optional target cap, volume/Laya/Builder filters and Kronos controls
continue to apply. Every BUY still needs the existing daily DII bulls-uptrend
gate. PH remains long-only. These controls can reject a setup or close a trade
before its structural stop/target. The checkbox does not restart an active run.

Run settings and signal/trade evidence retain the selected strategy, frozen
policy, pivot dates, prices, RSI values, confirmation date and bracket. Source
hashes include the detector and its indicator dependencies. These rules have
not established profitability; evaluation belongs to a separate funded replay.
Paper charts for this strategy use Wilder RSI and draw the saved price and RSI
divergence lines. RSI pivots stay on their native timeframe when changing views.
