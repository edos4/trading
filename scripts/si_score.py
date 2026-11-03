"""Deterministic scorer for the self-improving paper-trading loop (si.sh).

This file is frozen by ``si.sh`` (hash-pinned in ``runs/.si_manifest``): the
improvement stage may not modify it, so a run cannot "win" by redefining the
metric. Everything here is derived from the paper export (``pt.json`` shape)
alone, with no network access and no repo state beyond the file it is given.

Subcommands:
    score               Compute the score block for one export.
    assert-patternsv2   Exit 1 unless the book ran with Patternsv2 on.
    valid               Exit 0 when the score is usable for a verdict.
    compare             Exit 0 when a new score beats a reference score.
    best                Print the best accepted score under runs/accepted/.
    leaderboard         Append one row to runs/leaderboard.md.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCORE_VERSION = 1
DEFAULT_MARKET = "us"
DEFAULT_MIN_CLOSED = 10
TOL = 1e-9
MIN_COUNT_RATIO = 0.75
MAX_COUNT_RATIO = 1.25


def _num(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(out):
        return None
    return out


def _round(value: Any, digits: int = 4) -> Any:
    number = _num(value)
    if number is None:
        return None
    if math.isinf(number):
        return "inf" if number > 0 else "-inf"
    return round(number, digits)


def load_export(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def load_book(export: dict[str, Any], market: str) -> dict[str, Any]:
    for book in export.get("books") or []:
        if book.get("market") == market:
            return book
    raise ValueError(f"export has no book for market {market!r}")


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _trade_r(row: dict[str, Any]) -> float:
    r = _num(row.get("r"))
    if r is not None:
        return r
    pnl = _num(row.get("pnl_pct"))
    return 0.0 if pnl is None else pnl / 100.0


def _profit_factor(trades: list[dict[str, Any]]) -> float | None:
    gains = [_num(t.get("pnl")) or 0.0 for t in trades]
    gross_win = sum(x for x in gains if x > 0)
    gross_loss = -sum(x for x in gains if x < 0)
    if gross_loss > 0:
        return gross_win / gross_loss
    return None if gross_win > 0 else 0.0


def score_export(
    export: dict[str, Any],
    *,
    market: str = DEFAULT_MARKET,
    min_closed: int = DEFAULT_MIN_CLOSED,
) -> dict[str, Any]:
    """The evaluation block: outcomes only, no code knowledge required."""
    book = load_book(export, market)
    settings = book.get("settings") or {}
    trades = list(book.get("closed_trades") or [])

    rs = [_trade_r(row) for row in trades]
    wins = sum(1 for value in rs if value > 0)
    n = len(trades)
    exit_mix = Counter(str(t.get("exit_reason") or "unknown") for t in trades)
    by_pattern: dict[str, dict[str, Any]] = {}
    by_side: dict[str, dict[str, Any]] = {}
    for row, value in zip(trades, rs):
        pattern = str(row.get("pattern") or "unknown")
        side = str(row.get("action") or "?")
        pnl = _num(row.get("pnl")) or 0.0
        for bucket, key in ((by_pattern, pattern), (by_side, side)):
            entry = bucket.setdefault(
                key, {"n": 0, "wins": 0, "total_r": 0.0, "total_pnl": 0.0}
            )
            entry["n"] += 1
            entry["wins"] += 1 if value > 0 else 0
            entry["total_r"] += value
            entry["total_pnl"] += pnl
    for bucket in (by_pattern, by_side):
        for entry in bucket.values():
            entry["win_rate_pct"] = _round(100.0 * entry["wins"] / entry["n"])
            entry["avg_r"] = _round(entry["total_r"] / entry["n"])
            entry["total_r"] = _round(entry["total_r"])
            entry["total_pnl"] = _round(entry["total_pnl"], 2)

    sims = sorted(
        str(t.get("sim_closed")) for t in trades if t.get("sim_closed")
    )
    patterns_v2 = bool(settings.get("patterns_v2"))
    closed_count = book.get("closed_count")
    profit_factor = _profit_factor(trades)
    score = {
        "score_version": SCORE_VERSION,
        "market": market,
        "patterns_v2": patterns_v2,
        "min_closed": int(min_closed),
        "closed_count": int(closed_count) if closed_count is not None else n,
        "trades": n,
        "wins": wins,
        "win_rate_pct": _round(100.0 * wins / n if n else 0.0),
        "profit_factor": _round(profit_factor),
        "expectancy_r": _round(_mean(rs)),
        "avg_hold_bars": _round(
            _mean([_num(t.get("hold_bars")) or 0.0 for t in trades])
        ),
        "total_pnl": _round(sum(_num(t.get("pnl")) or 0.0 for t in trades), 2),
        "exit_reason_mix": dict(sorted(exit_mix.items())),
        "by_pattern": dict(sorted(by_pattern.items())),
        "by_side": dict(sorted(by_side.items())),
        "sim_window": {"first": sims[0] if sims else None, "last": sims[-1] if sims else None},
        "export_exported_at": export.get("exported_at"),
        "export_filter": export.get("filter"),
        "valid": bool(patterns_v2) and n >= int(min_closed),
        "scored_at": datetime.now(timezone.utc).isoformat(),
    }
    return score


def _pf_sort(value: Any) -> float:
    number = _num(value)
    return math.inf if number is None else number


def compare_scores(new: dict[str, Any], reference: dict[str, Any]) -> dict[str, Any]:
    """Improvement rule, fixed in code so it cannot be argued with.

    win_rate_pct >= reference AND profit_factor >= reference AND the trade
    count stays within +/-25% of the reference (win rate by trading less is not
    an edge).
    """
    reasons: list[str] = []
    if not new.get("valid"):
        reasons.append(
            "new run is INVALID: "
            f"patterns_v2={new.get('patterns_v2')} trades={new.get('trades')} "
            f"min_closed={new.get('min_closed')}"
        )
        return {"improved": False, "reasons": reasons}
    if not reference.get("valid"):
        reasons.append("reference run is INVALID; nothing legitimate to beat")
        return {"improved": False, "reasons": reasons}

    n_new = int(new.get("trades") or 0)
    n_ref = int(reference.get("trades") or 0)
    if n_ref and not (
        MIN_COUNT_RATIO * n_ref <= n_new <= MAX_COUNT_RATIO * n_ref
    ):
        reasons.append(
            f"trade count {n_new} outside {MIN_COUNT_RATIO:.2f}-{MAX_COUNT_RATIO:.2f}"
            f"x reference {n_ref}"
        )
    wr_new = _num(new.get("win_rate_pct")) or 0.0
    wr_ref = _num(reference.get("win_rate_pct")) or 0.0
    if wr_new + TOL < wr_ref:
        reasons.append(f"win rate {wr_new:.2f}% < reference {wr_ref:.2f}%")
    pf_new = _pf_sort(new.get("profit_factor"))
    pf_ref = _pf_sort(reference.get("profit_factor"))
    if pf_new + TOL < pf_ref:
        reasons.append(f"profit factor {pf_new:.4f} < reference {pf_ref:.4f}")
    strictly_better = (
        wr_new > wr_ref + TOL or pf_new > pf_ref + TOL
    )
    if not strictly_better:
        reasons.append("no metric strictly improved (equal is not an improvement)")
    return {
        "improved": not reasons,
        "reasons": reasons,
        "new": {
            "win_rate_pct": wr_new,
            "trades": n_new,
            "profit_factor": new.get("profit_factor"),
            "expectancy_r": new.get("expectancy_r"),
        },
        "reference": {
            "win_rate_pct": wr_ref,
            "trades": n_ref,
            "profit_factor": reference.get("profit_factor"),
            "expectancy_r": reference.get("expectancy_r"),
        },
    }


def _accepted_scores(runs_dir: Path) -> list[tuple[Path, dict[str, Any]]]:
    accepted = runs_dir / "accepted"
    found: list[tuple[Path, dict[str, Any]]] = []
    if not accepted.is_dir():
        return found
    for path in sorted(accepted.glob("*.json")):
        try:
            found.append((path, load_export(path)))
        except (OSError, json.JSONDecodeError):
            continue
    return found


def best_score(runs_dir: Path) -> tuple[Path, dict[str, Any]] | None:
    scored = [item for item in _accepted_scores(runs_dir) if item[1].get("valid")]
    if not scored:
        return None
    scored.sort(
        key=lambda item: (
            _num(item[1].get("win_rate_pct")) or 0.0,
            _pf_sort(item[1].get("profit_factor")),
            int(item[1].get("trades") or 0),
        )
    )
    return scored[-1]


def append_leaderboard(
    path: Path,
    *,
    score: dict[str, Any] | None,
    label: str,
    status: str,
    sha: str = "",
    note: str = "",
) -> None:
    header = (
        "| Run | When | Status | Win % | Trades | PF | Exp R | SHA | Note |\n"
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |\n"
    )
    row = [
        label,
        datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ"),
        status,
        f"{_num(score.get('win_rate_pct')) or 0.0:.2f}" if score else "-",
        str(score.get("trades")) if score else "-",
        (
            f"{_pf_sort(score.get('profit_factor')):.3f}"
            if score and score.get("profit_factor") is not None
            else ("inf" if score and _pf_sort(score.get("profit_factor")) == math.inf else "-")
        ),
        f"{_num(score.get('expectancy_r')) or 0.0:.3f}" if score else "-",
        sha[:10] if sha else "-",
        note.replace("|", "/"),
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    prefix = "" if path.exists() else header
    with path.open("a", encoding="utf-8") as handle:
        handle.write(prefix + "| " + " | ".join(row) + " |\n")


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    score = sub.add_parser("score", help="Score one export.")
    score.add_argument("--export", type=Path, required=True)
    score.add_argument("--market", default=DEFAULT_MARKET)
    score.add_argument("--min-closed", type=int, default=DEFAULT_MIN_CLOSED)
    score.add_argument("--out", type=Path)

    assert_p2 = sub.add_parser(
        "assert-patternsv2", help="Exit 1 unless Patternsv2 ran."
    )
    assert_p2.add_argument("--export", type=Path, required=True)
    assert_p2.add_argument("--market", default=DEFAULT_MARKET)

    valid = sub.add_parser("valid", help="Exit 0 when a score is usable.")
    valid.add_argument("--score", type=Path, required=True)

    compare = sub.add_parser("compare", help="Exit 0 when --new beats --reference.")
    compare.add_argument("--new", type=Path, required=True)
    compare.add_argument("--reference", type=Path, required=True)

    best = sub.add_parser("best", help="Print the best accepted score path.")
    best.add_argument("--runs-dir", type=Path, default=Path("runs"))

    board = sub.add_parser("leaderboard", help="Append one leaderboard row.")
    board.add_argument("--runs-dir", type=Path, default=Path("runs"))
    board.add_argument("--label", required=True)
    board.add_argument("--status", required=True)
    board.add_argument("--score", type=Path)
    board.add_argument("--sha", default="")
    board.add_argument("--note", default="")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "score":
        payload = score_export(
            load_export(args.export),
            market=args.market,
            min_closed=args.min_closed,
        )
        if args.out:
            _write_json(args.out, payload)
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    if args.command == "assert-patternsv2":
        book = load_book(load_export(args.export), args.market)
        settings = book.get("settings") or {}
        if not settings.get("patterns_v2"):
            print(
                "Patternsv2 is OFF in this export; refusing to score it.",
                file=sys.stderr,
            )
            return 1
        return 0
    if args.command == "valid":
        payload = load_export(args.score)
        if not payload.get("valid"):
            print(
                f"INVALID run: trades={payload.get('trades')} "
                f"min_closed={payload.get('min_closed')} "
                f"patterns_v2={payload.get('patterns_v2')}",
                file=sys.stderr,
            )
            return 1
        return 0
    if args.command == "compare":
        verdict = compare_scores(load_export(args.new), load_export(args.reference))
        print(json.dumps(verdict, indent=2, sort_keys=True))
        return 0 if verdict["improved"] else 1
    if args.command == "best":
        found = best_score(args.runs_dir)
        if found is None:
            return 1
        print(found[0])
        return 0
    if args.command == "leaderboard":
        score = load_export(args.score) if args.score else None
        append_leaderboard(
            args.runs_dir / "leaderboard.md",
            score=score,
            label=args.label,
            status=args.status,
            sha=args.sha,
            note=args.note,
        )
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
