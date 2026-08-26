# Philippine Stocks — Daytime Trading Plan

**Status:** research plan, not implementation.
**Date:** 2026-08-13
**Goal:** run this bot against PSE-listed names during Philippine daytime hours, so you can sit the session instead of waiting for the US open (~21:30–04:00 PHT).

This is a second **market profile** next to the existing US swing stack. It is not a small `.env` flip. Data, costs, session clock, shorts, currency, and live execution all change.

---

## 1. What “trade during the day” actually means

You are in UTC+8. The current bot is a **US swing** system:

- Universe: TradingView `america` / NASDAQ-NYSE
- Bars: daily / weekly (`1d`, `1W`)
- Scan cadence: once per hour (`SCAN_INTERVAL_SECONDS=3600`)
- Paper account: USD
- Live path: Interactive Brokers (commented out in `core/scanner.py`)

PSE hours sit in the middle of a Philippine workday. US hours sit overnight. That is the real motivation.

There are two different products hiding in the same sentence:

| Mode | What you do 09:30–15:00 PHT | Trade count | Fit with this repo |
|---|---|---|---|
| **A. Daytime swing operator** | Watch daily/weekly setups, click buys/sells at the broker while the cash market is open | A few signals per week, not per hour | High — reuse patterns, Kronos, paper, UI |
| **B. True intraday** | Trade 5m/15m structure through the AM and PM sessions | Many attempts per day | Low — patterns, scan loop, history fetch, and costs are all built for swing |

**Recommendation: ship Mode A first.** Mode B is a different strategy with worse economics on PSE (see §4). Do not convert the swing bot into a scalper as the first PH step.

Mode A still lets you trade *during the day*. The daily bar only prints once, but entries, stops, and size happen while the market is open and you are awake. That is the gap the US book cannot fill.

---

## 2. How the Philippine market works (facts this bot must honor)

### 2.1 Session clock (Asia/Manila, weekdays)

Source: PSE / broker published windows (BDO Securities, 2026 guides). Closed weekends and PH holidays (not US holidays).

| Time (PHT) | Window | Bot must |
|---|---|---|
| 09:00–09:15 | Pre-open | Queue limit orders only. No assumed fills. |
| 09:15–09:30 | Pre-open, no cancel | Do not modify/cancel. |
| 09:30–12:00 | Continuous AM | Scan + alerts + (manual) orders OK. |
| 12:00–13:00 | Recess | **Hard halt.** No new/modify/cancel. Scanner may still poll data; it must not emit “place now.” |
| 13:00–14:45 | Continuous PM | Same as AM. |
| 14:45–14:48 | Pre-close | Orders OK, matching frozen. |
| 14:48–14:50 | Pre-close, no cancel | Do not modify/cancel. |
| 14:50–15:00 | Run-off / trading-at-last | Only at the closing price. |
| 15:00 | Close | Off-hours orders sit until next pre-open. |

US/Eastern session labels in `analysis/chart_renderer.py` are wrong for this market. PH daily bars must use `Asia/Manila`.

### 2.2 Universe and liquidity

~280 listed names. A handful of banks, property, telcos, and conglomerates do almost all the volume (BDO, BPI, MBT, SM, SMPH, JFC, TEL, GLO, AC, ALI, MER, AP, ICT, URC, …). Many names print a few thousand shares a day. This bot’s volume gate and Kronos lookback both assume liquid tape.

**Starting universe: PSEi 30, then expand to the top ~50 by 20-day peso volume.** Drop anything below a hard ADV floor (proposal: ₱5M average daily value; tighten after paper). Do not scan “top 100 by market cap” the way the US path does — PSE market-cap lists are full of sleepy names.

### 2.3 Board lots and ticks (changes 23 Nov 2026)

Until PSETradeX go-live (**target 23 Nov 2026**), size must be a **board lot** from the price table (e.g. ₱5.00–9.99 → lot 100, tick ₱0.01). Odd lots are a separate book. After go-live, lot size becomes **1 share** for all names.

Paper/live sizing must round to the current lot table, then re-check after Nov 23.

Tick size is also price-tiered. Stops/TPs rounded to the tick, not to ¢0.01 USD.

### 2.4 Settlement, cash, same-day round trips

Equities settle **T+2** (SCCP, since Aug 2023). You can buy and sell in the same session if the broker allows it and you have the shares/cash. Unsettled sale proceeds are not always buying power. Paper must not assume US-style instant cash recycle.

### 2.5 Shorts

The current engine emits `SELL` as a short (regime filter: short only below SMA200). On PSE, short selling is **not** a retail default:

- Only **eligible** names (PSEi members + ETFs, plus a short-interest cap of 10%).
- Must go through a trading participant with **securities borrowing (SBL)**.
- Uptick rule. Day orders only. No shorts in pre-open / pre-close.
- Most retail COL / FirstMetroSec / DragonFi accounts are **long-only**.

**PH profile default: long-only.** Disable pattern `SELL`s and `KRONOS_RANK_LONG_ONLY=true`. Revisit shorts only if the live broker account is explicitly SBL-enabled.

### 2.6 Costs (as of Aug 2026)

Typical online broker (COL / FirstMetroSec / DragonFi style):

| Leg | Pieces | Approx. |
|---|---|---|
| Buy | commission 0.25% + 12% VAT on commission + PSE 0.005% + SCCP 0.01% | **~0.295%** |
| Sell | same as buy + stock transaction tax **0.1%** of gross (CMEPA / RR 20-2025, effective 1 Jul 2025; was 0.6%) | **~0.395%** |
| Round trip | | **~0.69%** |

`ENGINE.txn_cost_pct = 0.001` (0.10%) is a US number. Using it on PSE will print fake expectancy.

Worked implication: a 1% swing winner is barely above round-trip. A 0.5% intraday scalp is a loser before slippage. **Mode B needs a much larger edge than Mode A, and Mode A still needs a higher cost model and a higher minimum predicted move than the US book.**

Confirm the live broker’s min commission (some desks floor at ₱20) and current STT on the fee schedule before going live. DragonFi’s older help text still quotes 0.6% STT — treat vendor pages as stale until checked.

### 2.7 Data delay

TradingView’s free PSE feed is typically **15 minutes delayed**. Real-time PSE is a paid TradingView market-data add-on. Yahoo `*.PS` history is also delayed. Mode A on daily bars can live with delay. Mode B cannot — a 15-minute late 5m bar is fiction.

---

## 3. What this repo assumes today (gaps)

```
Today (US swing)                          Needed (PSE daytime)
─────────────────                         ────────────────────
TV_SCREENER=america                       philippines
TV_EXCHANGE=NASDAQ                        PSE
Yahoo chart ticker AAPL                   BDO.PS  (suffix map)
Chart TZ America/New_York                 Asia/Manila
Paper / risk in USD                       PHP
txn_cost_pct = 0.10%                      ~0.69% round trip
SELL = short                              long-only
Scan 24/7 hourly                          session clock + lunch halt
IBKR execution (disabled)                 no IBKR PSE product; PH SEC advisory vs IBKR
EDGAR 8-K blackout (some patterns)        PSE EDGE / no US filings
Kronos trained on liquid global names     unproven on PSE; likely fail-open or off
~280 names, many illiquid                 PSEi / ADV filter
```

Concrete code touchpoints (do not implement until §8 is approved):

| Area | Files | Change |
|---|---|---|
| Market profile | `config.py`, `.env.example` | `MARKET=us\|ph`, PHP capital, session TZ, cost model, long-only flag |
| Screener | `data/tv_client.py` | `set_markets("philippines")`, exchange `PSE`, Yahoo suffix `.PS` on `_fetch_history_chart` |
| Session | new helper, `core/scanner.py` | Manila calendar, recess = no orders |
| Charts | `analysis/chart_renderer.py` | `Asia/Manila` when market=ph |
| Paper | `core/paper_trader.py`, `config.py` | PHP equity, `MAX_DAILY_LOSS_PHP`, lot rounding |
| Engine | `core/engine_defaults.py` | PH `txn_cost_pct` ≈ 0.0035 one-way (or explicit buy/sell legs), long-only |
| Universe | `main.py` fetch path | ADV / PSEi filter, not raw top-100 mcap |
| Execution | `broker/` (missing; IBKR commented out) | **Do not revive IBKR for PSE.** Alerts + manual fill first |
| Earnings | `data/edgar_client.py`, channel patterns | Skip EDGAR on PH symbols |
| Kronos | `core/kronos_gate.py` | Default **off** for PH until a PH walk-forward says otherwise |

IBKR is the wrong live venue here:

- IBKR does not offer PSE cash equities as a normal product.
- PH SEC issued an advisory (Jan 2026) that IBKR is not licensed to solicit / broker in the Philippines.

Live PH fills must go through a **SEC-licensed local trading participant** (COL, FirstMetroSec, DragonFi, BDO Securities, BPI Trade, Unicapital, etc.).

---

## 4. Execution: the hard constraint

No major PH retail broker publishes a customer REST/FIX API for placing orders. COL explicitly has no algo/API. FirstMetroSec PRO has conditional orders (stop/limit) **inside their app**, not for this process. DragonFi / UTrade are the same shape: human UI.

That splits the project into two layers that must stay honest:

```
┌─────────────────────────────────────────────────────────┐
│  THIS BOT  (data → patterns → gates → paper / alerts)   │
│  PHP paper account, Manila clock, PSE universe          │
└──────────────────────────┬──────────────────────────────┘
                           │ Telegram / web dashboard
                           ▼
┌─────────────────────────────────────────────────────────┐
│  YOU + LOCAL BROKER UI  (COL / FMS / DragonFi / …)      │
│  Type the order. Lot-round. Respect recess / TAL.       │
└─────────────────────────────────────────────────────────┘
```

**Phase 1 live path = semi-auto.** Bot decides. Human clicks. Same as today’s US scanner logging “would trade,” except the operator is awake and the cash market is open.

Do **not** plan browser automation of the broker as the architecture. It fights ToS, 2FA, and PSETradeX UI churn, and it is a compliance mess.

If a licensed broker later offers FIX/OMS for this account, add `broker/ph_client.py` behind the same `OrderManager` interface the README already sketched for IBKR. Until then, live automation is out of scope.

FirstMetroSec conditional orders are a useful **manual** complement: once the bot alerts a BUY, you can park a stop-limit in PRO/GO and walk away. The bot does not own that order.

---

## 5. Recommended architecture: a market profile, not a fork

Keep one codebase. Select the market at process start.

```
.env.ph                          .env.us  (today)
────────                         ────────
MARKET=ph                        MARKET=us
TV_SCREENER=philippines          TV_SCREENER=america
TV_EXCHANGE=PSE                  TV_EXCHANGE=NASDAQ
WATCHLIST=BDO,BPI,SM,SMPH,...    WATCHLIST=AAPL,MSFT,...
CURRENCY=PHP                     CURRENCY=USD
PAPER_INITIAL_CAPITAL=1000000    PAPER_INITIAL_CAPITAL=100000
LONG_ONLY=true                   LONG_ONLY=false
TXN_COST_PCT=0.0035              TXN_COST_PCT=0.001
SESSION_TZ=Asia/Manila           SESSION_TZ=America/New_York
SCAN_INTERVAL_SECONDS=300        SCAN_INTERVAL_SECONDS=3600
KRONOS_GATE_ENABLED=false        KRONOS_GATE_ENABLED=true
```

Two processes can run on one machine without mixing books:

- Day: `python main.py --paper --env .env.ph` (or `MARKET=ph`)
- Night: existing US paper/live

Separate paper JSON files (`paper_account_ph.json` vs current default). Mixing PHP prices into the USD ledger would corrupt both.

Symbol identity:

| Layer | Example |
|---|---|
| Bot / broker / PSE | `BDO` |
| TradingView | `PSE:BDO` (screener `name=BDO`, `exchange=PSE`) |
| Yahoo chart history | `BDO.PS` |

`TVClient._fetch_history_chart` currently hits `https://query1.finance.yahoo.com/v8/finance/chart/{symbol}` with a bare ticker. For PH it must append `.PS` (and never send `PSE:BDO` to Yahoo).

Proof before any feature work:

```bash
python - <<'PY'
from tradingview_screener import Query, col
n, df = (Query()
    .select("name", "exchange", "close", "volume", "currency")
    .set_markets("philippines")
    .order_by("volume", ascending=False)
    .limit(10)
    .get_scanner_data())
print(n)
print(df)
PY
```

If that returns `PSE` rows with PHP closes, the screener path is viable. Then fetch `BDO.PS` daily history through the existing chart helper and confirm ≥400 bars for any Kronos experiment.

---

## 6. Mode A — daytime swing (build this)

Reuse Toby patterns on `1d` (and `1W` where a pattern already supports it). Operator sits 09:20–15:05 PHT.

### 6.1 Scan cadence

Hourly is fine for *new daily bars*, but the in-progress daily candle updates all morning. For a human who wants to act at the open / into the close, poll every **5 minutes during continuous trading**, sleep through recess, stop after 15:00. That is still Mode A: patterns stay on `1d`; you just see the forming bar sooner.

Do not enable 1m/5m pattern modules here.

### 6.2 Gates (PH-specific defaults)

| Gate | US default | PH Mode A default | Why |
|---|---|---|---|
| Patterns | enabled subset | same enabled subset, **longs only** | SELL=short is mostly unusable |
| SMA200 regime | on | on | still valid as trend filter |
| Kronos 3d | on | **off** until PH backtest | model + 3% min-move unproven on PSE; fail-open would silently skip the filter |
| Volume RVOL+OBV | off | keep off until PH A/B | US A/B already killed trade count; PSE volume is lumpier |
| Vision | off/optional | optional | same cost |
| Cost / R:R | 0.10% / 1.5 R | bake **0.69% round trip** into R:R | otherwise winners are costs |
| Hard stop | 6% | keep 6% or ATR floor, tick-rounded | gaps exist on PSE too |
| Earnings | EDGAR 8-K | skip or PSE EDGE later | EDGAR is US-only |

### 6.3 Daytime runbook (operator)

```
09:00  Bot already running. Pre-open: alerts are “queue,” not “filled.”
09:25  Glance dashboard: watchlist, open PHP positions, overnight gaps.
09:30  Open. If a daily setup is live, place the lot-rounded limit/market in the broker.
10:30  First scan digest. Ignore illiquid spikes.
12:00  Recess. Do not send orders. Lunch.
13:00  Resume. Check stops — some brokers do not hold stops through recess the way TWS does.
14:30  Last chance for same-day entries. Prefer not to chase into pre-close.
14:50  TAL: only if you explicitly want the close.
15:05  EOD paper mark, Telegram summary: signals, fills you confirmed, open risk.
```

Telegram payload for each alert (minimum):

- symbol, side (BUY / CLOSE only), pattern, confidence
- last, stop, target, proposed shares (already lot-rounded), notional PHP
- “session: AM | recess | PM | closed”
- “paper filled? yes/no” vs “awaiting your broker click”

The dashboard (`main.py --ui` / `--web`) already has Paper Trading. Point it at the PH account file and PHP labels. No new UI product required for Mode A.

### 6.4 Backtest before any peso is at risk

1. Spike data (§5 proof).
2. Cache daily OHLCV for PSEi 30 via Yahoo `*.PS`.
3. `python main.py --backtest` with `MARKET=ph`, long-only, PH costs.
4. `scripts/compare_patterns.py` on that universe — **do not trust US win rates**. Flag/pennant/channel stats in this repo are NASDAQ/NYSE.
5. Paper 20+ PH sessions with you clicking (or paper-only, no broker).
6. Only then size a tiny live sleeve.

Expect fewer trades than US. That is OK. The point is a daytime book, not matching US trade count.

---

## 7. Mode B — true intraday (later, maybe never)

Only after Mode A paper is honest.

What would have to change:

- Pattern timeframes: `15m` (MCP already maps `1m`/`5m` → `15m` in `TVClient.MCP_TIMEFRAME_MAP` — the stack is not a 1-minute bot).
- History: screener `SCREENER_FIELDS` and Yahoo `_CHART_SPECS` only know `1d` / `1W` today. Need a 15m history source (TradingView, or paid PSE).
- Scan: 30–60s during continuous trading, **zero order intent** in recess.
- Real-time data: paid TV PSE feed or broker quotes. Delayed 15m data is useless.
- Costs: 0.69% round trip + worse slippage on thin names. Need targets ≫ 1% or the sleeve dies.
- Lunch: flatten or carry? Carry is simpler; flatten needs two extra legs of cost.
- TAL / auction: do not treat 14:50–15:00 as continuous.

This is a new strategy sleeve (`patterns/` + scan policy), not a config flag. Budget it as its own project.

---

## 8. Implementation phases

Do not start Phase 2 until Phase 0 is green.

### Phase 0 — Data spike (half day)

- Confirm `set_markets("philippines")` + exchange `PSE`.
- Confirm Yahoo `BDO.PS` (and 10 other PSEi names) return 400+ daily bars.
- List ticker mismatches (SM vs SM.PS, PSE vs PSE.PS, preferred shares, warrants).
- Measure delay vs a live COL/FMS quote on one name.
- **Exit:** a 20-row CSV of (bot_symbol, tv_exchange, yahoo_symbol, last, volume, bars).

### Phase 1 — Market profile + PHP paper (2–4 days)

- `MARKET=ph` settings: screener, exchange, TZ, currency, costs, long-only, lot table.
- Yahoo `.PS` map in `TVClient._fetch_history_chart`.
- Separate paper ledger.
- Session helper: is_open / is_recess / is_preopen / is_tal / is_holiday.
- Scanner: still runs, but `PaperAccount` / any future order path refuses fills in recess and closed.
- Chart TZ `Asia/Manila`.
- Disable EDGAR on PH symbols.
- Kronos off.
- **Exit:** `python main.py --paper` on PSEi 30 through one full PH session without mixing USD.

### Phase 2 — Daytime operator loop (1–2 days)

- Scan every 5 minutes while continuous, pause order-intent in recess.
- Telegram (or web) alert schema in §6.3.
- Manual fill checkbox: operator marks “clicked at broker @ price” so paper tracks reality, or keep paper fully simulated and treat broker as a side account. Pick one and do not mix.
- EOD summary.
- **Exit:** you can sit 09:30–15:00, get alerts, place COL/FMS orders, and reconcile.

### Phase 3 — PH backtest + pattern cull (2–3 days)

- Cached `*.PS` history, PH costs, long-only, lot rounding.
- Per-pattern table on PSEi 30 / top-50 ADV.
- Drop patterns that were US-only (or keep them disabled as today).
- Optional: fine-tune / score Kronos on PH liquid names; only then consider the gate.
- **Exit:** a written “enabled PH patterns” list with sample size, not a vibe.

### Phase 4 — Tiny live sleeve (only after Phase 3)

- One licensed local broker account, PHP, long-only.
- Max 1–2 names, hard daily loss in PHP, lot-rounded.
- Human still clicks. Bot never sends the order.
- After 20 live fills, compare paper vs broker (slippage, lots, fees).

### Phase 5 — Optional later

- Mode B 15m sleeve.
- PSE EDGE disclosure calendar (replaces EDGAR).
- Broker API/FIX **if** a licensed TP offers it to this account.
- Board-lot table → 1-share after 23 Nov 2026.
- Dual-process supervisor: PH by day, US by night, one host.

---

## 9. Dual book: US night + PH day

Possible on one VPS:

| Clock (PHT) | Process |
|---|---|
| 09:00–15:30 | PH paper/alerts |
| 15:30–21:00 | idle / EOD jobs |
| 21:00–04:30 | existing US scan |

Do not share: paper JSON, daily loss counters, `max_open_positions`, or watchlists. Capital is also not shared unless you explicitly rebalance PHP↔USD outside the bot.

IBKR (if you still use it for US) stays US-only. PH stays local broker.

---

## 10. Risks and non-goals

**In scope for this plan**

- PSE data + PHP paper + daytime alerts + manual live.
- Long-only liquid names.
- Honest PH costs and session clock.

**Not in scope**

- Automated live orders through COL/FMS/DragonFi.
- IBKR for PSE.
- Short selling.
- 1-minute scalping.
- Mixing PHP and USD in one account.
- Trusting US pattern stats on PSE.
- Browser-driving the broker.

**Known failure modes**

| Failure | What you see | Mitigation |
|---|---|---|
| Illiquid fill | Alert at last, broker shows 0 size / 5% away | ADV floor, limit orders, cap % of 20d volume |
| Delayed TV/Yahoo | You click a stale breakout | Mode A only until paid real-time; compare to broker quote before send |
| Recess order | Broker reject / unexpected queue | Session helper, no-order window 12:00–13:00 |
| Lot rounding | Size 337 shares → 300, risk ≠ 2% | Round then recompute risk; skip if rounded risk > cap |
| Cost model too low | Paper rich, live poor | 0.69% round trip + slippage pad |
| Kronos on PSE | Gate fail-open = no gate | Keep off until PH score exists |
| Holiday | Bot trades a closed day | PH holiday calendar, not NYSE |
| PSETradeX Nov 2026 | Lots/ticks/FIX change under us | Revisit lot table on go-live |

---

## 11. Open decisions (need your call before coding)

1. **Mode A vs Mode B as the first build.** Recommendation: A.
2. **Broker for live clicks.** COL / FirstMetroSec / DragonFi / other — the bot does not care, but lot/fee/min-commission must match that desk.
3. **Paper vs hybrid fills.** Fully simulated PHP paper, or paper that waits for you to type the actual fill price.
4. **Starting capital (PHP)** and max daily loss in PHP.
5. **Universe:** PSEi 30 only, or top-N by peso volume.
6. **Keep US bot running at night** in parallel, or pause US while PH is being proven.
7. **Kronos:** leave off, or spend a GPU pass to score it on `*.PS` before any PH live.

---

## 12. Bottom line

You can trade Philippine stocks during the day with this codebase if we treat PSE as a **second market profile**: Manila clock, PHP, long-only, expensive round-trip, liquid names only, Yahoo `.PS` + TradingView `philippines`/`PSE`, and **you** as the order router at a licensed local broker.

That gets you a daytime swing book. It does not get you a US-style API scalper. The exchange is open 9:30–12:00 and 13:00–15:00. The missing piece is not another pattern file — it is market plumbing and an honest execution story.

Next step after you pick the open decisions: Phase 0 data spike, then Phase 1 PHP paper.
