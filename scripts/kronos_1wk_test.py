#!/usr/bin/env python3
"""
scripts/kronos_1wk_test.py — Walk-forward Kronos +1 week (5 trading days)
close-forecast test on /home/r00t/stocks_data, scoring windows as-of
2026-06-01 or later.

Writes raw per-window rows and summary findings as Markdown to
kronos_1_wk.md (project root by default).

Usage:
    .venv/bin/python scripts/kronos_1wk_test.py
    .venv/bin/python scripts/kronos_1wk_test.py --symbols 50 --liquid-only
    .venv/bin/python scripts/kronos_1wk_test.py --use-finetuned --sample-count 3
"""

from __future__ import annotations

import argparse
import math
import random
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.kronos_eval import (  # noqa: E402
    WEEK_AHEAD,
    WindowResult,
    _load_predictor,
    _run_symbol,
    _score,
)
from learn.dataset import DEFAULT_DATA_DIR, iter_ticker_frames  # noqa: E402
from utils.logger import log  # noqa: E402

DEFAULT_OUT = ROOT / "kronos_1_wk.md"
DEFAULT_START = "2026-06-01"


def _select_candidates(
    data_dir: Path,
    n_symbols: int,
    min_bars: int,
    start_ts: pd.Timestamp | None,
    liquid_only: bool,
    liquidity_window: int,
    seed: int,
) -> list[tuple[str, pd.DataFrame]]:
    candidates = [
        (symbol, df) for symbol, df in iter_ticker_frames(data_dir) if len(df) >= min_bars
    ]
    if start_ts is not None:
        n_before = len(candidates)
        candidates = [(s, df) for s, df in candidates if df.index[-1] >= start_ts]
        dropped = n_before - len(candidates)
        if dropped:
            log.warning(
                f"kronos-1wk | dropped {dropped} tickers with no data on/after {start_ts.date()}"
            )
    if not candidates:
        return []

    if liquid_only:
        def dollar_volume(df: pd.DataFrame) -> float:
            tail = df.tail(liquidity_window)
            return float((tail["close"] * tail["volume"]).mean())

        ranked = [(dollar_volume(df), symbol, df) for symbol, df in candidates]
        ranked = [r for r in ranked if not math.isnan(r[0])]
        ranked.sort(key=lambda r: r[0], reverse=True)
        return [(symbol, df) for _dv, symbol, df in ranked[:n_symbols]]

    rng = random.Random(seed)
    rng.shuffle(candidates)
    return candidates[:n_symbols]


def _symbol_score(results: list[WindowResult]) -> dict:
    return _score(results, "actual_1w", "pred_1w")


def _findings(results: list[WindowResult], candidates: list[tuple[str, pd.DataFrame]]) -> list[str]:
    score = _score(results, "actual_1w", "pred_1w")
    actual = np.array([r.actual_1w for r in results])
    pred = np.array([r.pred_1w for r in results])
    err = pred - actual

    lines: list[str] = []
    lines.append("## Findings")
    lines.append("")
    lines.append("Kronos +1 week (5 trading days) close % move.")
    lines.append("")
    if score["n"] == 0:
        lines.append("No windows scored.")
        return lines

    beats_naive = score["mae_pct"] < score["naive_mae_pct"]
    dir_acc = score["directional_accuracy_pct"]
    corr = float(np.corrcoef(pred, actual)[0, 1]) if len(pred) > 1 else float("nan")
    lines.append(
        f"- **Sample:** {len(candidates)} symbols, {score['n']} windows "
        f"(asof ≥ start_date, pred_len={WEEK_AHEAD})."
    )
    lines.append(
        f"- **MAE:** {score['mae_pct']:.3f}% · naive (flat 0%) MAE: "
        f"{score['naive_mae_pct']:.3f}% · "
        f"**{'beats' if beats_naive else 'loses to'}** flat baseline by "
        f"{abs(score['naive_mae_pct'] - score['mae_pct']):.3f} pp."
    )
    lines.append(
        f"- **Directional accuracy:** {dir_acc:.1f}% "
        f"({'above' if dir_acc > 50 else 'at/below'} coin-flip 50%)."
    )
    lines.append(
        f"- **Bias** (mean pred−actual): {float(np.mean(err)) * 100:.3f} pp · "
        f"**RMSE:** {float(np.sqrt(np.mean(err ** 2))) * 100:.3f}% · "
        f"**corr(pred, actual):** {corr:.3f}"
    )
    lines.append(
        f"- **Actual 1w move:** mean={float(np.mean(actual))*100:.3f}% "
        f"std={float(np.std(actual))*100:.3f}% · "
        f"**Pred 1w move:** mean={float(np.mean(pred))*100:.3f}% "
        f"std={float(np.std(pred))*100:.3f}%"
    )

    both_up = int(np.sum((pred > 0) & (actual > 0)))
    both_down = int(np.sum((pred < 0) & (actual < 0)))
    pred_up_act_down = int(np.sum((pred > 0) & (actual < 0)))
    pred_down_act_up = int(np.sum((pred < 0) & (actual > 0)))
    flatish = score["n"] - both_up - both_down - pred_up_act_down - pred_down_act_up
    lines.append(
        f"- **Sign matrix:** both+={both_up} · both-={both_down} · "
        f"pred+/act-={pred_up_act_down} · pred-/act+={pred_down_act_up} · "
        f"zero-side={flatish}"
    )
    lines.append("")

    by_sym: dict[str, list[WindowResult]] = {}
    for r in results:
        by_sym.setdefault(r.symbol, []).append(r)
    ranked = []
    for sym, rows in by_sym.items():
        s = _symbol_score(rows)
        if s["n"] >= 1:
            ranked.append((s["directional_accuracy_pct"], s["mae_pct"], s["n"], sym))
    ranked.sort(reverse=True)

    lines.append("### Per-symbol (dir_acc desc)")
    lines.append("")
    lines.append("| symbol | n | dir_acc% | mae% |")
    lines.append("| --- | ---: | ---: | ---: |")
    for dir_acc_s, mae_s, n_s, sym in ranked:
        lines.append(f"| {sym} | {n_s} | {dir_acc_s:.1f} | {mae_s:.3f} |")
    lines.append("")

    if beats_naive and dir_acc > 50:
        verdict = (
            "Kronos 1w forecast shows modest skill vs flat baseline on this "
            "Jun–Jul 2026 window (lower MAE and >50% direction). Treat as regime-specific; "
            "re-check before trusting the live gate."
        )
    elif beats_naive:
        verdict = (
            "Magnitude error beats flat baseline, but direction is not reliably "
            "above chance — weak confirmation signal for a BUY/SELL gate."
        )
    elif dir_acc > 50:
        verdict = (
            "Direction slightly better than chance, but MAE loses to flat — "
            "size of predicted moves is unreliable; gate should not size from forecast magnitude."
        )
    else:
        verdict = (
            "No clear edge vs flat 0% on this sample (MAE and/or direction). "
            "Do not strengthen kronos_gate reliance on this alone."
        )
    lines.append(f"**Verdict:** {verdict}")
    return lines


def write_report(
    out_path: Path,
    *,
    cfg: dict,
    candidates: list[tuple[str, pd.DataFrame]],
    results: list[WindowResult],
) -> None:
    score_1w = _score(results, "actual_1w", "pred_1w")
    score_1d = _score(results, "actual_1d", "pred_1d")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    lines: list[str] = []
    lines.append("# Kronos +1 week ahead forecast test")
    lines.append("")
    lines.append(f"_Generated: {now}_")
    lines.append("")
    lines.append("## Config")
    lines.append("")
    lines.append("| key | value |")
    lines.append("| --- | --- |")
    for k, v in cfg.items():
        lines.append(f"| `{k}` | {v} |")
    lines.append("")

    lines.append("## Summary")
    lines.append("")
    lines.append("+1 week primary; +1 day also recorded per window.")
    lines.append("")
    if score_1w["n"]:
        lines.append("| horizon | n | MAE% | naive MAE% | dir_hit% |")
        lines.append("| --- | ---: | ---: | ---: | ---: |")
        lines.append(
            f"| +1 week | {score_1w['n']} | {score_1w['mae_pct']:.3f} | "
            f"{score_1w['naive_mae_pct']:.3f} | {score_1w['directional_accuracy_pct']:.1f} |"
        )
        lines.append(
            f"| +1 day | {score_1d['n']} | {score_1d['mae_pct']:.3f} | "
            f"{score_1d['naive_mae_pct']:.3f} | {score_1d['directional_accuracy_pct']:.1f} |"
        )
    else:
        lines.append("_No windows._")
    lines.append("")

    lines.extend(_findings(results, candidates))
    lines.append("")

    lines.append("## Raw window data")
    lines.append("")
    lines.append(
        "| symbol | asof | actual_1d_pct | pred_1d_pct | actual_1w_pct | "
        "pred_1w_pct | err_1w_pct | dir_hit_1w |"
    )
    lines.append("| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |")
    for r in sorted(results, key=lambda x: (x.symbol, x.asof)):
        err_1w = (r.pred_1w - r.actual_1w) * 100
        hit = int(np.sign(r.pred_1w) == np.sign(r.actual_1w))
        lines.append(
            f"| {r.symbol} | {r.asof.date()} | "
            f"{r.actual_1d * 100:.6f} | {r.pred_1d * 100:.6f} | "
            f"{r.actual_1w * 100:.6f} | {r.pred_1w * 100:.6f} | "
            f"{err_1w:.6f} | {hit} |"
        )
    lines.append("")
    lines.append(
        f"_Notes:_ `*_pct` columns are close-to-close % moves from asof close. "
        f"1w = {WEEK_AHEAD} trading days. `dir_hit_1w=1` if sign(pred)==sign(actual). "
        "MAE = mean |pred − actual|. Naive MAE = mean |actual| (predict 0% move)."
    )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log.info(f"kronos-1wk | wrote {out_path} ({len(results)} windows)")


def run(
    data_dir: Path = DEFAULT_DATA_DIR,
    n_symbols: int = 30,
    windows_per_symbol: int = 8,
    lookback: int = 400,
    stride: int = 5,
    min_bars: int = 500,
    seed: int = 42,
    device: str | None = None,
    liquid_only: bool = True,
    liquidity_window: int = 60,
    sample_count: int = 3,
    start_date: str = DEFAULT_START,
    use_finetuned: bool = False,
    out_path: Path = DEFAULT_OUT,
) -> Path:
    if device is None:
        import torch

        device = "cuda" if torch.cuda.is_available() else "cpu"

    start_ts = pd.Timestamp(start_date) if start_date else None
    log.info(
        f"kronos-1wk | data_dir={data_dir} symbols={n_symbols} device={device} "
        f"windows/symbol={windows_per_symbol} lookback={lookback} stride={stride} "
        f"liquid_only={liquid_only} sample_count={sample_count} "
        f"start_date={start_date} use_finetuned={use_finetuned} out={out_path}"
    )

    candidates = _select_candidates(
        data_dir, n_symbols, min_bars, start_ts, liquid_only, liquidity_window, seed
    )
    if not candidates:
        raise SystemExit("kronos-1wk | no eligible tickers — check data_dir / start_date")

    log.info(f"kronos-1wk | evaluating {len(candidates)} symbols")
    predictor = _load_predictor(device=device, use_finetuned=use_finetuned)

    all_results: list[WindowResult] = []
    for symbol, df in candidates:
        try:
            results = _run_symbol(
                predictor,
                symbol,
                df,
                lookback,
                windows_per_symbol,
                stride,
                sample_count,
                start_ts,
            )
        except Exception as exc:
            log.warning(f"kronos-1wk | {symbol}: skipped ({exc})")
            continue
        all_results.extend(results)
        log.info(f"kronos-1wk | {symbol}: {len(results)} windows")

    if not all_results:
        raise SystemExit("kronos-1wk | no windows evaluated")

    cfg = {
        "data_dir": str(data_dir),
        "start_date": start_date,
        "horizon": f"{WEEK_AHEAD} trading days (+1 week)",
        "n_symbols_requested": n_symbols,
        "n_symbols_evaluated": len(candidates),
        "symbols": ",".join(s for s, _ in candidates),
        "windows_per_symbol": windows_per_symbol,
        "lookback": lookback,
        "stride": stride,
        "min_bars": min_bars,
        "liquid_only": liquid_only,
        "liquidity_window": liquidity_window,
        "sample_count": sample_count,
        "use_finetuned": use_finetuned,
        "device": device,
        "seed": seed,
        "n_windows": len(all_results),
    }
    write_report(out_path, cfg=cfg, candidates=candidates, results=all_results)

    score = _score(all_results, "actual_1w", "pred_1w")
    print()
    print("=" * 60)
    print(f"  Kronos +1 week — {len(candidates)} symbols, {len(all_results)} windows")
    print(
        f"  MAE={score['mae_pct']:.3f}%  naive={score['naive_mae_pct']:.3f}%  "
        f"dir_hit={score['directional_accuracy_pct']:.1f}%"
    )
    print(f"  Wrote {out_path}")
    print("=" * 60)
    return out_path


def main() -> None:
    p = argparse.ArgumentParser(description="Kronos +1 week ahead forecast test → kronos_1_wk.md")
    p.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    p.add_argument("--symbols", type=int, default=30, help="Number of tickers to evaluate")
    p.add_argument("--windows", type=int, default=8, help="Max walk-forward windows per symbol")
    p.add_argument("--lookback", type=int, default=400)
    p.add_argument("--stride", type=int, default=5, help="Bars between windows (smaller = more)")
    p.add_argument("--min-bars", type=int, default=500)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--device", type=str, default=None)
    p.add_argument("--liquid-only", action=argparse.BooleanOptionalAction, default=True)
    p.add_argument("--sample-count", type=int, default=3)
    p.add_argument("--start-date", type=str, default=DEFAULT_START)
    p.add_argument("--use-finetuned", action="store_true")
    p.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = p.parse_args()

    # Keep cwd-relative --out paths under project root when launched from elsewhere.
    out = args.out if args.out.is_absolute() else ROOT / args.out
    run(
        data_dir=args.data_dir,
        n_symbols=args.symbols,
        windows_per_symbol=args.windows,
        lookback=args.lookback,
        stride=args.stride,
        min_bars=args.min_bars,
        seed=args.seed,
        device=args.device,
        liquid_only=args.liquid_only,
        sample_count=args.sample_count,
        start_date=args.start_date,
        use_finetuned=args.use_finetuned,
        out_path=out,
    )


if __name__ == "__main__":
    main()
