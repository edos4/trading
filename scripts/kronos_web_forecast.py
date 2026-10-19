"""
scripts/kronos_web_forecast.py — forward-looking Kronos forecasts (web UI).

Runs BOTH Kronos-base and the fine-tuned predictor on the most recent
``--lookback`` daily bars of one or more symbols and writes JSON with the recent
actual closes plus the two forward forecast paths for each. This is the GPU
worker the web UI shells out to (the app venv is torch-free, so the model only
resolves inside ``.venv-kronos``).

    .venv-kronos/bin/python scripts/kronos_web_forecast.py \
        --symbols AAPL NVDA MSFT --days 5 --out /tmp/kronos_web.json

Each model is loaded once and reused across every symbol (2 loads per run, not
2 × symbols), so a batch costs barely more than a single symbol. A symbol that
has no bars — or is missing a model path — gets an ``errors`` entry and never
sinks the rest of the batch.

JSON written to ``--out``::

    {
      "days": 5,
      "lookback": 400,
      "symbols": ["AAPL", "NVDA"],
      "results": [
        {
          "symbol": "AAPL",
          "days": 5,
          "lookback": 400,
          "anchor": {"time": "2026-10-14", "value": 250.1},
          "history": [{"time": "2026-04-01", "value": 195.2}, ...],
          "base":      [{"time": "2026-10-15", "value": 249.0}, ...],
          "finetuned": [{"time": "2026-10-15", "value": 247.5}, ...],
          "errors": {"base": "...", "data": "..."}   # absent when clean
        },
        ...
      ]
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
        raise ValueError(f"no daily bars for {symbol} in stocks_history")
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


def _parse_symbols(raw: list[str]) -> list[str]:
    """Upper-case, drop spaces/empties, de-dupe while preserving order."""
    seen: list[str] = []
    for item in raw:
        for chunk in str(item).split(","):
            sym = chunk.replace(" ", "").strip().upper()
            if sym and sym not in seen:
                seen.append(sym)
    return seen


def _prepare(symbols: list[str], days: int, lookback: int, history: int) -> tuple[dict, dict]:
    """Load bars per symbol; returns ({symbol: df}, {symbol: result entry}).

    A symbol with no (or too few) bars gets an ``errors['data']`` entry and is
    left out of the model loop.
    """
    frames: dict[str, pd.DataFrame] = {}
    results: dict[str, dict] = {}
    for sym in symbols:
        entry: dict = {
            "symbol": sym,
            "days": days,
            "lookback": lookback,
            "anchor": None,
            "history": [],
            "base": [],
            "finetuned": [],
            "errors": {},
        }
        results[sym] = entry
        try:
            df = load_symbol_df(sym)
            if len(df) < lookback:
                raise ValueError(
                    f"only {len(df)} bars; need {lookback} for a Kronos context"
                )
        except ValueError as exc:
            log.warning(f"Kronos-web | {sym}: {exc}")
            entry["errors"]["data"] = str(exc)
            continue
        frames[sym] = df
        entry["anchor"] = {
            "time": _iso(df.index[-1]),
            "value": round(float(df["close"].iloc[-1]), 4),
        }
        entry["history"] = [
            {"time": _iso(t), "value": round(float(v), 4)}
            for t, v in df["close"].tail(history).items()
        ]
    return frames, results


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbols", required=True, nargs="+",
                    help="one or more tickers (commas also accepted)")
    ap.add_argument("--days", type=int, required=True)
    ap.add_argument("--lookback", type=int, default=LOOKBACK)
    ap.add_argument("--history", type=int, default=120,
                    help="recent actual bars to include for the chart")
    ap.add_argument("--sample-count", type=int, default=1)
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)

    if args.days < 1:
        raise SystemExit("--days must be >= 1")

    symbols = _parse_symbols(args.symbols)
    if not symbols:
        raise SystemExit("no symbols given")

    frames, results = _prepare(symbols, args.days, args.lookback, args.history)

    # Load each model once, then forecast every symbol from it.
    sys.path.insert(0, str(Path.home() / "Kronos"))
    from core.kronos_eval import _load_predictor

    if frames:
        for tag, use_ft in (("base", False), ("finetuned", True)):
            try:
                log.info(
                    f"Kronos-web | loading {tag} for "
                    f"{len(frames)} symbol(s) +{args.days}d ..."
                )
                predictor = _load_predictor(device="cuda", use_finetuned=use_ft)
            except Exception as exc:  # a missing/broken model must not hide bars
                log.exception(f"Kronos-web | {tag} model failed to load")
                for sym in frames:
                    results[sym]["errors"][tag] = f"{type(exc).__name__}: {exc}"
                continue
            for sym, df in frames.items():
                try:
                    results[sym][tag] = forecast_series(
                        predictor, df, args.days, args.lookback, args.sample_count,
                    )
                except Exception as exc:  # one model failing must not hide the other
                    log.exception(f"Kronos-web | {sym} {tag} failed")
                    results[sym]["errors"][tag] = f"{type(exc).__name__}: {exc}"
            del predictor
            import torch as _torch

            _torch.cuda.empty_cache()

    for entry in results.values():
        if not entry["errors"]:
            del entry["errors"]

    payload = {
        "days": args.days,
        "lookback": args.lookback,
        "symbols": [e["symbol"] for e in results.values()],
        "results": list(results.values()),
    }
    if not any(e["base"] or e["finetuned"] for e in results.values()):
        detail = "; ".join(
            f"{e['symbol']} ({e.get('errors') or 'no data'})"
            for e in results.values()
        )
        raise SystemExit(f"every symbol failed: {detail}")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload), encoding="utf-8")
    log.info(f"Kronos-web | wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
