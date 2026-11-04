"""scripts/kronos_confirm_worker.py — batched 1-day Kronos votes for the scanner.

Runs in ``.venv-kronos`` on the GPU host. Loads one model (fine-tuned by
default, never both), then performs ``--samples`` independent one-day
forecasts per symbol. Each call is seeded from (symbol, anchor date, sample
index) so a rerun reproduces the same votes and the scanner cache is truthful.

    .venv-kronos/bin/python scripts/kronos_confirm_worker.py \
        --symbols AAPL MSFT --samples 3 --model finetuned --out /tmp/confirm.json

The JSON payload is written to ``--out`` (log lines go to stdout):
``{"model": ..., "samples": ..., "device": ..., "results": [{"symbol": ...,
"anchor_close": ..., "anchor_date": ..., "returns": [...], "error": "..."}]}``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import zlib
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

from core.kronos_eval import LOOKBACK, MAX_CONTEXT, load_predictor, with_amount
from utils.logger import log

MIN_BARS = 60


def load_symbol_df(symbol: str, as_of: str | None = None) -> pd.DataFrame:
    """Daily bars, optionally cut at ``as_of`` (a replay anchor date).

    A historical replay must not let the model read bars that had not printed
    yet; without the cutoff the worker sees the live series.
    """
    from data.db import get_conn

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            sql = (
                "SELECT bar_date, open, high, low, close, volume "
                "FROM daily_bars WHERE symbol = %s"
            )
            params: list = [symbol.upper()]
            if as_of:
                sql += " AND bar_date <= %s"
                params.append(as_of)
            sql += " ORDER BY ts"
            cur.execute(sql, params)
            rows = cur.fetchall()
    finally:
        conn.close()
    if not rows:
        raise ValueError(f"no daily bars for {symbol} in stocks_history")
    df = pd.DataFrame(rows, columns=["bar_date", "open", "high", "low", "close", "volume"])
    df.index = pd.to_datetime(df["bar_date"])
    df = df.sort_index()
    return df[["open", "high", "low", "close", "volume"]][~df.index.duplicated(keep="last")]


def _prep_1d_frame(df: pd.DataFrame, lookback: int):
    if df is None or len(df) < MIN_BARS:
        return None
    work = df.dropna(subset=["open", "high", "low", "close"]).copy()
    if len(work) < MIN_BARS:
        return None
    work["volume"] = pd.to_numeric(work["volume"], errors="coerce").fillna(0.0)
    use = min(int(lookback), len(work), MAX_CONTEXT)
    if use < MIN_BARS:
        return None
    x_df = with_amount(work.iloc[-use:])
    last_close = float(x_df["close"].iloc[-1])
    if last_close <= 0:
        return None
    x_timestamp = pd.Series(pd.DatetimeIndex(x_df.index))
    last = pd.Timestamp(x_df.index[-1])
    y_timestamp = pd.Series(
        pd.bdate_range(start=last + pd.Timedelta(days=1), periods=1)
    )
    return x_df.reset_index(drop=True), x_timestamp, y_timestamp, last_close, last


def _seed(symbol: str, anchor_date: str, sample_index: int) -> int:
    raw = f"{symbol.upper()}:{anchor_date}:{sample_index}".encode("utf-8")
    return zlib.crc32(raw) & 0x7FFFFFFF


def forecast_returns(
    predictor, symbol: str, df: pd.DataFrame, samples: int, lookback: int = LOOKBACK,
) -> dict:
    prepared = _prep_1d_frame(df, lookback)
    if prepared is None:
        raise ValueError(f"only {len(df)} usable bars; need {MIN_BARS}")
    x_df, x_timestamp, y_timestamp, anchor_close, anchor_ts = prepared
    anchor_date = anchor_ts.date().isoformat()
    use_seed = callable(getattr(predictor, "set_seed", None))
    returns: list[float] = []
    predicted: list[float] = []
    for index in range(samples):
        seed = _seed(symbol, anchor_date, index)
        if use_seed:
            predictor.set_seed(seed)
        else:
            import torch

            torch.manual_seed(seed)
        pred = predictor.predict(
            df=x_df,
            x_timestamp=x_timestamp,
            y_timestamp=y_timestamp,
            pred_len=1,
            T=1.0,
            top_p=0.9,
            sample_count=1,
            verbose=False,
        )
        predicted_close = float(pred["close"].iloc[0])
        predicted.append(round(predicted_close, 4))
        returns.append(predicted_close / anchor_close - 1.0)
    return {
        "symbol": symbol.upper(),
        "anchor_close": round(anchor_close, 4),
        "anchor_date": anchor_date,
        "predicted": predicted,
        "returns": [round(value, 6) for value in returns],
    }


def _parse_symbols(raw: list[str]) -> list[str]:
    seen: list[str] = []
    for item in raw:
        for chunk in str(item).split(","):
            symbol = chunk.replace(" ", "").strip().upper()
            if symbol and symbol not in seen:
                seen.append(symbol)
    return seen


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbols", required=True, nargs="+")
    ap.add_argument("--samples", type=int, default=3)
    ap.add_argument("--model", choices=("base", "finetuned"), default="finetuned")
    ap.add_argument("--lookback", type=int, default=LOOKBACK)
    ap.add_argument(
        "--as-of", default=None,
        help="ignore bars after this ISO date (historical replay anchor)",
    )
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)

    symbols = _parse_symbols(args.symbols)
    if not symbols:
        raise SystemExit("no symbols given")
    samples = max(1, min(int(args.samples), 9))
    samples = samples if samples % 2 else max(1, samples - 1)

    predictor, device = load_predictor(
        use_finetuned=args.model == "finetuned",
    )
    log.info(f"Kronos-confirm | device={device}")
    results = []
    for symbol in symbols:
        try:
            df = load_symbol_df(symbol, args.as_of)
            results.append(
                forecast_returns(
                    predictor, symbol, df, samples, args.lookback,
                )
            )
        except Exception as exc:
            log.exception(f"Kronos-confirm | {symbol} failed")
            results.append({
                "symbol": symbol,
                "returns": [],
                "error": f"{type(exc).__name__}: {exc}",
            })

    payload = {
        "model": args.model,
        "samples": samples,
        "device": device,
        "results": results,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload), encoding="utf-8")
    log.info(f"Kronos-confirm | wrote {out} ({len(symbols)} symbols, {samples} samples)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
