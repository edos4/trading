"""Reproduce Patternsv2 detection, causal replay, and volume/return diagnostics.

Run ``python -m analysis.patternsv2_audit --help``. Inputs are explicit local
fixtures. No market clients, runtime stores, parameter fitting, or trading
execution are used. Fixed-horizon returns are descriptive event returns.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import hashlib
import inspect
import json
from pathlib import Path
import platform

import numpy as np
import pandas as pd
import scipy

from analysis.price_volume import volume_confirm_gate
from patterns_v2.patterns import PATTERN_NAMES, detect
from patterns_v2.patterns.double_bottom import detect as double_bottom
from patterns_v2.signals import _to_signal

_REPO = Path(__file__).resolve().parents[1]
DEFAULT_FIXTURES = _REPO / "tests" / "fixtures" / "barcache" / "us"


@dataclass(frozen=True)
class AuditPolicy:
    horizons: tuple[int, ...] = (5, 10, 20)
    cost_per_side: float = 0.001
    slippage_per_side: float = 0.0005
    rvol_min: float = 2.0
    obv_bars: int = 5

    def __post_init__(self) -> None:
        if not self.horizons or any(type(h) is not int or h < 1 for h in self.horizons):
            raise ValueError("horizons must be positive integer bar counts")
        for value in (self.cost_per_side, self.slippage_per_side):
            if not np.isfinite(value) or not 0 <= value < 1:
                raise ValueError("cost and slippage must be finite fractions in [0, 1)")
        if (not np.isfinite(self.rvol_min) or self.rvol_min < 0
                or type(self.obv_bars) is not int or self.obv_bars < 1):
            raise ValueError("volume policy must have nonnegative RVOL and positive OBV bars")


class _FrameStore:
    """Read-only adapter for the production volume gate; never touches a DB."""
    def __init__(self, frame: pd.DataFrame) -> None:
        self.frame = frame

    def get_df(self, _symbol: str, _timeframe: str, min_bars: int = 1) -> pd.DataFrame | None:
        return self.frame if len(self.frame) >= min_bars else None


def forward_return(
    opening: np.ndarray, close: np.ndarray, signal: int, direction: int,
    horizon: int, policy: AuditPolicy,
) -> dict | None:
    """Next-open entry, horizon-th close exit, costs on both executed legs."""
    entry, exit_bar = signal + 1, signal + horizon
    if entry >= len(opening) or exit_bar >= len(close):
        return None
    raw_entry, raw_exit = float(opening[entry]), float(close[exit_bar])
    if not np.isfinite(raw_entry) or not np.isfinite(raw_exit) or min(raw_entry, raw_exit) <= 0:
        return None
    slipped_entry = raw_entry * (1 + direction * policy.slippage_per_side)
    slipped_exit = raw_exit * (1 - direction * policy.slippage_per_side)
    gross = direction * (raw_exit / raw_entry - 1)
    net = (direction * (slipped_exit - slipped_entry)
           - policy.cost_per_side * (slipped_entry + slipped_exit)) / slipped_entry
    return {"gross": gross, "net": net}


def _summary(events: list[dict], horizon: int) -> dict:
    usable = [e["returns"][str(horizon)] for e in events if e["returns"][str(horizon)] is not None]
    net = [r["net"] for r in usable]
    return {
        "events": len(events), "matured": len(usable), "censored": len(events) - len(usable),
        "mean_gross_pct": float(np.mean([r["gross"] for r in usable]) * 100) if usable else None,
        "mean_net_pct": float(np.mean(net) * 100) if usable else None,
        "median_net_pct": float(np.median(net) * 100) if usable else None,
        "net_win_rate": float(np.mean(np.array(net) > 0)) if usable else None,
    }


def run_audit(
    fixture_root: Path = DEFAULT_FIXTURES, *, policy: AuditPolicy | None = None,
    check_prefixes: bool = False,
) -> dict:
    """Inspect defaults on a frozen corpus without selecting parameters by returns."""
    policy = policy or AuditPolicy()
    paths = sorted(fixture_root.glob("*.json"))
    if not paths:
        raise ValueError(f"No JSON fixtures at {fixture_root}")
    datasets, fixtures, timestamps = [], [], set()
    for path in paths:
        content = path.read_bytes()
        payload = json.loads(content)
        rows = payload["bars"]
        if not rows:
            continue
        times = [int(row["t"]) for row in rows]
        if any(b <= a for a, b in zip(times, times[1:])):
            raise ValueError(f"Fixture timestamps must increase strictly: {path.name}")
        frame = pd.DataFrame({name: [row.get(key, np.nan) for row in rows]
                              for name, key in (("open", "o"), ("high", "h"), ("low", "l"),
                                                ("close", "c"), ("volume", "v"))},
                             index=pd.to_datetime(times, unit="s", utc=True))
        datasets.append((str(payload.get("symbol", path.stem)), times, frame))
        fixtures.append({"file": path.name, "bars": len(rows), "sha256": hashlib.sha256(content).hexdigest()})
        timestamps.update(times)
    if not timestamps:
        raise ValueError("Fixtures contain no bars")
    unique_times = sorted(timestamps)
    split = unique_times[int(0.70 * (len(unique_times) - 1))]
    counts = {name: {"raw": 0, "converted": 0, "volume_passed": 0} for name in sorted(PATTERN_NAMES)}
    events, mismatches = [], []
    prefix_checks = 0
    for symbol, times, frame in datasets:
        h, l, c, opening = (frame[key].to_numpy(float) for key in ("high", "low", "close", "open"))
        full = detect(h, l, c)
        if check_prefixes:
            for n in range(60, len(c) + 1):
                got = detect(h[:n], l[:n], c[:n])
                prefix_checks += 1
                if got != [s for s in full if s["signal"] < n]:
                    mismatches.append({"symbol": symbol, "bars": n})
        for raw in full:
            name, j = raw["pattern"], raw["signal"]
            counts[name]["raw"] += 1
            past = frame.iloc[:j+1]
            signal = _to_signal(past, raw, symbol=symbol, timeframe="1d", latest=j)
            if signal is None:
                continue
            counts[name]["converted"] += 1
            verdict = volume_confirm_gate(signal, _FrameStore(past),
                                          rvol_min=policy.rvol_min, obv_bars=policy.obv_bars)
            counts[name]["volume_passed"] += int(verdict.passed)
            events.append({
                "symbol": symbol, "pattern": name, "direction": raw["direction"],
                "signal_bar": j, "signal_date": frame.index[j].isoformat(),
                "partition": "earlier" if times[j] < split else "later",
                "volume_passed": verdict.passed, "volume_reason": verdict.reason,
                "rvol": verdict.rvol, "obv_slope": verdict.obv_slope,
                "returns": {str(hz): forward_return(opening, c, j, raw["direction"], hz, policy)
                            for hz in policy.horizons},
            })
    groups = {}
    for name in ["all", *sorted(PATTERN_NAMES)]:
        for partition in ("all", "earlier", "later"):
            subset = [e for e in events if (name == "all" or e["pattern"] == name)
                      and (partition == "all" or e["partition"] == partition)]
            groups[f"{name}/{partition}"] = {
                gate: {str(hz): _summary([e for e in subset if gate == "all" or e["volume_passed"]], hz)
                       for hz in policy.horizons}
                for gate in ("all", "volume_passed")
            }
    sources = sorted((_REPO / "patterns_v2" / "patterns").glob("*.py"))
    sources += [_REPO / "patterns_v2" / "signals.py", Path(__file__),
                _REPO / "analysis" / "price_volume.py", _REPO / "analysis" / "indicator_engine.py"]
    return {
        "schema_version": 1, "runtime": {"python": platform.python_version(), "numpy": np.__version__,
                                         "pandas": pd.__version__, "scipy": scipy.__version__},
        "policy": asdict(policy), "double_bottom_defaults": {
            name: param.default for name, param in inspect.signature(double_bottom).parameters.items()
            if param.default is not inspect.Parameter.empty},
        "sources": {str(path.relative_to(_REPO)): hashlib.sha256(path.read_bytes()).hexdigest() for path in sources},
        "fixtures": fixtures, "symbols": len(datasets), "bars": sum(len(df) for _, _, df in datasets),
        "date_split": pd.Timestamp(split, unit="s", tz="UTC").isoformat(),
        "prefix_verification": {"requested": check_prefixes, "checks": prefix_checks, "mismatches": mismatches},
        "counts": counts, "summaries": groups, "events": events,
        "limitations": [
            "Selected pinned US fixtures; no representative universe or PSE sample.",
            "Small overlapping event samples; no significance or independence claim.",
            "Earlier/later slices are descriptive, both inspected; no untouched holdout or parameter fitting.",
            "Next-open fixed-horizon notional returns; no stops, targets, portfolio constraints, borrow fees or dividends.",
            "Volume selection reuses the production fail-open RVOL/OBV gate with explicit settings.",
        ],
    }


def render_markdown(report: dict) -> str:
    """Render the compact review table; full event details remain in JSON."""
    lines = ["# Patternsv2 offline fixture audit", "",
             "Generated by `python -m analysis.patternsv2_audit`. Descriptive event diagnostics; no parameter fitting.", "",
             f"Corpus: {report['symbols']} symbols, {report['bars']} bars. Date split: {report['date_split']}.",
             f"Policy: `{json.dumps(report['policy'], sort_keys=True)}`.",
             f"Prefix verification: `{json.dumps(report['prefix_verification'], sort_keys=True)}`.", "",
             "| Pattern | Raw | Converted | Volume passed |", "|---|---:|---:|---:|"]
    for name, counts in report["counts"].items():
        lines.append(f"| {name} | {counts['raw']} | {counts['converted']} | {counts['volume_passed']} |")
    lines += ["", "Returns enter at the next open; slippage and transaction costs apply on both legs.",
              "Unmatured horizons are excluded and counted as censored. Counts are events, not independent portfolio trades.", "",
              "| Pattern / slice | Volume selection | Bars held | Matured | Censored | Mean gross % | Mean net % |",
              "|---|---|---:|---:|---:|---:|---:|"]
    for name in ("all", "double_bottom", "double_top"):
        for partition in ("all", "earlier", "later"):
            for gate, horizons in report["summaries"][f"{name}/{partition}"].items():
                for hz in sorted(horizons, key=int):
                    metrics = horizons[hz]
                    fmt = lambda key: "—" if metrics[key] is None else f"{metrics[key]:.3f}"
                    lines.append(f"| {name} / {partition} | {gate} | {hz} | {metrics['matured']} | {metrics['censored']} | {fmt('mean_gross_pct')} | {fmt('mean_net_pct')} |")
    lines += ["", "Limitations:", "", *(f"- {item}" for item in report["limitations"]), "",
              "The JSON artifact records source/fixture hashes, every event, and all pattern summaries.", ""]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixtures", type=Path, default=DEFAULT_FIXTURES)
    parser.add_argument("--output", type=Path, help="JSON output; otherwise print to stdout")
    parser.add_argument("--markdown", type=Path, help="Optional compact Markdown report")
    parser.add_argument("--check-prefixes", action="store_true", help="Replay every eligible prefix for every family")
    parser.add_argument("--cost-per-side", type=float, default=0.001)
    parser.add_argument("--slippage-per-side", type=float, default=0.0005)
    parser.add_argument("--rvol-min", type=float, default=2.0)
    parser.add_argument("--obv-bars", type=int, default=5)
    args = parser.parse_args()
    policy = AuditPolicy(cost_per_side=args.cost_per_side, slippage_per_side=args.slippage_per_side,
                         rvol_min=args.rvol_min, obv_bars=args.obv_bars)
    report = run_audit(args.fixtures, policy=policy, check_prefixes=args.check_prefixes)
    payload = json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n"
    if args.output:
        args.output.write_text(payload)
    else:
        print(payload, end="")
    if args.markdown:
        args.markdown.write_text(render_markdown(report))
    return int(bool(report["prefix_verification"]["mismatches"]))


if __name__ == "__main__":
    raise SystemExit(main())
