"""
scripts/import_cjs_barcache.py — one-time seed of data/barcache/ from a .cjs cache dir.

The locked pattern-backtest scripts at C:\\Users\\dell\\tradingview-mcp keep their
daily bars in ``barcache/`` and ``flagcache/`` (``{... "bars":[{time,open,high,
low,close,volume}]}``). This converts those files into the Python barcache format
(data/barcache.py) so the refactored engine can be validated against the same
bars the documented numbers were computed on.

This is a SEED step, not a runtime dependency. For a fresh pull via the bot's own
data layer use ``scripts/build_barcache.py``.

    python -m scripts.import_cjs_barcache \
        --src "C:/Users/dell/tradingview-mcp/barcache" \
        --src "C:/Users/dell/tradingview-mcp/flagcache" \
        --earnings "C:/Users/dell/tradingview-mcp/earnings_cache.json" \
        --market us
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data.barcache import BARCACHE_ROOT, market_dir, write  # noqa: E402
from data.tv_client import OHLCVCandle  # noqa: E402


def _load_cjs_bars(path: Path) -> list[OHLCVCandle]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    raw = payload.get("bars") or []
    out: list[OHLCVCandle] = []
    for b in raw:
        t = b.get("time") if "time" in b else b.get("t")
        if t is None:
            continue
        out.append(OHLCVCandle(
            open=float(b["open"]),
            high=float(b["high"]),
            low=float(b["low"]),
            close=float(b["close"]),
            volume=float(b.get("volume") or 0),
            timestamp=datetime.fromtimestamp(int(t), tz=timezone.utc),
        ))
    out.sort(key=lambda c: c.timestamp or datetime.min.replace(tzinfo=timezone.utc))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--src", action="append", required=True,
                    help="a .cjs cache dir (repeatable); later dirs win on conflict")
    ap.add_argument("--market", default="us")
    ap.add_argument("--min-bars", type=int, default=100)
    ap.add_argument("--earnings", default=None,
                    help="path to a .cjs earnings_cache.json to copy in")
    ap.add_argument("--root", default=str(BARCACHE_ROOT))
    args = ap.parse_args(argv)

    out_dir = market_dir(args.market, args.root)
    out_dir.mkdir(parents=True, exist_ok=True)

    written = skipped_thin = skipped_bad = 0
    for src in args.src:
        src_dir = Path(src)
        if not src_dir.is_dir():
            print(f"  ! {src_dir} is not a directory — skipping")
            continue
        for f in sorted(src_dir.glob("*.json")):
            if f.stem.startswith("_"):
                continue
            try:
                candles = _load_cjs_bars(f)
            except (ValueError, KeyError) as exc:
                print(f"  ! {f.name}: {exc}")
                skipped_bad += 1
                continue
            if len(candles) < args.min_bars:
                skipped_thin += 1
                continue
            write(args.market, f.stem, candles,
                  source=f"import_cjs:{src_dir.name}", root=args.root)
            written += 1

    if args.earnings:
        ep = Path(args.earnings)
        if ep.exists():
            dst = Path(args.root) / "earnings_cache.json"
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ep, dst)
            print(f"  earnings_cache.json -> {dst}")

    manifest = {
        "built_utc": datetime.now(timezone.utc).isoformat(),
        "market": args.market,
        "count": written,
        "skipped_thin": skipped_thin,
        "skipped_bad": skipped_bad,
        "sources": args.src,
        "symbols": sorted(p.stem for p in out_dir.glob("*.json")
                          if not p.stem.startswith("_")),
    }
    (out_dir / "_manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"\n  wrote {written} symbols to {out_dir}  "
          f"(skipped {skipped_thin} thin, {skipped_bad} bad)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
