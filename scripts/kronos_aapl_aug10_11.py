#!/usr/bin/env python3
"""
scripts/kronos_aapl_aug10_11.py — Kronos forecast AAPL closes for 2026-08-10,
2026-08-11, and 2026-08-12.

Refreshes daily OHLCV from the chart API, uses lookback ending the last bar
before Aug 10, predicts those three trading days, writes
kronos_aapl_aug10_11.png (blue=recent actual, red=forecast).

Usage:
    .venv/bin/python scripts/kronos_aapl_aug10_11.py
"""

from __future__ import annotations

import sys
from datetime import timezone
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config import settings  # noqa: E402
from core.kronos_eval import LOOKBACK, MAX_CONTEXT, _load_predictor, with_amount  # noqa: E402
from data.history import load_daily_ohlcv_df  # noqa: E402
from data.tv_client import TVClient  # noqa: E402
from utils.logger import log  # noqa: E402

OUT = ROOT / "kronos_aapl_aug10_11.png"
CUTOFF = pd.Timestamp("2026-08-10")
TARGET_DATES = [
    pd.Timestamp("2026-08-10"),
    pd.Timestamp("2026-08-11"),
    pd.Timestamp("2026-08-12"),
]
PLOT_ACTUAL_BARS = 40


def refresh_aapl() -> pd.DataFrame:
    df = load_daily_ohlcv_df("AAPL")
    if df is not None and not df.empty:
        log.info(f"kronos-aug | AAPL stocks_history {len(df)} bars, last={df.index.max()}")
        return df

    tv = TVClient(settings.tv_screener, "NASDAQ")
    candles = tv._fetch_history_chart("AAPL", "1d")
    if not candles:
        raise SystemExit("Failed to fetch AAPL 1d history")

    rows = []
    for c in candles:
        ts = c.timestamp
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        rows.append({
            "open": float(c.open),
            "high": float(c.high),
            "low": float(c.low),
            "close": float(c.close),
            "volume": float(c.volume),
            "timestamp": ts,
        })
    out = pd.DataFrame(rows).set_index("timestamp")[["open", "high", "low", "close", "volume"]]
    log.info(f"kronos-aug | AAPL chart API {len(out)} bars, last={out.index.max()}")
    return out


def main() -> None:
    df = refresh_aapl()
    hist = df[df.index.normalize() < CUTOFF]
    if len(hist) < LOOKBACK:
        raise SystemExit(
            f"Need ≥{LOOKBACK} bars before {CUTOFF.date()}, have {len(hist)}"
        )

    use = min(LOOKBACK, len(hist), MAX_CONTEXT)
    x_df = with_amount(hist.iloc[-use:])
    tod = hist.index[-1].time()
    y_times = pd.DatetimeIndex(
        [pd.Timestamp.combine(d.date(), tod) for d in TARGET_DATES]
    )

    log.info(
        f"kronos-aug | context {x_df.index[0].date()}→{x_df.index[-1].date()} "
        f"lookback={use} | predict {[t.date() for t in y_times]}"
    )
    log.info(
        f"kronos-aug | last close {float(x_df['close'].iloc[-1]):.2f} @ {x_df.index[-1].date()}"
    )

    predictor = _load_predictor(device="cuda")
    pred = predictor.predict(
        df=x_df.reset_index(drop=True),
        x_timestamp=pd.Series(x_df.index),
        y_timestamp=pd.Series(y_times),
        pred_len=len(y_times),
        T=1.0,
        top_p=0.9,
        sample_count=5,
        verbose=True,
    )
    pred = pred.copy()
    pred.index = y_times

    last = float(x_df["close"].iloc[-1])
    for ts, row in pred.iterrows():
        log.info(
            f"kronos-aug | {ts.date()} pred close=${row['close']:.2f} "
            f"({row['close'] / last - 1:+.2%}) "
            f"OHLC=({row['open']:.2f}/{row['high']:.2f}/{row['low']:.2f}/{row['close']:.2f})"
        )

    actual_tail = hist.iloc[-PLOT_ACTUAL_BARS:]
    fig, ax = plt.subplots(figsize=(12, 5), dpi=120)
    ax.plot(
        actual_tail.index,
        actual_tail["close"],
        color="blue",
        linewidth=1.8,
        label="Actual",
    )
    bridge_x = [actual_tail.index[-1], *list(pred.index)]
    bridge_y = [float(actual_tail["close"].iloc[-1]), *pred["close"].tolist()]
    ax.plot(
        bridge_x,
        bridge_y,
        color="red",
        linewidth=1.8,
        marker="o",
        markersize=5,
        label="Kronos prediction (Aug 10–12)",
    )
    ax.axvline(actual_tail.index[-1], color="gray", linestyle="--", linewidth=0.8, alpha=0.6)
    for ts, row in pred.iterrows():
        ax.annotate(
            f"{row['close']:.2f}",
            (ts, row["close"]),
            textcoords="offset points",
            xytext=(0, 10),
            ha="center",
            fontsize=9,
            color="red",
        )
    ax.set_title(
        f"AAPL Kronos forecast — Aug 10–12 2026\n"
        f"(context through {hist.index[-1].date()}, lookback={use})"
    )
    ax.set_ylabel("Close")
    ax.legend(loc="best")
    ax.grid(True, alpha=0.3)
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(OUT)
    plt.close(fig)
    log.info(f"kronos-aug | wrote {OUT}")


if __name__ == "__main__":
    main()
