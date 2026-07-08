# Pennant Continuation Pattern — LOCKED CONDITIONS (2026-07-05)

## Gate — Flagpole (the impulse move)

| # | Condition | Detail |
|---|-----------|--------|
| G1 | Sharp move **≥10%** | Over ≤10 trading days, either direction (bullish or bearish pennant) |
| G2 | Volume ≥1.3x prior avg | Flagpole avg volume ≥1.3x the 20-day average preceding it |

## Consolidation (the pennant itself) — C1-C6

| # | Condition | Detail |
|---|-----------|--------|
| C1 | Starts within 1-2 bars of flagpole extreme | Peak (bullish) or trough (bearish) |
| C2 | Duration 5-**10** trading days | Short-and-sharp is the real "tell" — losers dragged on ~1.3 bars longer on average |
| C3 | Converging trendlines | Upper (swing highs) and lower (swing lows) slope toward each other — true convergence, not parallel (parallel = flag, not pennant) |
| C4 | No close outside trendlines | Until the breakout bar |
| C5 | Retrace **≤30%** of flagpole range | Shallower pullback = stronger continuation thesis — losers retraced ~27% on average vs ~24% for winners |
| C6 | Volume contraction ≤70% of flagpole avg | The "coiling" tell |

## Breakout / Entry — C7-C8 (C9 dropped)

| # | Condition | Detail |
|---|-----------|--------|
| C7 | Close beyond trendline | Same direction as flagpole |
| C8 | Breakout volume ≥1.5x consolidation avg | Rules out low-conviction fakeouts |
| ~~C9~~ | ~~RSI confirms direction~~ | **REJECTED** — RSI at breakout showed no separation between winners (72.9 direction-adjusted) and losers (71.8). Not enforced. |

## Exit Rule — 5% close-based trailing stop

Entry: breakout-bar close (C7-C8 confirmed).
Exit: trailing stop 5% from the extreme close since entry (highest close for bullish/long, lowest close for bearish/short); exit on close breaching the stop. No fixed target, no time stop.

Chosen over 3%/7% trailing, measured-move (20d/40d cap), and hybrid fixed→trail variants — 3% churns out on normal post-breakout noise; measured-move and hybrid variants either didn't generalize past a small sample or were outlier-driven.

## Backtest Results (253-ticker verified NASDAQ+NYSE universe, $10,000/trade)

| Metric | Value |
|--------|-------|
| Trades | 41 |
| Wins | 25 (**61.0%** win rate) |
| Losses | 16 |
| Capital deployed | $410,000 ($10k × 41) |
| **Net P&L** | **+$23,493.68** |
| ROI on deployed | **5.73%** |
| Avg win | +$1,179.04 |
| Avg loss | -$373.90 |
| Payoff ratio | ~3.2 : 1 |

## Universe (253 verified tickers)

**~182 NASDAQ**: AAL, AAPL, ABNB, ACAD, ACMR, ADBE, ADI, ADSK, AFRM, AKAM, ALGM, ALGN, ALNY, AMAT, AMBA, AMD, AMGN, AMZN, APP, APPN, ARGX, ARM, ARWR, ASML, AVGO, AXON, BEAM, BIIB, BKNG, BMRN, CAKE, CASY, CBSH, CDNS, CDW, CELH, CHKP, CHRW, CHTR, COIN, COST, CPRT, CRSP, CRUS, CRWD, CSCO, CSGP, CTAS, CTSH, DASH, DDOG, DIOD, DKNG, DLTR, DXCM, EA, EBAY, EDIT, ENPH, ENTG, EXEL, FAST, FFIV, FIVE, FLEX, FOXA, FSLR, FTNT, GEN, GILD, GOOGL, GTLB, HALO, HBAN, HOOD, ICLR, IDXX, ILMN, INCY, INSM, INTC, INTU, IONS, ISRG, JAZZ, JBHT, JBLU, KDP, KHC, KLAC, KRYS, LCID, LKQ, LRCX, LSCC, LULU, MANH, MAR, MCHP, MDB, MDLZ, MEDP, MELI, META, MNST, MPWR, MRNA, MRVL, MSFT, MSTR, MTSI, MU, NBIX, NET, NFLX, NICE, NTAP, NTLA, NVDA, NWSA, NXPI, ODFL, OKTA, OLLI, ON, OTEX, PANW, PAYX, PCAR, PCTY, PCVX, PEP, POWI, PTC, PTCT, PYPL, QCOM, QRVO, REGN, RGNX, RIVN, RMBS, ROKU, ROST, SBUX, SFM, SIRI, SITM, SLAB, SMCI, SNPS, SOFI, SPSC, SRPT, SSNC, STX, SUPN, SWKS, TER, TMUS, TRMB, TSLA, TTD, TTWO, TXN, TXRH, ULCC, ULTA, UPST, UTHR, VRSK, VRSN, VRTX, VTRS, WBD, WDAY, WDC, WING, WTFC, ZION, ZM, ZS

**~71 NYSE**: ABT, ACN, AMT, ANET, APD, AXP, BA, BAC, BLK, BMY, BRK.B, BSX, C, CAT, CCI, CI, COP, CRM, CVS, CVX, DE, DELL, DUK, ECL, EOG, EW, FCX, FDX, GD, GE, GS, HD, HPE, HUM, IBM, ICE, JNJ, JPM, KO, LLY, LMT, LOW, MA, MCD, MCK, MCO, MDT, MMM, MRK, MS, NEE, NEM, NKE, NOC, NOW, ORCL, PFE, PG, PINS, PLD, PSA, PSX, RTX, SAP, SIRI, SLB, SNAP, SO, SPGI, SPOT, SQ, SYK, T, TGT, UBER, UNH, UPS, VLO, VZ, WFC, XOM, ZBH

## Scripts (`C:\Users\dell\tradingview-mcp\`)

- `pennant_scan.cjs` — live screener, `DATA_DIR` = `pennant_data_live`
- `pennant_find_historical.cjs` — historical occurrence finder for backtesting, `DATA_DIR` = `pennant_data_253`
- `backtest_pennant_200.cjs` — $10,000/trade backtest with the locked 5% trailing-stop exit