"""Compare scanner hot paths with a git revision, without contacting data providers.

Run: .venv/bin/python scripts/benchmark_scanner.py --baseline <revision>
Timings are medians of three runs; HTTP work is a fixed-latency synthetic load.
"""

from __future__ import annotations

import argparse
import ast
import asyncio
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median
import subprocess
import sys
import time
from types import MethodType

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from data import ohlcv_store, stream_server, tv_client


def baseline_object(revision, module, name, method=None):
    path = Path(module.__file__).relative_to(Path(__file__).resolve().parents[1])
    source = subprocess.check_output(["git", "show", f"{revision}:{path}"], text=True)
    node = next(n for n in ast.parse(source).body if isinstance(n, ast.ClassDef) and n.name == name)
    if method:
        node = next(n for n in node.body if getattr(n, "name", None) == method)
    namespace = {**vars(module), "asdict": asdict}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), "exec"), namespace)
    return namespace[method or name]


def timed(function):
    samples = []
    for _ in range(3):
        start = time.perf_counter()
        function()
        samples.append(time.perf_counter() - start)
    return median(samples)


def compare(name, old, new):
    before, after = timed(old), timed(new)
    print(f"{name}: {before:.6f}s -> {after:.6f}s ({before / after:.2f}x)")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", required=True)
    args = parser.parse_args()
    old_tape = baseline_object(args.baseline, stream_server, "_SymbolTape")
    old_store = baseline_object(args.baseline, ohlcv_store, "OHLCVStore")
    old_fetch = baseline_object(args.baseline, tv_client, "TVClient", "fetch_snapshot")
    start = datetime(2024, 1, 1, 21, tzinfo=timezone.utc)
    candles = [tv_client.OHLCVCandle(10, 12, 9, 11, 100, start + timedelta(days=i))
               for i in range(420)]
    rows = [{**asdict(c), "timestamp": int(c.timestamp.timestamp())} for c in candles]
    old, new = old_tape(rows), stream_server._SymbolTape(rows)
    stamps = [row["timestamp"] for row in rows[100:300]]

    def lookups(tape):
        return [(tape.snapshot(ts, include_history=False), tape.index_for_asof(ts),
                 tape.next_ts_after(ts)) for ts in stamps]

    assert lookups(old) == lookups(new)
    compare("Replay: 200 sessions on a 420-bar tape", lambda: lookups(old), lambda: lookups(new))

    def frames(store_type):
        store = store_type()
        for _ in range(50):
            store.replace_all("TEST", "1d", candles)
            frame = store.get_df("TEST", "1d")
        return frame

    assert frames(old_store).equals(frames(ohlcv_store.OHLCVStore))
    compare("Frames: 50 rebuilds of 420 bars", lambda: frames(old_store),
            lambda: frames(ohlcv_store.OHLCVStore))

    def fetches(fetch):
        client = tv_client.TVClient("america", "NASDAQ")
        client.fetch_snapshot = MethodType(fetch, client)

        def candle(*args):
            time.sleep(0.025)
            return candles[-1]

        def history(*args, **kwargs):
            time.sleep(0.025)
            return candles.copy()

        async def analysis(*args, **kwargs):
            return {}

        client._fetch_candle_screener = candle
        client._fetch_history_screener = history
        client._fetch_mcp_analysis = analysis

        async def scan():
            semaphore = asyncio.Semaphore(4)
            store = ohlcv_store.OHLCVStore()

            async def snapshot(symbol):
                async with semaphore:
                    return await client.fetch_snapshot(symbol, "1d", store=store)

            snapshots = await asyncio.gather(*(snapshot(str(i)) for i in range(20)))
            assert all(s.candle.close == 11 for s in snapshots)

        asyncio.run(scan())

    compare("Synthetic HTTP: 20 symbols, 4 workers, 2x25ms calls",
            lambda: fetches(old_fetch), lambda: fetches(tv_client.TVClient.fetch_snapshot))


if __name__ == "__main__":
    main()
