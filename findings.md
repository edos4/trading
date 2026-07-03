# Code Review Findings

Scope reviewed: `main.py`, `core/backtester.py`, `core/scanner.py`, `data/tv_client.py`, `data/ohlcv_store.py`, and all modules in `patterns/`.

## Findings

### 1. Critical: short-trade exits are simulated with long-side price checks

Evidence:
- `patterns/pattern_002_double_top.py:132-143` emits a `SELL` signal with `stop_loss = close * 1.03` and `take_profit = neckline * 0.93`, so the stop is above entry and the target is below entry.
- `core/backtester.py:261-269` checks every position with:
  - `candle.low <= position.stop_loss`
  - `candle.high >= position.take_profit`
- `core/backtester.py:295-298` later calculates short P&L correctly, but the exit trigger was already chosen using long-side trigger logic.

Why this is wrong:
- For a short, the stop should trigger when `high >= stop_loss`.
- For a short, the target should trigger when `low <= take_profit`.
- With the current code, a short stop above entry will often trigger immediately because the next candle's `low` is normally below that stop, even if price never traded up to the stop. Likewise, a short take-profit below entry can be considered hit because `high >= take_profit` is usually true.

Impact:
- Double-top backtest results are not valid. Short trades can be stopped or targeted on prices that were not actually touched in the required direction.

### 2. High: double-top documented exit rules are not implemented in the backtest

Evidence:
- `patterns/double_top.md:72-74` says the exit is `7% below neckline OR 5 days after neckline break, whichever earlier`, plus a trailing stop at `3% above lowest close since entry`.
- `patterns/pattern_002_double_top.py:72-73` names `TAKE_PROFIT_BELOW_NK` and `TRAILING_STOP_PCT`.
- `patterns/pattern_002_double_top.py:140-143` returns a fixed `stop_loss` and fixed `take_profit`; it does not return the neckline-break date, lowest close since entry, or a max holding period after neckline break.
- `core/backtester.py:261-269` only checks fixed stop/target levels.

Why this is wrong:
- The documented trailing stop is path-dependent: `lowest close since entry * 1.03`.
- The documented time exit is also path-dependent: `5 days after neckline break`.
- The current backtest does neither, so it is not testing the locked double-top rule set in `patterns/double_top.md`.

Impact:
- The backtest cannot be used to validate the stated swing-trading double-top strategy until these exits are modeled.

### 3. High: backtest takes signals that live/paper scanning would reject

Evidence:
- `core/scanner.py:136-153` rejects any signal below `settings.vision_min_indicator_confidence` even when vision is disabled.
- `config.py:46` sets that threshold to `0.6`.
- `core/backtester.py:236-246` takes every non-`None` signal without checking confidence.
- `patterns/pattern_001_ema_crossover.py:98-125` can return crossover signals with confidence below `0.6`.

Why this is wrong:
- The backtest and scanner do not share the same trade gate.
- A backtest may include low-confidence trades that live/paper mode would skip.

Impact:
- Backtest trade count, win rate, and P&L can materially diverge from live behavior.

### 4. High: backtest history comes from Yahoo while live scanning uses TradingView data

Evidence:
- `data/tv_client.py:1-7` says OHLCV comes from TradingView and "no Yahoo Finance".
- `data/tv_client.py:69` sets `_CHART_API = "https://query1.finance.yahoo.com/v8/finance/chart"`.
- `data/tv_client.py:301-368` fetches historical candles from that Yahoo endpoint.
- `core/backtester.py:315-316` uses `_fetch_history_chart()` directly for backtests.
- `main.py:67-78` selects symbols from TradingView, then `Backtester` fetches history through that Yahoo-backed path.

Why this is wrong:
- Backtest signals are generated from a different data source than live scanning.
- Exchange-specific TradingView symbols can map differently on Yahoo, and live mode even replaces the latest history bar with TradingView screener data in `data/tv_client.py:374-386`, while backtest mode does not.

Impact:
- The backtest is not a faithful replay of the data source used by the trading scanner.

### 5. Medium: EMA crossover pattern is still a template and lacks a swing-trade exit model

Evidence:
- `patterns/pattern_001_ema_crossover.py:1-18` labels the pattern as a template and says to replace parameters and logic with the actual rules.
- `patterns/pattern_001_ema_crossover.py:135-144` returns `TradeSignal` without `stop_loss` or `take_profit`.
- `core/backtester.py:212-229` does not evaluate new opposite signals while a position is open; it only waits for stop/target exits.
- `core/backtester.py:250-257` force-closes remaining positions at the final candle.

Why this is wrong:
- EMA trades have no modeled swing-trade exit except end-of-data.
- A later opposite crossover will not close or reverse the open position because signal detection is skipped while `open_position` is not `None`.

Impact:
- EMA crossover backtest results are not meaningful for swing trading as written.

### 6. Medium: reported performance metrics are not portfolio-valid for swing trading

Evidence:
- `core/backtester.py:72-78` reports total/average P&L as a simple sum and mean of trade percentages.
- `core/backtester.py:80-87` computes drawdown from cumulative trade percentages, not an equity curve.
- `core/backtester.py:89-96` annualizes per-trade returns with `sqrt(252)`.
- `core/backtester.py:184-188` loops symbol/timeframe results sequentially and appends trades, but does not model cash, position sizing, overlapping positions, max open positions, commissions, slippage, borrow costs, or order timing.

Why this is wrong:
- Swing strategies can hold overlapping positions for days or weeks.
- A valid portfolio backtest needs dated equity, capital allocation, position sizing, and realistic execution assumptions.

Impact:
- The current summary is useful as a rough signal scan, but not as a valid swing-trading performance backtest.

## Double-Check Notes

- I re-checked the critical short-exit issue against both sides of the code: `SELL` signals put stops above entry in `patterns/pattern_002_double_top.py:140`, while `_check_exit()` uses `low <= stop_loss` for all actions in `core/backtester.py:265`. That confirms the finding.
- I also re-checked the documented double-top exit rules in `patterns/double_top.md:72-74` against the only backtest exit function in `core/backtester.py:261-269`; the 5-day neckline-break exit and trailing stop are absent.
- Syntax check passed with `python3 -m compileall main.py core patterns data analysis config.py utils`.
