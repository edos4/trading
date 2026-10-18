"""
learn/kronos_dataset.py — lean fine-tune corpus builder for Kronos-base.

Implements docs/kronos-finetune.md §5 (P1-01 / P1-02).

Why this exists: the old `prepare_dataset` ranked the top 1,500 liquid
symbols and pickled a `dict[symbol] -> DataFrame` (float64 + DatetimeIndex).
At the full 17,719-symbol universe that design is OOM-adjacent (~2.5 GB
pickle, doubled on load, plus ~36M `(symbol, i)` index tuples ≈ 2.6–2.9 GB
for train alone) on a box with only ~20 GB free (FreeToken MoE offload eats
the rest). This builder instead:

  - streams one ordered `SELECT ... ORDER BY symbol, ts` (no 17,719
    round-trips, no per-symbol DataFrame pile-up),
  - writes one contiguous float32 array + offset index (~1.3 GB on disk,
    near-zero index overhead via cumulative `window_count` offsets),
  - derives time features from a stored day-ordinal (no minute/hour columns
    for daily bars — they are re-emitted as constant 0 at sample time to
    keep the 5-dim stamp contract the pretrained weights expect),
  - splits chronologically per symbol at a single `--train-cutoff` so
    walk-forward evaluation strictly after the cutoff cannot overlap
    training (the old last-10%-per-symbol split left this ambiguous).

Artifacts under `<out_dir>/` (default `~/Kronos/finetuned/dataset/`):

  - `features.f32` — float32[N, 6] `[open, high, low, close, volume, amount]`,
    all symbols concatenated in symbol order. Only pre-cutoff bars are
    stored; post-cutoff bars are held out entirely (never written).
  - `days.i32` — int32[N] date ordinal per bar (for weekday/day/month stamps).
  - `symbols.json` — per symbol `{name, market, offset, count, train_count,
    val_count, val_offset, window_count}` plus `train_cutoff` header.
  - `build_report.json` — corpus stats (symbols kept/dropped, bars, US/PH
    split, train/val sizes).

Torch-free on purpose: dataset build and unit tests run in the app venv.
The torch `Dataset` wrapper lives in `core/kronos_finetune.py` (kronos venv).
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

FEATURE_COLS = ["open", "high", "low", "close", "volume", "amount"]
# Model contract is 5-dim stamps (minute, hour, weekday, day, month).
# Daily bars have no intraday clock: minute/hour are constant 0, computed at
# sample time, never stored. Storage is just the day ordinal.
STAMP_COLS = ["minute", "hour", "weekday", "day", "month"]

FEATURES_N = 6
DEFAULT_TRAIN_CUTOFF = "2026-01-01"
DEFAULT_VAL_FRAC = 0.1

FEATURES_FILE = "features.f32"
DAYS_FILE = "days.i32"
SYMBOLS_FILE = "symbols.json"
REPORT_FILE = "build_report.json"


def amount_for_frame(df: pd.DataFrame) -> pd.Series:
    """`amount = volume * mean(OHLC)` — KronosPredictor's documented fallback."""
    ohlc_mean = df[["open", "high", "low", "close"]].mean(axis=1)
    return (df["volume"] * ohlc_mean).astype(np.float64)


def normalize_window(x: np.ndarray, lookback: int, clip: float) -> np.ndarray:
    """Z-normalize a (L, 6) window from its lookback slice only.

    No lookahead: statistics come from `x[:lookback]`; mutating any bar at
    or after `lookback` must not change `out[:lookback]`. Clamped to
    [-clip, clip], matching the ported training loops.
    """
    past = x[:lookback]
    mean = past.mean(axis=0)
    std = past.std(axis=0)
    return np.clip((x - mean) / (std + 1e-5), -clip, clip).astype(np.float32)


def stamp_for_ordinals(ordinals: np.ndarray) -> np.ndarray:
    """(N, 5) float32 stamps `[minute=0, hour=0, weekday, day, month]`."""
    ordinals = np.asarray(ordinals, dtype=np.int32)
    out = np.zeros((len(ordinals), 5), dtype=np.float32)
    for i, o in enumerate(ordinals.tolist()):
        d = date.fromordinal(int(o))
        out[i, 2] = d.weekday()
        out[i, 3] = d.day
        out[i, 4] = d.month
    return out


def split_counts(n_pre: int, val_frac: float = DEFAULT_VAL_FRAC) -> tuple[int, int]:
    """Chronological (train, val) split of `n_pre` pre-cutoff bars."""
    n_val = int(round(n_pre * val_frac))
    return n_pre - n_val, n_val


def _frame_dates(df: pd.DataFrame) -> pd.DatetimeIndex:
    if isinstance(df.index, pd.DatetimeIndex):
        return pd.DatetimeIndex(pd.to_datetime(df.index))
    for col in ("bar_date", "date", "datetime", "ts"):
        if col in df.columns:
            return pd.DatetimeIndex(pd.to_datetime(df[col]))
    raise ValueError("kronos_dataset | frame has no DatetimeIndex or date column")


def _prepare_symbol(
    symbol: str,
    df: pd.DataFrame | None,
    market: str | None,
    *,
    cutoff: pd.Timestamp,
    min_bars: int,
    max_stale_days: int | None,
    reference_date: date | None,
) -> tuple[str, str, np.ndarray, np.ndarray, str | None]:
    """Clean one symbol frame → (symbol, market, feats, ordinals, drop_reason).

    Shared by the in-memory and streaming build paths so both apply identical
    filtering: dedupe, numeric coercion, NaN-OHLC drop, `min_bars` on the full
    frame and again on the pre-cutoff slice, optional staleness drop, then the
    cutoff split. `drop_reason` is None when the symbol is kept.
    """
    symbol = ((symbol or "").upper().strip())
    market = ((market or "us").lower())
    if not symbol or df is None or len(df) == 0:
        return symbol, market, np.empty((0, FEATURES_N), np.float32), np.empty(0, np.int32), "thin"
    work = df.copy()
    try:
        work.index = _frame_dates(work)
    except ValueError:
        return symbol, market, np.empty((0, FEATURES_N), np.float32), np.empty(0, np.int32), "thin"
    work = work.sort_index()
    work = work[~work.index.duplicated(keep="last")]
    for col in ("open", "high", "low", "close", "volume"):
        work[col] = pd.to_numeric(work[col], errors="coerce")
    work = work.dropna(subset=["open", "high", "low", "close"])
    work["volume"] = work["volume"].fillna(0.0)
    if len(work) < min_bars:
        return symbol, market, np.empty((0, FEATURES_N), np.float32), np.empty(0, np.int32), "thin"
    if max_stale_days is not None:
        ref = reference_date or date.today()
        last_day = pd.Timestamp(work.index[-1]).date()
        if (ref - last_day).days > max_stale_days:
            return symbol, market, np.empty((0, FEATURES_N), np.float32), np.empty(0, np.int32), "stale"
    pre = work[work.index < cutoff]
    if len(pre) < min_bars:
        return symbol, market, np.empty((0, FEATURES_N), np.float32), np.empty(0, np.int32), "thin"
    feats = np.empty((len(pre), FEATURES_N), dtype=np.float32)
    feats[:, 0] = pre["open"].to_numpy(dtype=np.float64)
    feats[:, 1] = pre["high"].to_numpy(dtype=np.float64)
    feats[:, 2] = pre["low"].to_numpy(dtype=np.float64)
    feats[:, 3] = pre["close"].to_numpy(dtype=np.float64)
    feats[:, 4] = pre["volume"].to_numpy(dtype=np.float64)
    feats[:, 5] = amount_for_frame(pre).to_numpy(dtype=np.float64)
    ordinals = np.array(
        [pd.Timestamp(t).date().toordinal() for t in pre.index],
        dtype=np.int32,
    )
    return symbol, market, feats, ordinals, None


def _write_corpus(
    out: Path,
    kept: list[tuple[str, str, np.ndarray, np.ndarray]],
    *,
    train_cutoff: str,
    val_frac: float,
    min_bars: int,
    max_stale_days: int | None,
    dropped_thin: int,
    dropped_stale: int,
) -> dict:
    """Write features/days/symbols/report from kept (symbol, market, feats, ords)."""
    kept = sorted(kept, key=lambda r: r[0])
    total = sum(len(r[2]) for r in kept)
    us_bars = sum(len(r[2]) for r in kept if r[1] != "ph")
    ph_bars = sum(len(r[2]) for r in kept if r[1] == "ph")
    feat_path = out / FEATURES_FILE
    days_path = out / DAYS_FILE
    feats_mm = np.memmap(str(feat_path), dtype=np.float32, mode="w+", shape=(total, FEATURES_N))
    days_mm = np.memmap(str(days_path), dtype=np.int32, mode="w+", shape=(total,))
    symbols: list[dict] = []
    offset = 0
    train_total = val_total = 0
    for symbol, market, feats, ordinals in kept:
        n = len(feats)
        feats_mm[offset:offset + n] = feats
        days_mm[offset:offset + n] = ordinals
        n_train, n_val = split_counts(n, val_frac)
        symbols.append({
            "name": symbol,
            "market": market,
            "offset": offset,
            "count": n,
            "train_count": n_train,
            "val_count": n_val,
            "val_offset": offset + n_train,
            "window_count": 0,  # filled by window_counts()
        })
        offset += n
        train_total += n_train
        val_total += n_val
    del feats_mm, days_mm

    (out / SYMBOLS_FILE).write_text(
        json.dumps(
            {"train_cutoff": pd.Timestamp(train_cutoff).date().isoformat(),
             "val_frac": val_frac, "symbols": symbols},
            indent=1,
        ),
        encoding="utf-8",
    )
    report = {
        "train_cutoff": pd.Timestamp(train_cutoff).date().isoformat(),
        "symbols_kept": len(kept),
        "symbols_dropped_thin": dropped_thin,
        "symbols_dropped_stale": dropped_stale,
        "bars_total": total,
        "bars_us": us_bars,
        "bars_ph": ph_bars,
        "bars_train": train_total,
        "bars_val": val_total,
        "val_frac": val_frac,
        "min_bars": min_bars,
        "max_stale_days": max_stale_days,
    }
    (out / REPORT_FILE).write_text(json.dumps(report, indent=1), encoding="utf-8")
    return report


def build_from_frames(
    frames: list[tuple[str, pd.DataFrame, str]],
    out_dir: str | Path,
    *,
    train_cutoff: str = DEFAULT_TRAIN_CUTOFF,
    min_bars: int = 500,
    max_stale_days: int | None = None,
    val_frac: float = DEFAULT_VAL_FRAC,
    reference_date: date | None = None,
) -> dict:
    """Build the corpus from in-memory (symbol, df, market) frames.

    `df` needs open/high/low/close/volume with a DatetimeIndex (or a
    bar_date/date/datetime/ts column). Used by unit tests and small corpora;
    the DB path (`build_dataset`) streams incrementally instead of collecting
    frames. Returns the build report dict.
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    cutoff = pd.Timestamp(train_cutoff).normalize()

    kept: list[tuple[str, str, np.ndarray, np.ndarray]] = []
    dropped_thin = 0
    dropped_stale = 0
    for symbol, df, market in frames:
        sym, mkt, feats, ordinals, reason = _prepare_symbol(
            symbol, df, market, cutoff=cutoff, min_bars=min_bars,
            max_stale_days=max_stale_days, reference_date=reference_date,
        )
        if reason == "stale":
            dropped_stale += 1
        elif reason is not None:
            dropped_thin += 1
        else:
            kept.append((sym, mkt, feats, ordinals))
    return _write_corpus(
        out, kept, train_cutoff=train_cutoff, val_frac=val_frac,
        min_bars=min_bars, max_stale_days=max_stale_days,
        dropped_thin=dropped_thin, dropped_stale=dropped_stale,
    )


def window_counts(
    dataset_dir: str | Path, lookback: int, predict_len: int,
) -> tuple[list[dict], np.ndarray]:
    """Per-symbol train window counts + cumulative offsets for O(1) sampling.

    Avoids the old `list[(symbol, i)]` index (~36M tuples ≈ 2.6–2.9 GB for
    train alone at full universe); this is ~70 KB. Read-only: `window_count`
    is refreshed only in the returned in-memory copy, never on disk.
    """
    meta = json.loads((Path(dataset_dir) / SYMBOLS_FILE).read_text(encoding="utf-8"))
    symbols = meta["symbols"]
    window = lookback + predict_len + 1
    counts = np.array(
        [max(0, s["train_count"] - window + 1) for s in symbols], dtype=np.int64
    )
    cum = np.zeros(len(counts) + 1, dtype=np.int64)
    np.cumsum(counts, out=cum[1:])
    for s, c in zip(symbols, counts.tolist()):
        s["window_count"] = int(c)
    return symbols, cum


def locate_window(
    symbols: list[dict], cum: np.ndarray, global_idx: int,
) -> tuple[dict, int]:
    """Map a flat window index → (symbol meta, start offset within train)."""
    import bisect

    pos = bisect.bisect_right(cum, global_idx) - 1
    return symbols[pos], global_idx - int(cum[pos])


def load_corpus(dataset_dir: str | Path) -> tuple[np.memmap, np.memmap, dict]:
    """Open features/days read-only plus symbols meta (no copy)."""
    d = Path(dataset_dir)
    meta = json.loads((d / SYMBOLS_FILE).read_text(encoding="utf-8"))
    total = sum(s["count"] for s in meta["symbols"])
    feats = np.memmap(str(d / FEATURES_FILE), dtype=np.float32, mode="r",
                      shape=(total, FEATURES_N))
    days = np.memmap(str(d / DAYS_FILE), dtype=np.int32, mode="r", shape=(total,))
    return feats, days, meta


def build_dataset(
    out_dir: str | Path,
    *,
    train_cutoff: str = DEFAULT_TRAIN_CUTOFF,
    min_bars: int = 500,
    max_stale_days: int | None = None,
    val_frac: float = DEFAULT_VAL_FRAC,
    reference_date: date | None = None,
) -> dict:
    """Single ordered scan of `daily_bars` → lean corpus on disk.

    Genuinely streaming: one `SELECT ... ORDER BY symbol, ts` through a
    server-side cursor, one symbol's bars in RAM at a time, accepted bars
    appended straight to the output files (no 18k-frame pile-up, no second
    pass). Requires local Postgres (the training host owns `stocks_history`).
    """
    import psycopg

    from data.db import build_dsn

    dsn = build_dsn()
    cutoff = pd.Timestamp(train_cutoff).normalize()

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    feat_path = out / FEATURES_FILE
    days_path = out / DAYS_FILE

    symbols: list[dict] = []
    offset = 0
    train_total = val_total = 0
    us_bars = ph_bars = 0
    dropped_thin = dropped_stale = 0

    conn = psycopg.connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT symbol, market FROM symbols ORDER BY symbol")
            market_of = {sym: (mkt or "us").lower() for sym, mkt in cur.fetchall()}

        with (
            open(feat_path, "wb") as f_feats,
            open(days_path, "wb") as f_days,
            conn.cursor(name="kronos_dataset_scan") as cur,
        ):
            cur.itersize = 50_000
            cur.execute(
                "SELECT symbol, ts, open, high, low, close, volume "
                "FROM daily_bars ORDER BY symbol, ts"
            )
            cur_symbol: str | None = None
            cur_rows: list[tuple] = []

            def _flush() -> None:
                nonlocal offset, train_total, val_total
                nonlocal us_bars, ph_bars, dropped_thin, dropped_stale
                if cur_symbol is None or not cur_rows:
                    return
                arr = np.array(cur_rows, dtype=np.float64)  # ts,o,h,l,c,v
                idx = pd.to_datetime(arr[:, 0], unit="s", utc=True).tz_convert(None)
                df = pd.DataFrame(
                    arr[:, 1:6], index=idx,
                    columns=["open", "high", "low", "close", "volume"],
                )
                sym, mkt, feats, ordinals, reason = _prepare_symbol(
                    cur_symbol, df, market_of.get(cur_symbol, "us"),
                    cutoff=cutoff, min_bars=min_bars,
                    max_stale_days=max_stale_days,
                    reference_date=reference_date,
                )
                if reason == "stale":
                    dropped_stale += 1
                elif reason is not None:
                    dropped_thin += 1
                else:
                    n = len(feats)
                    f_feats.write(feats.tobytes())
                    f_days.write(ordinals.tobytes())
                    n_train, n_val = split_counts(n, val_frac)
                    symbols.append({
                        "name": sym,
                        "market": mkt,
                        "offset": offset,
                        "count": n,
                        "train_count": n_train,
                        "val_count": n_val,
                        "val_offset": offset + n_train,
                        "window_count": 0,  # filled by window_counts()
                    })
                    offset += n
                    train_total += n_train
                    val_total += n_val
                    if mkt == "ph":
                        ph_bars += n
                    else:
                        us_bars += n

            for sym, ts, o, h, l, c, v in cur:
                if sym != cur_symbol:
                    _flush()
                    cur_symbol, cur_rows = sym, []
                cur_rows.append((ts, o, h, l, c, v))
            _flush()
    finally:
        conn.close()

    # `symbols` is already in DB scan order (`ORDER BY symbol`) and offsets
    # were assigned in that same order — do NOT re-sort here (Python vs
    # Postgres collation can disagree on punctuation, which would invalidate
    # the offsets).
    (out / SYMBOLS_FILE).write_text(
        json.dumps(
            {"train_cutoff": pd.Timestamp(train_cutoff).date().isoformat(),
             "val_frac": val_frac, "symbols": symbols},
            indent=1,
        ),
        encoding="utf-8",
    )
    report = {
        "train_cutoff": pd.Timestamp(train_cutoff).date().isoformat(),
        "symbols_kept": len(symbols),
        "symbols_dropped_thin": dropped_thin,
        "symbols_dropped_stale": dropped_stale,
        "bars_total": offset,
        "bars_us": us_bars,
        "bars_ph": ph_bars,
        "bars_train": train_total,
        "bars_val": val_total,
        "val_frac": val_frac,
        "min_bars": min_bars,
        "max_stale_days": max_stale_days,
    }
    (out / REPORT_FILE).write_text(json.dumps(report, indent=1), encoding="utf-8")
    return report
