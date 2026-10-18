"""
scripts/kronos_forecast_plot.py — walk-forward Kronos forecast plot for one symbol.

Loads daily bars from the local stocks_history Postgres, runs the fine-tuned
Kronos predictor at each trading-day close from --start onward (400-bar
lookback context, +3 trading-day forecast), and plots actual close vs the
predicted +1d / +3d paths.

    .venv-kronos/bin/python scripts/kronos_forecast_plot.py --symbol AAPL --start 2026-10-01
    .venv-kronos/bin/python scripts/kronos_forecast_plot.py --symbol NVDA --start 2026-09-01 --use-base
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import matplotlib
import pandas as pd

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

from core.kronos_eval import (  # noqa: E402
    GATE_HORIZON_BARS,
    LOOKBACK,
    with_amount,
)
from utils.logger import log  # noqa: E402


def load_symbol_df(symbol: str) -> pd.DataFrame:
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
        raise SystemExit(f"no bars for {symbol} in stocks_history")
    df = pd.DataFrame(rows, columns=["bar_date", "open", "high", "low", "close", "volume"])
    df.index = pd.to_datetime(df["bar_date"])
    df = df.sort_index()
    return df[["open", "high", "low", "close", "volume"]][~df.index.duplicated(keep="last")]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbol", default="AAPL")
    ap.add_argument("--start", default="2026-10-01")
    ap.add_argument("--lookback", type=int, default=LOOKBACK)
    ap.add_argument("--horizon", type=int, default=GATE_HORIZON_BARS)
    ap.add_argument("--sample-count", type=int, default=1)
    ap.add_argument("--use-base", action="store_true",
                    help="use base weights instead of the fine-tune")
    ap.add_argument("--compare", action="store_true",
                    help="run base AND fine-tuned, plot actual + both +3d paths")
    ap.add_argument("--plot-horizon", type=int, default=3,
                    help="which forecast horizon to plot in --compare mode (1 or 3)")
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)

    symbol = args.symbol.upper()
    df = load_symbol_df(symbol)
    start = pd.Timestamp(args.start)
    # Asofs with a full horizon ahead of them (counted, not positional — safe
    # for thin/delisted tapes with gaps or duplicate sessions).
    asofs = [d for d in df.index[df.index >= start]
             if int((df.index > d).sum()) >= args.horizon]
    if not asofs:
        raise SystemExit(f"no asof dates >= {args.start} with full horizons")

    sys.path.insert(0, str(Path.home() / "Kronos"))
    from core.kronos_eval import _load_predictor

    models = ["base", "finetuned"] if args.compare else (
        ["base"] if args.use_base else ["finetuned"])
    results: dict[str, dict[str, dict]] = {}
    for tag in models:
        predictor = _load_predictor(device="cuda", use_finetuned=(tag == "finetuned"))
        pred_1d: dict[pd.Timestamp, float] = {}
        pred_hd: dict[pd.Timestamp, float] = {}
        for asof in asofs:
            # Walk-forward contract: context is the `lookback` bars ending ON
            # asof (asof + 399 before it); every predicted bar is strictly
            # after asof. The assertion below enforces it per window.
            ctx = df.loc[:asof].tail(args.lookback)
            assert len(ctx) == args.lookback, f"{symbol} {asof.date()}: only {len(ctx)} context bars"
            future_idx = df.index[df.index > asof][: args.horizon]
            assert len(future_idx) == args.horizon, f"{symbol} {asof.date()}: incomplete horizon"
            assert ctx.index[-1] <= asof < future_idx[0], (
                f"lookahead! ctx ends {ctx.index[-1].date()}, asof {asof.date()}, "
                f"first target {future_idx[0].date()}")
            print(f"  [{tag}] ctx {ctx.index[0].date()}..{ctx.index[-1].date()} "
                  f"({len(ctx)} bars ending {asof.date()}) -> "
                  f"pred {[d.date().isoformat() for d in future_idx]}", flush=True)
            x_df = with_amount(ctx)
            last_close = float(x_df["close"].iloc[-1])
            x_ts = pd.Series(pd.DatetimeIndex(x_df.index))
            y_ts = pd.Series(pd.DatetimeIndex(future_idx))
            try:
                out = predictor.predict(
                    df=x_df.reset_index(drop=True), x_timestamp=x_ts,
                    y_timestamp=y_ts, pred_len=args.horizon,
                    T=1.0, top_p=0.9, sample_count=args.sample_count, verbose=False,
                )
            except Exception as exc:  # noqa: BLE001 - one bad window must not kill the plot
                log.warning(f"plot | {symbol} {tag} asof {asof.date()}: predict failed ({exc})")
                continue
            pred_1d[pd.Timestamp(future_idx[0])] = float(out["close"].iloc[0])
            pred_hd[pd.Timestamp(future_idx[-1])] = float(out["close"].iloc[-1])
            print(f"  [{tag}] asof {asof.date()}: pred+1d={out['close'].iloc[0]:.2f} "
                  f"pred+{args.horizon}d={out['close'].iloc[-1]:.2f} "
                  f"(actual close {last_close:.2f})", flush=True)
        del predictor
        import torch as _torch
        _torch.cuda.empty_cache()
        results[tag] = {"pred_1d": pred_1d, "pred_hd": pred_hd}

    def _hits(pred: dict[pd.Timestamp, float], horizon: int) -> tuple[int, int]:
        hits, n = 0, 0
        for asof in asofs:
            fut = df.loc[asof:].iloc[1: args.horizon + 1]["close"]
            if len(fut) < args.horizon:
                continue
            t = fut.index[horizon - 1]
            if t in pred:
                base = float(df.loc[asof, "close"])
                if np.sign(pred[t] - base) == np.sign(float(fut.iloc[horizon - 1]) - base):
                    hits += 1
                n += 1
        return hits, n

    stats = {tag: {"h1": _hits(results[tag]["pred_1d"], 1),
                   "hh": _hits(results[tag]["pred_hd"], args.horizon)}
             for tag in models}
    for tag in models:
        (h1, n1), (hh, nh) = stats[tag]["h1"], stats[tag]["hh"]
        print(f"[{tag}] direction hit: +1d {h1}/{n1}  +{args.horizon}d {hh}/{nh}", flush=True)
    n = stats[models[0]]["hh"][1]

    # ── plot ─────────────────────────────────────────────────────────────────
    plot_df = df.loc[df.index >= (asofs[0] - pd.Timedelta(days=45))]
    fig, ax = plt.subplots(figsize=(13, 6))
    ax.plot(plot_df.index, plot_df["close"], color="black", lw=1.8, label="actual close")
    ax.axvline(asofs[0], color="gray", ls="--", lw=1, label="forecast start")
    colors = {"base": "tab:blue", "finetuned": "tab:red"}
    title_bits = []
    for tag in models:
        s1 = pd.Series(results[tag]["pred_1d"]).sort_index()
        sh = pd.Series(results[tag]["pred_hd"]).sort_index()
        (h1, n1), (hh, nh) = stats[tag]["h1"], stats[tag]["hh"]
        if args.compare:
            ph = args.plot_horizon
            series = s1 if ph == 1 else sh
            ax.plot(series.index, series.values, "s-", color=colors[tag], ms=4,
                    label=f"{tag} pred +{ph}d")
        else:
            ax.plot(s1.index, s1.values, "o-", color="tab:blue", ms=4, label="Kronos pred +1d")
            ax.plot(sh.index, sh.values, "s-", color="tab:red", ms=4,
                    label=f"Kronos pred +{args.horizon}d")
        title_bits.append(f"{tag} [+1d {h1}/{n1}, +{args.horizon}d {hh}/{nh}]")
    ax.set_title(f"{symbol} — Kronos walk-forward from {asofs[0].date()}  " + "  ".join(title_bits))
    ax.set_xlabel("date")
    ax.set_ylabel("close (USD)")
    ax.legend()
    fig.autofmt_xdate()
    fig.tight_layout()
    default_tag = "compare" if args.compare else models[0]
    out = Path(args.out) if args.out else Path(f"/tmp/kronos_{symbol.lower()}_{default_tag}.png")
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=120)
    print(f"wrote {out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
