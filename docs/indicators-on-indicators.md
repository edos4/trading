# Indicators on indicators

Status: implemented.

A new **Indicators** tab on `python main.py --web` loads TradingView studies whose source is another study’s plot — TradingView’s “indicator on indicator” — using the paid account’s browser session. A saved stack paper-trades through the existing US and PH books.

Decisions locked with the request:

- The tab is a top-level page, same nav as Paper and Patterns.
- Values come from TradingView as the subscribed user. The Python detectors in `indicators/` stay as they are.
- Paper fills use the current paper ledger, caps, and session rules.
- There is no TradingView API key. Auth is the two session cookies below.

## 1. What the subscription buys

TradingView counts “indicator on indicator” separately from “indicators per chart”. Figures are from [the pricing page](https://www.tradingview.com/pricing/) as of 2026-10-12. Recheck that page before buying; the vendor changes the table.

| Plan | Indicator on indicator | Indicators per chart | Parallel chart connections | Historical bars |
| --- | ---: | ---: | ---: | ---: |
| Basic (free) | 1 | 2 | 2 | 5K |
| Essential | 1 | 5 | 10 | 10K |
| Plus | 9 | 10 | 20 | 10K |
| Premium | 24 | 25 | 50 | 20K |
| Ultimate | 49 | 50 | 200 | 40K |

Plus is the first plan that can stack more than one child study. A paper scan holds one chart socket per symbol in flight, so `TV_CHART_CONNECTIONS` must stay at or under that plan’s parallel-connection cap. Daily swing history (about 450 bars) fits every row in the table.

An invite-only or paid script works only when this same account already has access to it on tradingview.com. The bot does not buy script access and does not try scripts the session is not entitled to.

## 2. `.env` keys

TradingView does not issue an API key for chart studies. Log into tradingview.com with the subscribed account, open DevTools → Application → Cookies → `https://www.tradingview.com`, and copy these two cookie values. Do not put the TradingView password in `.env`.

```
# Subscribed account. Cookie values, not the password.
# DevTools → Application → Cookies → https://www.tradingview.com
TV_SESSIONID=
TV_SESSIONID_SIGN=

# Must be <= the plan row in the table above.
# Plus example: 9 stacked studies, 20 chart sockets.
TV_INDICATOR_ON_INDICATOR_LIMIT=9
TV_CHART_CONNECTIONS=20
```

`TV_SESSIONID` is the `sessionid` cookie. `TV_SESSIONID_SIGN` is the `sessionid_sign` cookie. Both are required. A missing or expired pair makes the Indicators tab show logged-out, and paper start for a stack refuses to run.

Leave the values empty until the cookies are copied. Empty means the feature is off; the rest of `--web` keeps working.

`config.py` is the only reader (`settings.tv_sessionid`, `settings.tv_sessionid_sign`, `settings.tv_indicator_on_indicator_limit`, `settings.tv_chart_connections`). Nothing else reads `os.environ`.

## 3. What exists today

`python main.py --web` nav is Explorer, Backtest, Paper, Replay, Patterns (`web/templates/base.html`). Routes live in `web/app.py`. There is no `/indicators` page.

`indicators/` is a set of local `BaseIndicator` detectors (Supertrend and the rest). `indicators.discover()` loads them. Paper and Explorer already have an **Indicators only** checkbox that scans those local classes and skips pattern discovery (`core/scanner.py`, `POST /api/paper/start` with `indicators_only`). That path never asks TradingView to compute a study, and it cannot set one study’s source to another study’s plot.

`data/tv_client.py` talks to the public screener and to `tradingview-mcp` for a coin snapshot. It has no logged-in chart session.

Paper fills already go through `PaperAccount.open_position`. Indicators-only books allow one open position per symbol and refuse a new one once open notional uses up the account (`core/paper_trader.py`). PH stays long-only. Those rules stay.

## 4. Architecture

```
Indicators tab (/indicators)
    │  saved stack: studies + source links + signal rule
    ▼
TvStudyClient  (one process-wide chart socket pool)
    │  TV_SESSIONID + TV_SESSIONID_SIGN
    │  wss chart session, studies created parent-then-child
    ▼
plot series on TradingView’s own bars
    │
    ├─ tab preview for one symbol
    └─ paper scan → TradeSignal → PaperAccount (same ledger the Paper tab shows)
```

### Chart session

One shared client inside the web process. It checks the cookies with TradingView’s logged-in user request before opening sockets. The socket then sends that session’s auth token.

For each symbol the client:

1. Opens a chart session on `prodata` (the endpoint a logged-in session uses).
2. Sets the symbol and timeframe (`1d` or `1W`, the same frames local indicators use).
3. Creates studies in stack order. A study whose source is price uses `open` / `high` / `low` / `close` / `hl2` / `hlc3` / `ohlc4`. A study whose source is another study sends that parent study’s id and plot index in the source input. That second form is the indicator-on-indicator request, and TradingView enforces `TV_INDICATOR_ON_INDICATOR_LIMIT`.
4. Reads the study’s historical periods (plot values aligned to bars) and closes the chart session when the queue needs the slot.

Concurrency is capped by `TV_CHART_CONNECTIONS`, independent of `SCANNER_CONCURRENCY`. A 429, `study_not_auth`, or a session-expired error fails that symbol and is shown on the tab. It does not take down the web process.

Built-in studies are addressed by TradingView’s study name. Community and personal scripts are addressed by pine id (`PUB;…` or the saved-script id returned for this session). The tab lists scripts this session can see; it does not accept an arbitrary id typed in hope.

### Saved stack

Stored as JSON under `data/cache/tv_stacks.json` (same kind of local ledger file as the paper account). One stack:

- `id`, `name`, `timeframe` (`1d` or `1W`)
- `studies[]`: pine or built-in id, version, input values, and a source that is either a price series or `{study_index, plot}` pointing at an earlier study
- `signal`: how a bar becomes BUY or SELL
- `stop`: how that signal gets a protective stop so `TradeSignal` can fill

The child study count (studies after the first whose source is another study) is rejected above `TV_INDICATOR_ON_INDICATOR_LIMIT` before any socket opens.

### Signal rule

Plot values are not orders. A stack cannot paper-trade until it has a rule:

- `cross` — one plot crosses another plot, or crosses a level
- `threshold` — a plot is above or below a level on the latest closed bar, and was not on the previous bar
- `strategy` — the script is a TradingView strategy and the latest closed bar has an entry order in the study report

Stop is one of: a named plot on the protective side of price, a percent of price, or ATR from the bar (local ATR, only for the stop distance). The same `emit` checks used by local indicators apply: stop on the protective side, no stop farther than half the price, PH shorts dropped.

Trailing stacks use the existing indicators-only ratchet (`ratchet_indicator_stop` / `exit_indicator`) when the rule says the position exits on the opposite signal. Fixed-target stacks set `take_profit` once and then follow the normal bar exit.

## 5. Indicators tab

`GET /indicators` renders `web/templates/indicators.html` with `active="indicators"`. Nav link label is **Indicators**, placed after Patterns.

The page:

- Session line: logged in, or “session expired — update TV_SESSIONID”. No cookie values in the HTML or the API response.
- Stack editor: ordered studies, inputs, source dropdown (price series, or a plot of a study above this one).
- Preview: symbol, exchange, timeframe. Shows the last closed bar’s plot values and whether the signal rule would fire. Does not open a paper position.
- Paper controls: market (US/PH), symbol count, extra tickers, stream checkbox. **Start paper** calls the existing paper start API with the stack id.

Authenticated the same way as the other pages (`WEB_UI_USERNAME` / `WEB_UI_PASSWORD`).

API, all behind `require_login`:

| Method | Path | Role |
| --- | --- | --- |
| `GET` | `/api/indicators/session` | Cookie check only. `{ok, plan}` or `{ok: false}`. |
| `GET` | `/api/indicators/catalog` | Built-ins plus scripts this session can open. |
| `GET` | `/api/indicators/stacks` | Saved stacks. |
| `POST` | `/api/indicators/stacks` | Create or replace one stack. Validates the child-study cap. |
| `DELETE` | `/api/indicators/stacks/{id}` | Remove a stack that is not the one a running book is using. |
| `POST` | `/api/indicators/preview` | One symbol, one stack, last plots and the signal bar. |

## 6. Paper trading

`PaperStartRequest` gains `tv_stack_id: str = ""`. When it is set:

- `indicators_only` is forced on for the book, and pattern-only / patternsv2 are forced off. The local `indicators.discover()` list is not loaded.
- The scanner’s detector list is one `TvStackIndicator` (`BaseIndicator`) bound to that stack. `analyze()` reads the study periods for the symbol and returns a `TradeSignal` only on the latest closed bar that the rule fires. `pattern` on the signal is the stack name, so the Paper blotter shows it. `provenance` is `tv_indicator`.
- Open, stop ratchet, and opposite-signal exit use the indicators-only branches already in `PaperAccount`. One position per symbol, notional room, PH long-only, no fill outside the cash session.
- The Paper tab needs no new blotter. Its existing **Indicators only** checkbox is a different mode (local Python detectors). Starting a stack from the Indicators tab is the TradingView mode. A book shows which one it is running in its status line.
- Symbol universe is the same screener slice Paper already uses (`n_symbols` plus extra tickers), capped in flight by `TV_CHART_CONNECTIONS`.

Stream: when the Paper stream checkbox is on, fills and the clock still come from the existing paper-trade stream (local history replay). Study values for each simulated day are the TradingView study period whose bar time matches that day. If TradingView has no period for that bar, the symbol is skipped for the day and the log says so. The stream does not send orders to TradingView.

A running book keeps its stack id. Editing or deleting that stack is refused until the book stops. Session expiry mid-run stops new entries, leaves open positions to their stops, and surfaces the error on the book status line.

## 7. Work order

1. **Settings.** Add the four keys to `config.py` and `.env.example`. Empty cookies disable the client. Limits clamp to at least 1.
2. **Session check.** `TvStudyClient.session()` returns ok or expired. Unit-test the disabled and HTTP-error paths with a fake transport. No live cookie in tests.
3. **Study fetch.** One symbol, a two-study stack (parent on `close`, child source = parent plot). Parse periods into a dataframe keyed by bar time. Cap child studies. Pool size = `TV_CHART_CONNECTIONS`.
4. **Tab.** Nav item, page, stack CRUD, preview. Browser check: login → Indicators → session line, save a stack, preview one symbol.
5. **Paper.** `tv_stack_id` on start. Scanner uses `TvStackIndicator` only. A fake study series drives one BUY and one trailing exit through `PaperAccount` without a network call. A book started this way shows the position on `GET /api/paper/status`.
6. **Live smoke.** With real cookies in `.env` (not committed), preview one liquid US symbol and start a 5-symbol paper book. Confirm the Paper tab lists the fill and that a second symbol does not open a second position on the same name.

## 8. Out of scope

- Replacing or rewriting `indicators/*.py`.
- Live broker orders. `broker/` is still empty; this feature is paper only.
- Storing the TradingView username or password.
- Sharing one session across machines, or running more chart sockets than the plan allows.
- Using a script the account cannot open on the website.
