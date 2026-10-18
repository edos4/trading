"""
scripts/kronos_universe_plot.py — aggregate walk-forward plot over many symbols.

For each symbol: walk-forward Kronos forecasts (base + fine-tuned) from
--start, rebase actual to 100 at the first asof, average rebased actuals and
predictions per target date across the universe. One graph, statistical view.

    .venv-kronos/bin/python scripts/kronos_universe_plot.py --start 2026-10-01 --top-n 30
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from core.kronos_eval import GATE_HORIZON_BARS, LOOKBACK, with_amount  # noqa: E402
from utils.logger import log  # noqa: E402

NASDAQ_CANDIDATES = (
    "NVDA MSFT AAPL AMZN AVGO META GOOGL GOOG TSLA COST NFLX PLTR AMD LIN "
    "AMAT MU ADOBE AMGN INTU ISRG BKNG CSCO CMCSA TXN HON QCOM ADP GILD MELI "
    "KLAC LRCX PANW APP ARM AZN CCEP"
).split()


def load_symbol_df(symbol: str) -> pd.DataFrame | None:
    from data.db import get_conn

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT bar_date, open, high, low, close, volume "
                "FROM daily_bars WHERE symbol = %s ORDER BY ts",
                (symbol.upper(),),
            )
            rows = cur.fetchall()
    finally:
        conn.close()
    if not rows:
        return None
    df = pd.DataFrame(rows, columns=["bar_date", "open", "high", "low", "close", "volume"])
    df.index = pd.to_datetime(df["bar_date"])
    df = df.sort_index()
    return df[["open", "high", "low", "close", "volume"]][~df.index.duplicated(keep="last")]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--start", default="2026-10-01")
    ap.add_argument("--top-n", type=int, default=30)
    ap.add_argument("--lookback", type=int, default=LOOKBACK)
    ap.add_argument("--horizon", type=int, default=GATE_HORIZON_BARS)
    ap.add_argument("--sample-count", type=int, default=1)
    ap.add_argument("--out", default="/tmp/kronos_nasdaq30.png")
    args = ap.parse_args(argv)
    start = pd.Timestamp(args.start)

    # Universe: candidates with a full lookback + full horizon span.
    universe: list[tuple[str, pd.DataFrame]] = []
    for sym in NASDAQ_CANDIDATES:
        df = load_symbol_df(sym)
        if df is None or len(df) < args.lookback + 30:
            continue
        asofs = [d for d in df.index[df.index >= start]
                 if int((df.index > d).sum()) >= args.horizon]
        if len(asofs) < 5 or df.index[-1] < start + pd.Timedelta(days=7):
            continue
        universe.append((sym, df))
        if len(universe) >= args.top_n:
            break
    print(f"universe: {len(universe)} symbols", flush=True)

    sys.path.insert(0, str(Path.home() / "Kronos"))
    from core.kronos_eval import _load_predictor

    models = {"base": _load_predictor(device="cuda", use_finetuned=False),
              "finetuned": _load_predictor(device="cuda", use_finetuned=True)}

    act_series, pred_series = {}, {}
    hits = {m: [0, 0] for m in models}
    for sym, df in universe:
        asofs = [d for d in df.index[df.index >= start]
                 if int((df.index > d).sum()) >= args.horizon]
        base_close = float(df.loc[asofs[0], "close"])
        for d in [asofs[0]] + [f for a in asofs for f in df.index[df.index > a][: args.horizon]]:
            if d not in act_series:
                act_series[d] = []
            if d >= asofs[0]:
                act_series[d].append(float(df.loc[d, "close"]) / base_close * 100)
        for tag, predictor in models.items():
            for asof in asofs:
                ctx = df.loc[:asof].tail(args.lookback)
                if len(ctx) < args.lookback:
                    continue
                assert ctx.index[-1] <= asof
                x_df = with_amount(ctx)
                x_ts = pd.Series(pd.DatetimeIndex(x_df.index))
                fut = df.index[df.index > asof][: args.horizon]
                y_ts = pd.Series(pd.DatetimeIndex(fut))
                try:
                    out = predictor.predict(
                        df=x_df.reset_index(drop=True), x_timestamp=x_ts,
                        y_timestamp=y_ts, pred_len=args.horizon,
                        T=1.0, top_p=0.9, sample_count=args.sample_count, verbose=False)
                except Exception as exc:  # noqa: BLE001
                    log.warning(f"plot | {sym} {tag}: {exc}")
                    continue
                tgt = pd.Timestamp(fut[-1])
                pred_series.setdefault((tag, tgt), []).append(
                    float(out["close"].iloc[-1]) / base_close * 100)
                if tag == "finetuned" or True:
                    a = float(df.loc[tgt, "close"])
                    b = float(df.loc[asof, "close"])
                    if np.sign(float(out["close"].iloc[-1]) - b) == np.sign(a - b):
                        hits[tag][0] += 1
                    hits[tag][1] += 1
        print(f"  {sym}: {len(asofs)} windows", flush=True)

    fig, ax = plt.subplots(figsize=(13, 6))
    ad = pd.Series({d: float(np.mean(v)) for d, v in sorted(act_series.items())})
    ax.plot(ad.index, ad.values, color="black", lw=1.8, label="actual (mean rebased)")
    for tag, color in (("base", "tab:blue"), ("finetuned", "tab:red")):
        s = pd.Series({d: float(np.mean(v))
                       for (t, d), v in sorted(pred_series.items()) if t == tag})
        h, n = hits[tag]
        ax.plot(s.index, s.values, "s-", color=color, ms=4,
                label=f"{tag} +{args.horizon}d (dir {h}/{n})")
    ax.set_title(f"Top-{len(universe)} Nasdaq walk-forward from {start.date()} "
                 f"(rebased mean, sample_count={args.sample_count})")
    ax.set_xlabel("date")
    ax.set_ylabel("rebased close (100 at start)")
    ax.legend()
    fig.autofmt_xdate()
    fig.tight_layout()
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=120)
    for tag in models:
        h, n = hits[tag]
        print(f"[{tag}] direction +{args.horizon}d: {h}/{n} ({h / max(1, n) * 100:.1f}%)", flush=True)
    print(f"wrote {out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
