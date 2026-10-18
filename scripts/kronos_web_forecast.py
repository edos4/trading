"""
scripts/kronos_web_forecast.py — one forward-looking Kronos forecast (web UI).

Runs BOTH Kronos-base and the fine-tuned predictor on the most recent
``--lookback`` daily bars of a single symbol and writes JSON with the recent
actual closes plus the two forward forecast paths. This is the GPU worker the
web UI shells out to (the app venv is torch-free, so the model only resolves
inside ``.venv-kronos``).

    .venv-kronos/bin/python scripts/kronos_web_forecast.py \
        --symbol AAPL --days 5 --out /tmp/kronos_web.json

JSON written to ``--out``::

    {
      "symbol": "AAPL",
      "days": 5,
      "lookback": 400,
      "anchor": {"time": "2026-10-14", "value": 250.1},
      "history": [{"time": "2026-04-01", "value": 195.2}, ...],
      "base":      [{"time": "2026-10-15", "value": 249.0}, ...],
      "finetuned": [{"time": "2026-10-15", "value": 247.5}, ...],
      "errors": {"base": "...", "finetuned": "..."}   # absent when clean
    }

Log lines go to stdout (loguru console sink), so the payload is a file, never
stdout — the caller reads ``--out``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

from core.kronos_eval import LOOKBACK, with_amount
from utils.logger import log


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
        raise SystemExit(f"no daily bars for {symbol} in stocks_history")
    df = pd.DataFrame(rows, columns=["bar_date", "open", "high", "low", "close", "volume"])
    df.index = pd.to_datetime(df["bar_date"])
    df = df.sort_index()
    return df[["open", "high", "low", "close", "volume"]][~df.index.duplicated(keep="last")]


def _iso(ts) -> str:
    return pd.Timestamp(ts).strftime("%Y-%m-%d")


def forecast_series(
    predictor, df: pd.DataFrame, days: int, lookback: int, sample_count: int,
) -> list[dict]:
    """Forecast ``days`` business days past the last bar; list of {time,value}."""
    ctx = with_amount(df.tail(lookback))
    if len(ctx) < 60:
        raise ValueError(f"only {len(ctx)} context bars (need >= 60)")
    last = pd.Timestamp(ctx.index[-1])
    # Walk-forward contract: every predicted bar is strictly after the last
    # context bar (the anchor). Future sessions are business days because the
    # real exchange calendar is not knowable ahead of the data.
    y_idx = pd.bdate_range(start=last + pd.Timedelta(days=1), periods=days)
    x_df = ctx.reset_index(drop=True)
    x_ts = pd.Series(pd.DatetimeIndex(ctx.index))
    y_ts = pd.Series(pd.DatetimeIndex(y_idx))
    out = predictor.predict(
        df=x_df, x_timestamp=x_ts, y_timestamp=y_ts, pred_len=days,
        T=1.0, top_p=0.9, sample_count=sample_count, verbose=False,
    )
    return [
        {"time": _iso(t), "value": round(float(v), 4)}
        for t, v in zip(y_idx, out["close"].tolist())
    ]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--days", type=int, required=True)
    ap.add_argument("--lookback", type=int, default=LOOKBACK)
    ap.add_argument("--history", type=int, default=120,
                    help="recent actual bars to include for the chart")
    ap.add_argument("--sample-count", type=int, default=1)
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)

    symbol = args.symbol.upper().strip()
    if args.days < 1:
        raise SystemExit("--days must be >= 1")

    df = load_symbol_df(symbol)
    if len(df) < args.lookback:
        raise SystemExit(
            f"{symbol}: only {len(df)} bars; need {args.lookback} for a Kronos context"
        )
    anchor_time = _iso(df.index[-1])
    anchor_value = round(float(df["close"].iloc[-1]), 4)
    history = [
        {"time": _iso(t), "value": round(float(v), 4)}
        for t, v in df["close"].tail(args.history).items()
    ]

    sys.path.insert(0, str(Path.home() / "Kronos"))
    from core.kronos_eval import _load_predictor

    payload: dict = {
        "symbol": symbol,
        "days": args.days,
        "lookback": args.lookback,
        "anchor": {"time": anchor_time, "value": anchor_value},
        "history": history,
        "base": [],
        "finetuned": [],
    }
    errors: dict[str, str] = {}
    for tag, use_ft in (("base", False), ("finetuned", True)):
        try:
            log.info(f"Kronos-web | {symbol} +{args.days}d: loading {tag} ...")
            predictor = _load_predictor(device="cuda", use_finetuned=use_ft)
            payload[tag] = forecast_series(
                predictor, df, args.days, args.lookback, args.sample_count,
            )
            del predictor
            import torch as _torch

            _torch.cuda.empty_cache()
        except Exception as exc:  # one model failing must not hide the other
            log.exception(f"Kronos-web | {symbol} {tag} failed")
            errors[tag] = f"{type(exc).__name__}: {exc}"
    if errors:
        payload["errors"] = errors
    if not payload["base"] and not payload["finetuned"]:
        raise SystemExit(
            f"{symbol}: both models failed ({errors or 'unknown error'})"
        )

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload), encoding="utf-8")
    log.info(f"Kronos-web | wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
