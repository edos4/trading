#!/usr/bin/env python3
"""
scripts/kronos_test.py — Plot AAPL Apr–Jun actual (blue) vs Kronos 5d preds (red).

Walk-forward: every 5 trading days, feed lookback of true history, predict the
next 5 closes, stitch into one red series. Same dates as actual for comparison.

  First window: history through 2026-03-31 → predict Apr 1,2,6,7,8.

Usage:
    .venv/bin/python scripts/kronos_test.py
    .venv/bin/python scripts/kronos_test.py --year 2025
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.kronos_eval import (  # noqa: E402
    LOOKBACK,
    MAX_CONTEXT,
    WEEK_AHEAD,
    _load_predictor,
    with_amount,
)
from utils.logger import log  # noqa: E402

DEFAULT_CSV = Path("/home/r00t/stocks_data/A/AAPL.csv")
DEFAULT_OUT = ROOT / "kronos_test.png"
DEFAULT_YEAR = 2026
DEFAULT_LOOKBACK = LOOKBACK
DEFAULT_HORIZON = WEEK_AHEAD
MIN_LOOKBACK = 60


def load_aapl(csv_path: Path) -> pd.DataFrame:
    raw = pd.read_csv(csv_path)
    raw["timestamp"] = pd.to_datetime(raw["timestamp"], unit="s")
    return (
        raw.sort_values("timestamp")
        .drop_duplicates("timestamp")
        .set_index("timestamp")[["open", "high", "low", "close", "volume"]]
    )


def april_june_slice(df: pd.DataFrame, year: int) -> tuple[pd.DataFrame, int, int]:
    """Return (full df, forecast_start_i, forecast_end_i exclusive)."""
    start = pd.Timestamp(f"{year}-04-01")
    end = pd.Timestamp(f"{year}-07-01")
    pred_mask = (df.index.normalize() >= start) & (df.index.normalize() < end)
    pred_idx = df.index[pred_mask]
    if len(pred_idx) == 0:
        raise SystemExit(f"No AAPL bars for {year}-04-01 .. {year}-06-30 in {DEFAULT_CSV}")

    forecast_start_i = int(df.index.get_indexer([pred_idx[0]])[0])
    forecast_end_i = int(df.index.get_indexer([pred_idx[-1]])[0]) + 1
    return df, forecast_start_i, forecast_end_i


def resolve_lookback(available: int, requested: int | None) -> int:
    cap = min(available, MAX_CONTEXT)
    lookback = DEFAULT_LOOKBACK if requested is None else requested
    lookback = min(lookback, cap)
    if lookback < MIN_LOOKBACK:
        raise SystemExit(
            f"Resolved lookback={lookback} < MIN_LOOKBACK={MIN_LOOKBACK} "
            f"(available={available}, max_context={MAX_CONTEXT})"
        )
    if lookback < DEFAULT_LOOKBACK:
        log.warning(
            f"kronos-test | lookback={lookback} < default {DEFAULT_LOOKBACK} "
            f"(only {available} bars available before forecast start)"
        )
    return lookback


def walk_forward_5d(
    predictor,
    df: pd.DataFrame,
    forecast_start_i: int,
    forecast_end_i: int,
    lookback: int,
    horizon: int,
    sample_count: int,
) -> list[dict]:
    """Non-overlapping walk-forward: each window predicts ``horizon`` trading days.

    Window k uses true history ending the bar before that window's first forecast
    day — e.g. first Apr window predicts from Mar 31 into Apr 1.. (5 trading days).
    """
    if forecast_start_i < lookback:
        raise SystemExit(
            f"Need ≥{lookback} lookback bars before forecast start; "
            f"only {forecast_start_i} available"
        )

    windows: list[dict] = []
    cursor = forecast_start_i
    n_days = forecast_end_i - forecast_start_i

    while cursor < forecast_end_i:
        step = min(horizon, forecast_end_i - cursor)
        x_df = with_amount(df.iloc[cursor - lookback : cursor])
        y_actual = df.iloc[cursor : cursor + step]
        pred_df = predictor.predict(
            df=x_df.reset_index(drop=True),
            x_timestamp=pd.Series(x_df.index),
            y_timestamp=pd.Series(y_actual.index),
            pred_len=step,
            T=1.0,
            top_p=0.9,
            sample_count=sample_count,
            verbose=False,
        )
        pred_close = pred_df["close"].copy()
        pred_close.index = y_actual.index
        last = float(x_df["close"].iloc[-1])
        windows.append(
            {
                "origin": x_df.index[-1],  # last actual bar fed to Kronos
                "forecast_start": y_actual.index[0],
                "forecast_end": y_actual.index[-1],
                "actual": y_actual["close"].astype(float),
                "pred": pred_close.astype(float),
                "pred_ret": float(pred_close.iloc[-1]) / last - 1.0,
                "act_ret": float(y_actual["close"].iloc[-1]) / last - 1.0,
                "mae": float((pred_close - y_actual["close"]).abs().mean()),
            }
        )
        cursor += step
        done = cursor - forecast_start_i
        log.info(
            f"kronos-test | window origin={windows[-1]['origin'].date()} → "
            f"{windows[-1]['forecast_start'].date()}..{windows[-1]['forecast_end'].date()} "
            f"({done}/{n_days} bars covered)"
        )

    return windows


def run(
    csv_path: Path,
    out_path: Path,
    year: int,
    device: str,
    sample_count: int,
    lookback: int | None = None,
    horizon: int = DEFAULT_HORIZON,
) -> Path:
    df, forecast_start_i, forecast_end_i = april_june_slice(load_aapl(csv_path), year)
    use_lookback = resolve_lookback(forecast_start_i, lookback)
    y_actual = df.iloc[forecast_start_i:forecast_end_i]

    log.info(
        f"kronos-test | AAPL {year} Apr–Jun | {horizon}d walk-forward | "
        f"lookback={use_lookback}/{MAX_CONTEXT} | "
        f"range {y_actual.index[0].date()}→{y_actual.index[-1].date()} "
        f"({len(y_actual)} bars)"
    )

    predictor = _load_predictor(device=device)
    if getattr(predictor, "max_context", MAX_CONTEXT) < use_lookback:
        raise SystemExit(
            f"Predictor max_context={predictor.max_context} < lookback={use_lookback}"
        )

    windows = walk_forward_5d(
        predictor,
        df,
        forecast_start_i,
        forecast_end_i,
        use_lookback,
        horizon,
        sample_count,
    )

    pred_all = pd.concat([w["pred"] for w in windows])
    act_all = y_actual["close"]
    mae = float((pred_all - act_all).abs().mean())
    mape = float(((pred_all - act_all).abs() / act_all).mean() * 100)
    pred_ret = np.array([w["pred_ret"] for w in windows])
    act_ret = np.array([w["act_ret"] for w in windows])
    dir_hit = float((np.sign(pred_ret) == np.sign(act_ret)).mean() * 100)
    w0 = windows[0]
    log.info(
        f"kronos-test | windows={len(windows)} close MAE=${mae:.2f} MAPE={mape:.2f}% "
        f"| {horizon}d-ret dir hit={dir_hit:.1f}%"
    )
    log.info(
        f"kronos-test | example window: from {w0['origin'].date()} predict "
        f"{w0['forecast_start'].date()} .. {w0['forecast_end'].date()} "
        f"({len(w0['pred'])} trading days)"
    )

    # Simple overlay: blue = actual close, red = Kronos 5d walk-forward preds.
    fig, ax = plt.subplots(figsize=(12, 5), dpi=120)
    ax.plot(
        act_all.index,
        act_all.values,
        color="blue",
        linewidth=1.8,
        label="Actual",
    )
    ax.plot(
        pred_all.index,
        pred_all.values,
        color="red",
        linewidth=1.8,
        label=f"Kronos {horizon}d prediction",
    )
    ax.set_title(f"AAPL — actual vs Kronos {horizon}d prediction ({year} Apr–Jun)")
    ax.set_ylabel("Close")
    ax.legend(loc="best")
    ax.grid(True, alpha=0.3)
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(out_path)
    plt.close(fig)
    log.info(f"kronos-test | wrote {out_path}")
    return out_path


def main() -> None:
    p = argparse.ArgumentParser(description="Kronos AAPL Apr–Jun 5d walk-forward plot")
    p.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    p.add_argument("--out", type=Path, default=DEFAULT_OUT)
    p.add_argument("--year", type=int, default=DEFAULT_YEAR)
    p.add_argument("--device", default="cuda")
    p.add_argument("--sample-count", type=int, default=3)
    p.add_argument(
        "--lookback",
        type=int,
        default=None,
        help=f"Context bars (default: {DEFAULT_LOOKBACK}, capped at {MAX_CONTEXT})",
    )
    p.add_argument(
        "--horizon",
        type=int,
        default=DEFAULT_HORIZON,
        help=f"Trading days predicted per window (default: {DEFAULT_HORIZON})",
    )
    args = p.parse_args()
    run(
        args.csv,
        args.out,
        args.year,
        args.device,
        args.sample_count,
        args.lookback,
        args.horizon,
    )


if __name__ == "__main__":
    main()
