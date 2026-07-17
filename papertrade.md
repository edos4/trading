# Paper Trading — Plan (plain-English version)

## What we're building

A "practice mode" that runs the bot against **live, real-time market data**,
lets it detect setups and pretend to place trades — but with fake money
instead of real money. It's the difference between the backtester (which
replays the *past*) and this (which watches the *present*, live, without
risking capital). No real broker or real order gets touched.

You'll be able to run it two ways, same as the backtester:
- From the command line, left running in the background like the live
  scanner.
- From the desktop app (`main.py --ui`), with a "Paper Trading" screen next
  to the existing "Backtest" screen — start/stop buttons, a table of open
  positions, a table of closed trades, and a running equity chart.

## How it will work

1. **Same scanning loop, same patterns.** It reuses the exact same
   market-scanning process that already runs live — fetching fresh prices,
   feeding them to every enabled pattern, and checking whether a setup
   appears. Nothing about pattern detection changes.

2. **Instead of "would trade" being logged and forgotten, it actually
   opens a fake position.** Today, when the live scanner sees a valid
   signal, it just logs a line saying "would have bought this" and moves
   on — there's no real order execution wired up. Paper trading changes
   that one step: instead of just logging it, it opens a simulated
   position with fake money, and tracks it going forward.

3. **Managing open trades uses the exact same rules as the backtester.**
   Stop-losses, profit targets, and trailing stops are checked the same
   way, using the same logic already proven out in backtesting — so
   results here are directly comparable to backtest results, not a
   different set of rules.

4. **A virtual account keeps score.** It tracks: how much fake cash is
   left, which positions are currently open, every trade that's closed,
   and a running equity curve — exactly like the backtester's report, just
   built up trade-by-trade in real time instead of all at once.

5. **Progress is saved to disk continuously**, so if you stop and restart
   it, it picks back up where it left off instead of losing the running
   account. A reset option lets you wipe it and start over with a fresh
   fake balance.

6. **The same safety limits apply**: a cap on how many positions can be
   open at once, a cap on how much of the account any one trade can use,
   and a daily loss limit — the same knobs that exist in the settings
   today but currently aren't being used by anything.

## What this is NOT

- Not real trading. No real broker, no real orders, no real money — ever,
  in this feature.
- Not multiple strategies running side-by-side in one session — one
  virtual account per run.
- Not tick-by-tick precision. Trades fill and get checked on the same
  schedule the live scanner already uses (e.g. once per scan interval),
  same resolution as the backtester — not instant, real-time execution.

## Decisions still open for review

1. **Fill price** — when a setup is detected, should the fake trade fill
   at the price the setup was detected at, or wait for the *next* price
   update (which is what the backtester does, to avoid an unrealistic
   advantage)?
2. **Should the vision/AI double-check step run in paper trading too?**
   The live scanner has an extra AI-vision confirmation step before a
   real trade would fire. Keeping it makes paper trading behave exactly
   like live trading would; skipping it means more trades happen faster,
   which may be useful for gathering data quicker.
3. **One shared practice account, or named sessions?** So you could run
   more than one paper-trading setup at the same time and compare them,
   instead of only ever having one running account.
