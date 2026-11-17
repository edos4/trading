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
import numpy as np

from core.kronos_eval import LOOKBACK, MAX_CONTEXT, load_predictor, with_amount
from core.kronos_input import InsufficientHistory
from utils.logger import log

MIN_BARS = 60

#: Structured failure classes for the gate. ``insufficient_history`` means the
#: symbol's own series is too short for the model's minimum context: no retry of
#: that anchor can succeed, so the gate must not spend its bounded replay retry
#: on it (the third try stops a whole replay run).
SYMBOL_HISTORY_ERROR_KIND = "insufficient_history"


def error_kind(exc: BaseException) -> str:
    """Classify a per-symbol worker failure for the caller's retry decision."""
    return (SYMBOL_HISTORY_ERROR_KIND if isinstance(exc, InsufficientHistory)
            else "worker_error")


def load_symbol_df(symbol: str, as_of: str | None = None, market: str = "us") -> pd.DataFrame:
    """Daily bars, optionally cut at ``as_of`` (a replay anchor date).

    A historical replay must not let the model read bars that had not printed
    yet; without the cutoff the worker sees the live series.
    """
    from data.db import get_conn
    if market == "ph":
        from core.market import ph_history_symbol
        symbol = ph_history_symbol(symbol)

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            sql = (
                "SELECT b.bar_date, b.open, b.high, b.low, b.close, b.volume "
                "FROM daily_bars b JOIN symbols s ON s.symbol = b.symbol "
                "WHERE b.symbol = %s AND s.market = %s"
            )
            params: list = [symbol.upper(), market]
            if as_of:
                sql += " AND b.bar_date <= %s"
                params.append(as_of)
            sql += " ORDER BY b.ts"
            cur.execute(sql, params)
            rows = cur.fetchall()
    finally:
        conn.close()
    if not rows:
        raise InsufficientHistory(f"no daily bars for {symbol} in stocks_history")
    df = pd.DataFrame(rows, columns=["bar_date", "open", "high", "low", "close", "volume"])
    df.index = pd.to_datetime(df["bar_date"])
    df = df.sort_index()
    return df[["open", "high", "low", "close", "volume"]][~df.index.duplicated(keep="last")]


def _prep_1d_frame(df: pd.DataFrame, lookback: int):
    if df is None or len(df) < MIN_BARS:
        return None
    work = df.copy().sort_index()
    work = work[~work.index.duplicated(keep="last")]
    use = min(int(lookback), len(work), MAX_CONTEXT)
    if use < MIN_BARS:
        return None
    work = work.iloc[-use:].copy()
    from core.kronos_input import prepare_context
    try:
        work = prepare_context(work, use).frame
    except InsufficientHistory:
        # The symbol's own regime is shorter than the minimum context: report
        # the specific reason instead of folding it into the generic
        # invalid-context branch, which callers would treat as a worker defect.
        raise
    except ValueError:
        return None
    columns = ["open", "high", "low", "close", "volume"]
    if any(col not in work for col in columns) or not isinstance(work.index, pd.DatetimeIndex):
        return None
    work[columns] = work[columns].apply(pd.to_numeric, errors="coerce")
    work["volume"] = work["volume"].fillna(0.0)
    if (len(work) < MIN_BARS or work.index.isna().any()
            or not np.isfinite(work[columns].to_numpy()).all()
            or (work[columns[:4]] <= 0).any().any() or (work["volume"] < 0).any()
            or (work["high"] < work[columns[:4]].max(axis=1)).any()
            or (work["low"] > work[columns[:4]].min(axis=1)).any()):
        return None
    x_df = with_amount(work)
    last_close = float(x_df["close"].iloc[-1])
    if not np.isfinite(x_df.to_numpy()).all() or last_close <= 0:
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


def _reported_anchor_close(anchor_close: float) -> float:
    """Keep four-decimal quotes without turning a sub-penny close into zero."""
    rounded = round(float(anchor_close), 4)
    return rounded if rounded > 0 else float(anchor_close)


def _credible_close(predicted_close: float, anchor_close: float) -> bool:
    """Whether a sampled close may stand as a one-day forecast.

    Non-finite, nonpositive, collapsed and exploded closes are all model
    artifacts rather than directional evidence (docs/kronos-3-preds.md §4), so
    all of them are rejected the same way.
    """
    from core.kronos_confirm import MAX_CREDIBLE_RETURN

    if not np.isfinite(predicted_close) or predicted_close <= 0:
        return False
    if not np.isfinite(anchor_close) or anchor_close <= 0:
        return False
    return abs(predicted_close / anchor_close - 1.0) <= MAX_CREDIBLE_RETURN


def forecast_returns(
    predictor, symbol: str, df: pd.DataFrame, samples: int, lookback: int = LOOKBACK,
) -> dict:
    from core.kronos_confirm import atr_pct_from_frame

    from core.kronos_input import prepare_context
    if df is None or len(df) < MIN_BARS:
        raise InsufficientHistory(f"only {0 if df is None else len(df)} bars; need {MIN_BARS}")
    selection = prepare_context(df, lookback)
    prepared = _prep_1d_frame(selection.frame, lookback)
    if prepared is None:
        raise ValueError("Invalid OHLC context: require finite positive prices, valid ranges and nonnegative volume")
    x_df, x_timestamp, y_timestamp, anchor_close, anchor_ts = prepared
    anchor_date = anchor_ts.date().isoformat()
    model_context = selection.evidence()
    use_seed = callable(getattr(predictor, "set_seed", None))
    returns: list[float] = []
    predicted: list[float] = []
    rejected = 0
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
        # One unusable simulation must not discard the whole symbol. Dropping it
        # leaves the gate's majority rule to block on the survivors, where
        # aborting the symbol raised a raw worker error on every scan instead.
        if not _credible_close(predicted_close, anchor_close):
            rejected += 1
            continue
        predicted.append(round(predicted_close, 4))
        returns.append(predicted_close / anchor_close - 1.0)
    if rejected:
        log.warning(
            f"Kronos-confirm | {symbol} dropped {rejected}/{samples} unusable simulations"
        )
    # A model that produced no usable simulation has nothing to say, which is
    # the gate's no_opinion case, not a worker failure. Raising here put an
    # unreadable ValueError in the Paper signal log on every scan of the same
    # (symbol, anchor date); the surviving count travels in the payload instead.
    return {
        "symbol": symbol.upper(),
        "model_context": model_context,
        "anchor_close": _reported_anchor_close(anchor_close),
        "anchor_date": anchor_date,
        "atr_pct": atr_pct_from_frame(x_df, anchor_close),
        "predicted": predicted,
        "returns": [round(value, 6) for value in returns],
        "rejected": rejected,
    }


def _parse_symbols(raw: list[str]) -> list[str]:
    seen: list[str] = []
    for item in raw:
        for chunk in str(item).split(","):
            symbol = chunk.replace(" ", "").strip().upper()
            if symbol and symbol not in seen:
                seen.append(symbol)
    return seen


def load_confirmation_predictor(model: str, model_root: Path | None = None):
    """Confirmation must never silently substitute base for selected weights."""
    if model == "finetuned":
        from core.kronos_finetune import PREDICTOR_OUT, TOKENIZER_OUT
        if not (PREDICTOR_OUT.exists() and TOKENIZER_OUT.exists()):
            raise ValueError("Selected fine-tuned Kronos checkpoints are missing; confirmation unavailable")
    extra = {"model_root": model_root} if model_root is not None else {}
    predictor, device = load_predictor(use_finetuned=model == "finetuned", **extra)
    for component in (getattr(predictor, "model", None), getattr(predictor, "tokenizer", None)):
        if callable(getattr(component, "eval", None)):
            component.eval()
    return predictor, device


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbols", required=True, nargs="+")
    ap.add_argument("--samples", type=int, default=3)
    ap.add_argument("--model", choices=("base", "finetuned"), default="finetuned")
    ap.add_argument("--market", choices=("us", "ph"), default="us")
    ap.add_argument("--lookback", type=int, default=LOOKBACK)
    ap.add_argument(
        "--as-of", default=None,
        help="ignore bars after this ISO date (historical replay anchor)",
    )
    ap.add_argument("--out", required=True)
    ap.add_argument('--decision-at', default=None, help='Current live decision timestamp; replay omits this')
    ap.add_argument('--require-provenance',action='store_true')
    ap.add_argument('--contexts',default=None,help='Content-addressed daily snapshots supplied by the scanner')
    args = ap.parse_args(argv)

    symbols = _parse_symbols(args.symbols)
    if not symbols:
        raise SystemExit("no symbols given")
    samples = max(1, min(int(args.samples), 9))
    samples = samples if samples % 2 else max(1, samples - 1)

    from core.kronos_provenance import decision_provenance
    from core.kronos_finetune import FINETUNE_DIR
    model_root = FINETUNE_DIR.resolve() if args.model == "finetuned" else None
    provenance=(decision_provenance(model_root,args.as_of,market=args.market,decision_at=args.decision_at,required=args.require_provenance)
                if args.model=='finetuned' else {'identity_verified':False,'out_of_sample_eligible':False,
                                                'reason':'base model training/selection boundary unavailable'})
    if args.require_provenance and not provenance['identity_verified']:
        raise ValueError(provenance['reason'])
    predictor, device = load_confirmation_predictor(args.model, model_root)
    log.info(f"Kronos-confirm | device={device}")
    results = []
    contexts=json.loads(Path(args.contexts).read_text()) if args.contexts else None
    for symbol in symbols:
        try:
            if contexts is not None:
                from core.kronos_context import context_frame
                if symbol not in contexts or not args.as_of:
                    raise ValueError('Missing dated scanner model context')
                df=context_frame(contexts[symbol],args.as_of)
            else:
                df=load_symbol_df(symbol,args.as_of,args.market)
            row=forecast_returns(predictor,symbol,df,samples,args.lookback)
            row['context_sha256']=contexts[symbol]['sha256'] if contexts is not None else None
            results.append(row)
        except Exception as exc:
            log.exception(f"Kronos-confirm | {symbol} failed")
            results.append({
                "symbol": symbol,
                "returns": [],
                "error": f"{type(exc).__name__}: {exc}",
                # The gate decides retry-vs-record from this, not from the
                # message text: a short series is a symbol defect, an
                # unavailable worker is not.
                "error_kind": error_kind(exc),
            })

    payload = {
        'model_provenance':provenance,
        'model_identity': provenance.get('manifest_sha256', f'legacy:{model_root}') if args.model == 'finetuned' else 'base',
        "market": args.market,
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
    from core.kronos_process import locked_main
    raise SystemExit(locked_main(main))
