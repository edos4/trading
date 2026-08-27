#!/usr/bin/env python3
"""
scripts/kronos_1wk_test.py — Walk-forward Kronos +3 trading-day
close-forecast test on stocks_history, scored for this project's
`kronos_gate` (direction + |pred| >= 3% in 3 days), not just raw MAE.

Writes raw per-window rows and summary findings as Markdown to
kronos_1_wk.md (project root by default). Filename is historical.

What this validates:
  - Unconditional forecast skill vs flat / prior-3d persistence / majority-sign
  - Gate-filtered skill (same 3% / 3d floor as live confirm/veto)
  - Bootstrap CIs (windows are correlated — treat intervals as soft)

What this does NOT validate:
  - Pattern-conditional gate lift (run formal backtest A/B with kronos_gate
    on/off for that). This script has no pattern detections.

Usage:
    .venv/bin/python scripts/kronos_1wk_test.py
    .venv/bin/python scripts/kronos_1wk_test.py --symbols 50 --liquid-only
    .venv/bin/python scripts/kronos_1wk_test.py --use-finetuned --sample-count 3
    .venv/bin/python scripts/kronos_1wk_test.py --min-move 0.06
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

from config import settings  # noqa: E402
from core.kronos_eval import (  # noqa: E402
    LOOKBACK,
    WEEK_AHEAD,
    WindowResult,
    _load_predictor,
    _run_symbol,
    _score,
    _score_persistence,
    bootstrap_ci,
    majority_sign_baseline,
    score_gate_rule,
)
from learn.dataset import iter_ticker_frames  # noqa: E402
from utils.logger import log  # noqa: E402

DEFAULT_OUT = ROOT / "kronos_1_wk.md"
DEFAULT_START = "2026-06-01"


def _select_candidates(
    n_symbols: int,
    min_bars: int,
    start_ts: pd.Timestamp | None,
    liquid_only: bool,
    liquidity_window: int,
    seed: int,
) -> list[tuple[str, pd.DataFrame]]:
    candidates = [
        (symbol, df) for symbol, df in iter_ticker_frames(min_bars=min_bars)
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


def _fmt_ci(ci: tuple[float, float, float], digits: int = 1) -> str:
    lo, mid, hi = ci
    if any(math.isnan(x) for x in (lo, mid, hi)):
        return "n/a"
    return f"{mid:.{digits}f} [{lo:.{digits}f}, {hi:.{digits}f}]"


def _findings(
    results: list[WindowResult],
    candidates: list[tuple[str, pd.DataFrame]],
    *,
    min_move: float,
    n_boot: int,
    seed: int,
) -> list[str]:
    score = _score(results, "actual_1w", "pred_1w")
    persist = _score_persistence(results)
    maj = majority_sign_baseline(results)
    gate = score_gate_rule(results, min_move)
    ci_all = bootstrap_ci(results, n_boot=n_boot, seed=seed)
    ci_gate = bootstrap_ci(results, n_boot=n_boot, seed=seed, min_move=min_move)

    actual = np.array([r.actual_1w for r in results])
    pred = np.array([r.pred_1w for r in results])
    err = pred - actual

    lines: list[str] = []
    lines.append("## Findings")
    lines.append("")
    lines.append(
        "Primary question for this repo: does Kronos 3d forecast skill survive "
        f"the live gate rule (`|pred_3d| ≥ {min_move:.0%}` + sign), vs weak "
        "baselines? Pattern-conditional A/B is out of scope here."
    )
    lines.append("")
    if score["n"] == 0:
        lines.append("No windows scored.")
        return lines

    # ── Unconditional ──────────────────────────────────────────────────────
    beats_flat = score["mae_pct"] < score["naive_mae_pct"]
    beats_persist_mae = (
        persist.get("n", 0) > 0 and score["mae_pct"] < persist["mae_pct"]
    )
    beats_persist_dir = (
        persist.get("n", 0) > 0
        and score["directional_accuracy_pct"] > persist["directional_accuracy_pct"]
    )
    dir_acc = score["directional_accuracy_pct"]
    corr = float(np.corrcoef(pred, actual)[0, 1]) if len(pred) > 1 else float("nan")
    unique_asof = len({pd.Timestamp(r.asof).normalize() for r in results})

    lines.append("### Unconditional forecast (all windows)")
    lines.append("")
    lines.append(
        f"- **Sample:** {len(candidates)} symbols, {score['n']} windows, "
        f"{unique_asof} distinct asof dates (stride-overlapped context — "
        f"effective n ≪ {score['n']}; CIs are soft)."
    )
    lines.append(
        f"- **MAE:** {score['mae_pct']:.3f}% · flat-0 MAE: {score['naive_mae_pct']:.3f}% · "
        f"**{'beats' if beats_flat else 'loses to'}** flat by "
        f"{abs(score['naive_mae_pct'] - score['mae_pct']):.3f} pp."
    )
    if persist.get("n"):
        lines.append(
            f"- **vs persistence** (prior {WEEK_AHEAD}d return): "
            f"persist MAE={persist['mae_pct']:.3f}% dir={persist['directional_accuracy_pct']:.1f}% · "
            f"Kronos MAE **{'better' if beats_persist_mae else 'worse'}**, "
            f"dir **{'better' if beats_persist_dir else 'worse/equal'}**."
        )
    if maj.get("n"):
        lines.append(
            f"- **vs majority-sign** (always "
            f"{'up' if maj['majority_sign'] > 0 else 'down'} on this tape): "
            f"dir={maj['directional_accuracy_pct']:.1f}% — if Kronos dir is not "
            f"clearly above this, apparent edge may be regime bias."
        )
    lines.append(
        f"- **Directional accuracy:** {dir_acc:.1f}% "
        f"(bootstrap median+95% CI: {_fmt_ci(ci_all['dir_acc_pct_ci'])}%) · "
        f"coin-flip reference 50%."
    )
    lines.append(
        f"- **MAE bootstrap CI:** {_fmt_ci(ci_all['mae_pct_ci'], 3)}%"
    )
    lines.append(
        f"- **Signed return** (always trade pred sign, 3d hold): "
        f"{score['signed_return_pct']:.3f}% mean per window."
    )
    lines.append(
        f"- **Bias** (mean pred−actual): {float(np.mean(err)) * 100:.3f} pp · "
        f"**RMSE:** {float(np.sqrt(np.mean(err ** 2))) * 100:.3f}% · "
        f"**corr(pred, actual):** {corr:.3f}"
    )
    lines.append(
        f"- **Actual 3d move:** mean={float(np.mean(actual))*100:.3f}% "
        f"std={float(np.std(actual))*100:.3f}% · "
        f"**Pred 3d move:** mean={float(np.mean(pred))*100:.3f}% "
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

    # ── Gate-filtered (project-relevant) ───────────────────────────────────
    lines.append(f"### Gate-filtered (`|pred_1w| ≥ {min_move:.0%}`, live kronos_gate)")
    lines.append("")
    if not gate.get("n"):
        lines.append(
            f"- No windows cleared min_move={min_move:.0%}. "
            "Gate would veto everything on this sample (or min_move is too high)."
        )
    else:
        lines.append(
            f"- **Coverage:** {gate['n']}/{gate['n_all']} windows "
            f"({gate['coverage_pct']:.1f}%) would clear the magnitude floor."
        )
        lines.append(
            f"- **Dir accuracy (gate slice):** {gate['directional_accuracy_pct']:.1f}% "
            f"(bootstrap: {_fmt_ci(ci_gate['dir_acc_pct_ci'])}%)."
        )
        lines.append(
            f"- **MAE (gate slice):** {gate['mae_pct']:.3f}% · "
            f"flat-0 on same slice: {gate['naive_mae_pct']:.3f}% · "
            f"bootstrap MAE CI: {_fmt_ci(ci_gate['mae_pct_ci'], 3)}%."
        )
        lines.append(
            f"- **Signed return (gate slice):** {gate['signed_return_pct']:.3f}% "
            f"mean per cleared window — proxy for confirm-and-hold-3d expectancy."
        )
        buy_s = (
            f"{gate['buy_dir_hit_pct']:.1f}%"
            if not math.isnan(gate.get("buy_dir_hit_pct", float("nan")))
            else "n/a"
        )
        sell_s = (
            f"{gate['sell_dir_hit_pct']:.1f}%"
            if not math.isnan(gate.get("sell_dir_hit_pct", float("nan")))
            else "n/a"
        )
        lines.append(
            f"- **By side:** pred-BUY n={gate['n_buy']} hit={buy_s} · "
            f"pred-SELL n={gate['n_sell']} hit={sell_s}."
        )
    lines.append("")

    by_sym: dict[str, list[WindowResult]] = {}
    for r in results:
        by_sym.setdefault(r.symbol, []).append(r)
    ranked = []
    for sym, rows in by_sym.items():
        s = _score(rows, "actual_1w", "pred_1w")
        g = score_gate_rule(rows, min_move)
        if s["n"] >= 1:
            ranked.append(
                (
                    s["directional_accuracy_pct"],
                    g.get("directional_accuracy_pct", float("nan")),
                    g.get("n", 0),
                    s["mae_pct"],
                    s["n"],
                    sym,
                )
            )
    ranked.sort(reverse=True)

    lines.append("### Per-symbol (uncond dir_acc desc)")
    lines.append("")
    lines.append("| symbol | n | dir% | mae% | gate_n | gate_dir% |")
    lines.append("| --- | ---: | ---: | ---: | ---: | ---: |")
    for dir_u, dir_g, n_g, mae_s, n_s, sym in ranked:
        gdir = f"{dir_g:.1f}" if n_g and not math.isnan(dir_g) else "—"
        lines.append(
            f"| {sym} | {n_s} | {dir_u:.1f} | {mae_s:.3f} | {n_g} | {gdir} |"
        )
    lines.append("")

    # Verdict prioritizes gate slice + baselines (what this project needs).
    gate_dir = gate.get("directional_accuracy_pct", float("nan"))
    gate_ok = (
        gate.get("n", 0) >= 20
        and not math.isnan(gate_dir)
        and gate_dir > 55
        and gate.get("signed_return_pct", 0) > 0
    )
    maj_dir = maj.get("directional_accuracy_pct", 50.0)
    clears_bias = (not math.isnan(gate_dir)) and gate_dir > maj_dir + 2.0

    if gate.get("n", 0) == 0:
        verdict = (
            "No gate-cleared windows — cannot endorse kronos_gate from this run. "
            "Lower KRONOS_MIN_MOVE_PCT or widen the date/symbol sample."
        )
    elif gate_ok and clears_bias and (beats_persist_dir or beats_persist_mae):
        verdict = (
            f"Gate slice looks usable on this window: dir={gate_dir:.1f}% with "
            f"positive signed return, above majority-sign bias, and competitive "
            f"vs persistence. Still re-check with pattern backtest A/B "
            f"(kronos_gate on/off) before trusting live vetoes."
        )
    elif gate_ok:
        verdict = (
            f"Gate-filtered direction ({gate_dir:.1f}%) and signed return look "
            f"OK, but edge vs persistence/majority-sign is thin or absent — "
            f"treat as weak confirm only; do not size from |pred_1w|."
        )
    elif not math.isnan(gate_dir) and gate_dir > 50 and beats_flat:
        verdict = (
            "Mixed: some directional lean after the magnitude floor, but not "
            "strong enough vs baselines / sample size to strengthen kronos_gate. "
            "Keep gate conservative or disable until pattern A/B shows lift."
        )
    else:
        verdict = (
            "No clear gate-relevant edge on this sample (dir, signed return, "
            "and/or baselines). Do not strengthen kronos_gate reliance on this "
            "alone; prefer backtest A/B."
        )
    lines.append(f"**Verdict:** {verdict}")
    lines.append("")
    lines.append(
        "_Caveat:_ this is still unconditional on chart patterns. Live gate only "
        "fires after a Toby pattern — run formal BT with `kronos_gate` toggled "
        "for decision quality."
    )
    return lines


def write_report(
    out_path: Path,
    *,
    cfg: dict,
    candidates: list[tuple[str, pd.DataFrame]],
    results: list[WindowResult],
    min_move: float,
    n_boot: int,
    seed: int,
) -> None:
    score_1w = _score(results, "actual_1w", "pred_1w")
    score_1d = _score(results, "actual_1d", "pred_1d")
    persist = _score_persistence(results)
    gate = score_gate_rule(results, min_move)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    lines: list[str] = []
    lines.append("# Kronos +3 trading-day ahead forecast test")
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
    lines.append(
        "+3 trading days primary (live gate horizon). Gate row uses the same "
        f"`|pred| ≥ {min_move:.0%}` floor as `core/kronos_gate.py`."
    )
    lines.append("")
    if score_1w["n"]:
        lines.append(
            "| slice | n | MAE% | flat MAE% | dir_hit% | signed_ret% |"
        )
        lines.append("| --- | ---: | ---: | ---: | ---: | ---: |")
        lines.append(
            f"| +3 days (all) | {score_1w['n']} | {score_1w['mae_pct']:.3f} | "
            f"{score_1w['naive_mae_pct']:.3f} | {score_1w['directional_accuracy_pct']:.1f} | "
            f"{score_1w['signed_return_pct']:.3f} |"
        )
        lines.append(
            f"| +1 day (all) | {score_1d['n']} | {score_1d['mae_pct']:.3f} | "
            f"{score_1d['naive_mae_pct']:.3f} | {score_1d['directional_accuracy_pct']:.1f} | "
            f"{score_1d['signed_return_pct']:.3f} |"
        )
        if persist.get("n"):
            lines.append(
                f"| persistence 3d | {persist['n']} | {persist['mae_pct']:.3f} | "
                f"{persist['naive_mae_pct']:.3f} | {persist['directional_accuracy_pct']:.1f} | "
                f"{persist['signed_return_pct']:.3f} |"
            )
        if gate.get("n"):
            lines.append(
                f"| gate @{min_move:.0%} | {gate['n']} | {gate['mae_pct']:.3f} | "
                f"{gate['naive_mae_pct']:.3f} | {gate['directional_accuracy_pct']:.1f} | "
                f"{gate['signed_return_pct']:.3f} |"
            )
        else:
            lines.append(f"| gate @{min_move:.0%} | 0 | — | — | — | — |")
    else:
        lines.append("_No windows._")
    lines.append("")

    lines.extend(
        _findings(
            results, candidates, min_move=min_move, n_boot=n_boot, seed=seed,
        )
    )
    lines.append("")

    lines.append("## Raw window data")
    lines.append("")
    lines.append(
        "| symbol | asof | actual_1d_pct | pred_1d_pct | actual_1w_pct | "
        "pred_1w_pct | persist_1w_pct | err_1w_pct | dir_hit_1w | gate_clear |"
    )
    lines.append(
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"
    )
    for r in sorted(results, key=lambda x: (x.symbol, x.asof)):
        err_1w = (r.pred_1w - r.actual_1w) * 100
        hit = int(np.sign(r.pred_1w) == np.sign(r.actual_1w))
        clear = int(abs(r.pred_1w) >= min_move)
        persist_s = (
            f"{r.persist_1w * 100:.6f}"
            if not math.isnan(r.persist_1w)
            else ""
        )
        lines.append(
            f"| {r.symbol} | {r.asof.date()} | "
            f"{r.actual_1d * 100:.6f} | {r.pred_1d * 100:.6f} | "
            f"{r.actual_1w * 100:.6f} | {r.pred_1w * 100:.6f} | "
            f"{persist_s} | {err_1w:.6f} | {hit} | {clear} |"
        )
    lines.append("")
    lines.append(
        f"_Notes:_ `*_pct` = close-to-close % from asof. Horizon = {WEEK_AHEAD} trading days (not 1 calendar week). "
        f"`persist_1w` = prior {WEEK_AHEAD}d return ending at asof. "
        f"`gate_clear=1` if `|pred_1w| ≥ {min_move:.0%}` (live floor). "
        "MAE = mean |pred − actual|. Flat MAE = mean |actual|. "
        "Signed return = mean(sign(pred) × actual)."
    )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log.info(f"kronos-1wk | wrote {out_path} ({len(results)} windows)")


def run(
    n_symbols: int = 30,
    windows_per_symbol: int = 8,
    lookback: int = LOOKBACK,
    stride: int = 5,
    min_bars: int = 500,
    seed: int = 42,
    device: str | None = None,
    liquid_only: bool = True,
    liquidity_window: int = 60,
    sample_count: int | None = None,
    start_date: str = DEFAULT_START,
    use_finetuned: bool = False,
    min_move: float | None = None,
    n_boot: int = 1000,
    out_path: Path = DEFAULT_OUT,
) -> Path:
    if device is None:
        import torch

        device = "cuda" if torch.cuda.is_available() else "cpu"

    if sample_count is None:
        sample_count = settings.kronos_sample_count
    if min_move is None:
        min_move = settings.kronos_min_move_pct

    if use_finetuned:
        log.warning(
            "kronos-1wk | --use-finetuned: ensure finetune train window does NOT "
            "overlap this start_date…asof range, or scores are contaminated."
        )

    start_ts = pd.Timestamp(start_date) if start_date else None
    log.info(
        f"kronos-1wk | stocks_history symbols={n_symbols} device={device} "
        f"windows/symbol={windows_per_symbol} lookback={lookback} stride={stride} "
        f"liquid_only={liquid_only} sample_count={sample_count} "
        f"start_date={start_date} use_finetuned={use_finetuned} "
        f"min_move={min_move} n_boot={n_boot} out={out_path}"
    )

    candidates = _select_candidates(
        n_symbols, min_bars, start_ts, liquid_only, liquidity_window, seed
    )
    if not candidates:
        raise SystemExit("kronos-1wk | no eligible tickers — check stocks_history / start_date")

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
        "source": "stocks_history",
        "start_date": start_date,
        "horizon": f"{WEEK_AHEAD} trading days (3d gate, not 1 calendar week)",
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
        "min_move_pct": min_move,
        "n_boot": n_boot,
        "device": device,
        "seed": seed,
        "n_windows": len(all_results),
        "note": (
            "Unconditional walk-forward + gate-filtered metrics; "
            "not pattern-conditional. Use BT kronos_gate A/B for that."
        ),
    }
    write_report(
        out_path,
        cfg=cfg,
        candidates=candidates,
        results=all_results,
        min_move=min_move,
        n_boot=n_boot,
        seed=seed,
    )

    score = _score(all_results, "actual_1w", "pred_1w")
    gate = score_gate_rule(all_results, min_move)
    print()
    print("=" * 60)
    print(f"  Kronos +3 trading days — {len(candidates)} symbols, {len(all_results)} windows")
    print(
        f"  all:  MAE={score['mae_pct']:.3f}%  flat={score['naive_mae_pct']:.3f}%  "
        f"dir={score['directional_accuracy_pct']:.1f}%  "
        f"signed={score['signed_return_pct']:.3f}%"
    )
    if gate.get("n"):
        print(
            f"  gate@{min_move:.0%}: n={gate['n']} cover={gate['coverage_pct']:.0f}%  "
            f"MAE={gate['mae_pct']:.3f}%  dir={gate['directional_accuracy_pct']:.1f}%  "
            f"signed={gate['signed_return_pct']:.3f}%"
        )
    else:
        print(f"  gate@{min_move:.0%}: n=0 (nothing cleared magnitude floor)")
    print(f"  Wrote {out_path}")
    print("=" * 60)
    return out_path


def main() -> None:
    p = argparse.ArgumentParser(
        description=(
            "Kronos +3 trading-day forecast test with gate-filtered metrics → kronos_1_wk.md"
        )
    )
    p.add_argument("--symbols", type=int, default=30, help="Number of tickers to evaluate")
    p.add_argument("--windows", type=int, default=8, help="Max walk-forward windows per symbol")
    p.add_argument("--lookback", type=int, default=LOOKBACK)
    p.add_argument("--stride", type=int, default=5, help="Bars between windows (smaller = more)")
    p.add_argument("--min-bars", type=int, default=500)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--device", type=str, default=None)
    p.add_argument("--liquid-only", action=argparse.BooleanOptionalAction, default=True)
    p.add_argument(
        "--sample-count",
        type=int,
        default=None,
        help=f"Kronos samples to average (default: settings={settings.kronos_sample_count})",
    )
    p.add_argument("--start-date", type=str, default=DEFAULT_START)
    p.add_argument("--use-finetuned", action="store_true")
    p.add_argument(
        "--min-move",
        type=float,
        default=None,
        help=(
            "Gate magnitude floor as fraction "
            f"(default: settings.kronos_min_move_pct={settings.kronos_min_move_pct})"
        ),
    )
    p.add_argument("--n-boot", type=int, default=1000, help="Bootstrap resamples for CIs")
    p.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = p.parse_args()

    # Keep cwd-relative --out paths under project root when launched from elsewhere.
    out = args.out if args.out.is_absolute() else ROOT / args.out
    run(
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
        min_move=args.min_move,
        n_boot=args.n_boot,
        out_path=out,
    )


if __name__ == "__main__":
    main()
