"""
scripts/build_barcache.py — (re)build data/barcache/ from the bot's own data layer.

Fetches daily bars per symbol via data.history.fetch_ohlcv_candles (GET /api/history
→ TradingView fallback) and writes them to data/barcache/<market>/<SYMBOL>.json in
the format data/barcache.py expects. Skips symbols with fewer than --min-bars.

    python scripts/build_barcache.py --market us --universe default
    python scripts/build_barcache.py --market us --symbols NVDA,AMD --refresh

Use scripts/import_cjs_barcache.py instead to seed from an existing .cjs cache dir.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data.barcache import BARCACHE_ROOT, cache_path, market_dir, write  # noqa: E402
from data.universes import load as load_universe  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--market", default="us")
    ap.add_argument("--universe", default="default",
                    help="name under data/universes/ (default: 'default')")
    ap.add_argument("--symbols", default=None,
                    help="comma-separated override for --universe")
    ap.add_argument("--min-bars", type=int, default=100)
    ap.add_argument("--refresh", action="store_true",
                    help="re-fetch symbols already cached (default: skip them)")
    ap.add_argument("--root", default=str(BARCACHE_ROOT))
    args = ap.parse_args(argv)

    # --ui/--web history mode: facade first, no Yahoo surprises on a laptop.
    from data.history import enable_ui_web_history, fetch_ohlcv_candles
    enable_ui_web_history()

    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    else:
        symbols = load_universe(args.universe)

    out_dir = market_dir(args.market, args.root)
    out_dir.mkdir(parents=True, exist_ok=True)

    written = skipped_cached = skipped_thin = failed = 0
    for i, sym in enumerate(symbols, 1):
        if not args.refresh and cache_path(args.market, sym, args.root).exists():
            skipped_cached += 1
            continue
        try:
            candles = fetch_ohlcv_candles(sym, "1d", market=args.market)
        except Exception as exc:  # noqa: BLE001 - one bad symbol must not abort the sweep
            print(f"  [{i}/{len(symbols)}] {sym}: FETCH ERROR {exc}")
            failed += 1
            continue
        if len(candles) < args.min_bars:
            print(f"  [{i}/{len(symbols)}] {sym}: {len(candles)} bars < {args.min_bars} — skip")
            skipped_thin += 1
            continue
        write(args.market, sym, candles, source="fetch_ohlcv_candles", root=args.root)
        written += 1
        if written % 25 == 0:
            print(f"  [{i}/{len(symbols)}] {written} written…")

    manifest = {
        "built_utc": datetime.now(timezone.utc).isoformat(),
        "market": args.market,
        "universe": args.universe if not args.symbols else "custom",
        "count": len(list(out_dir.glob("*.json"))) - 1,
        "written_this_run": written,
        "skipped_cached": skipped_cached,
        "skipped_thin": skipped_thin,
        "failed": failed,
        "symbols": sorted(p.stem for p in out_dir.glob("*.json")
                          if not p.stem.startswith("_")),
    }
    (out_dir / "_manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"\n  {written} written, {skipped_cached} already cached, "
          f"{skipped_thin} thin, {failed} failed  ->  {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
