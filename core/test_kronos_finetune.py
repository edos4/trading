"""Unit checks for the Kronos fine-tune path (CPU only, no GPU/weights/DB).

Covers docs/kronos-finetune.md §10 (P5-01):

  - No lookahead: max(train date) < min(val date); val is a 10% ±1% tail of
    each symbol's pre-cutoff bars; post-cutoff bars are never written.
  - No window's predict span crosses the train cutoff.
  - Normalization uses the lookback slice only.
  - Same seed ⇒ identical draw sequence.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from learn import kronos_dataset as kd


def _frame(n: int = 800, start: str = "2023-01-02", seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(start=start, periods=n)
    close = 100 + np.cumsum(rng.normal(0, 1, n))
    close = np.maximum(close, 5.0)
    df = pd.DataFrame(
        {
            "open": close * (1 + rng.normal(0, 0.002, n)),
            "high": close * 1.01,
            "low": close * 0.99,
            "close": close,
            "volume": rng.integers(10_000, 1_000_000, n).astype(float),
        },
        index=idx,
    )
    return df


def test_split_is_chronological_and_ratio(tmp_path):
    frames = [("AAA", _frame(1000), "us"), ("BBB.PS", _frame(600), "ph")]
    report = kd.build_from_frames(frames, tmp_path, train_cutoff="2026-01-01",
                                  min_bars=500)
    assert report["symbols_kept"] == 2
    meta = kd.load_corpus(tmp_path)[2]
    for s in meta["symbols"]:
        n = s["count"]
        # Val is 10% ±1% of each symbol's pre-cutoff bars.
        assert abs(s["val_count"] / n - 0.1) <= 0.01
        assert s["train_count"] + s["val_count"] == n
        # Chronological: val slice immediately follows train slice.
        assert s["val_offset"] == s["offset"] + s["train_count"]
    # Cutoff is structural: nothing at/after it was stored.
    days = np.asarray(kd.load_corpus(tmp_path)[1])
    assert days.max() < pd.Timestamp("2026-01-01").date().toordinal()


def test_post_cutoff_bars_held_out(tmp_path):
    df = _frame(800, start="2025-01-02")  # spans the 2026-01-01 cutoff
    n_pre = int((df.index < pd.Timestamp("2026-01-01")).sum())
    assert 0 < n_pre < len(df)  # test actually straddles the cutoff
    kd.build_from_frames([("AAA", df, "us")], tmp_path,
                         train_cutoff="2026-01-01", min_bars=100)
    feats, _days, meta = kd.load_corpus(tmp_path)
    assert meta["symbols"][0]["count"] == n_pre
    assert len(feats) == n_pre


def test_no_window_predict_span_crosses_cutoff(tmp_path):
    kd.build_from_frames([("AAA", _frame(1000), "us")], tmp_path,
                         train_cutoff="2026-01-01", min_bars=500)
    lookback, predict = 256, 10
    window = lookback + predict + 1
    _feats, days, meta = kd.load_corpus(tmp_path)
    s = meta["symbols"][0]
    cutoff_ord = pd.Timestamp("2026-01-01").date().toordinal()
    # Latest train-window start; its full span (lookback + predict + 1) must
    # still end before the cutoff.
    last_start = s["offset"] + s["train_count"] - window
    assert days[last_start + window - 1] < cutoff_ord


def test_normalization_uses_lookback_only():
    rng = np.random.default_rng(0)
    x = rng.normal(100, 5, (267, 6)).astype(np.float32)
    out = kd.normalize_window(x, 256, 5.0)
    mutated = x.copy()
    mutated[260:] *= 10  # mutate inside the predict span only
    out2 = kd.normalize_window(mutated, 256, 5.0)
    np.testing.assert_allclose(out[:256], out2[:256], rtol=1e-5, atol=1e-6)
    assert np.abs(out).max() <= 5.0 + 1e-6


def test_draw_sequence_deterministic(tmp_path):
    kd.build_from_frames(
        [("AAA", _frame(1000, seed=1), "us"), ("BBB", _frame(900, seed=2), "us")],
        tmp_path, train_cutoff="2026-01-01", min_bars=500,
    )
    from core.kronos_finetune import LeanWindowDataset

    def _seq():
        ds = LeanWindowDataset(tmp_path, 64, 10, 5.0, seed=42, n_iter=50)
        ds.set_epoch_seed(3)
        return [ds._draw()[0].tobytes() for _ in range(20)]

    assert _seq() == _seq()


def test_window_counts_offset_index(tmp_path):
    kd.build_from_frames(
        [("AAA", _frame(1000, seed=1), "us"), ("BBB", _frame(900, seed=2), "us")],
        tmp_path, train_cutoff="2026-01-01", min_bars=500,
    )
    lookback, predict = 64, 10
    window = lookback + predict + 1
    symbols, cum = kd.window_counts(tmp_path, lookback, predict)
    total = 0
    for s in symbols:
        expect = max(0, s["train_count"] - window + 1)
        assert s["window_count"] == expect
        total += expect
    assert int(cum[-1]) == total
    # locate_window round-trips the flat index space.
    if total:
        meta, local = kd.locate_window(symbols, cum, total - 1)
        assert 0 <= local < meta["window_count"]


def test_prepare_symbol_drop_reasons():
    cutoff = pd.Timestamp("2026-01-01")
    # Thin: too few bars.
    _, _, f, _o, reason = kd._prepare_symbol(
        "AAA", _frame(100), "us", cutoff=cutoff, min_bars=500,
        max_stale_days=None, reference_date=None,
    )
    assert reason == "thin" and len(f) == 0
    # Stale: last bar older than max_stale_days.
    _, _, _, _, reason = kd._prepare_symbol(
        "BBB", _frame(800, start="2020-01-02"), "us", cutoff=cutoff,
        min_bars=500, max_stale_days=30,
        reference_date=pd.Timestamp("2026-06-01").date(),
    )
    assert reason == "stale"
    # Kept: stored bars are all strictly before the cutoff.
    df = _frame(800, start="2025-01-02")
    sym, mkt, feats, ords, reason = kd._prepare_symbol(
        "ccc.ps", df, "PH", cutoff=cutoff, min_bars=100,
        max_stale_days=None, reference_date=None,
    )
    assert reason is None
    assert (sym, mkt) == ("CCC.PS", "ph")
    assert len(feats) == int((df.index < cutoff).sum())
    assert ords.max() < cutoff.date().toordinal()
    assert feats.shape[1] == 6


def test_prepare_dataset_cache_cutoff(tmp_path, monkeypatch):
    from core import kronos_finetune as kf

    kd.build_from_frames([("AAA", _frame(1000), "us")], tmp_path,
                         train_cutoff="2026-01-01", min_bars=500)
    # Same cutoff → reuse, DB never touched.
    d = kf.prepare_dataset(train_cutoff="2026-01-01", min_bars=500,
                           dataset_dir=tmp_path)
    assert d == tmp_path
    # Different cutoff → rebuild, not silent reuse.
    calls = []
    monkeypatch.setattr(
        kd, "build_dataset",
        lambda *a, **k: (calls.append((a, k)), kd.build_from_frames(
            [("AAA", _frame(1000), "us")], tmp_path,
            train_cutoff=k.get("train_cutoff", "2026-01-01"), min_bars=100))[1],
    )
    kf.prepare_dataset(train_cutoff="2025-01-01", min_bars=500,
                       dataset_dir=tmp_path)
    assert len(calls) == 1
    assert kf._cached_cutoff(tmp_path) == "2025-01-01"


class _FakeCursor:
    """Minimal psycopg cursor: plain (market map) and named (row stream)."""

    def __init__(self, conn, name=None):
        self._conn = conn
        self._name = name
        self.itersize = 0
        self._rows = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, *a):
        if "FROM symbols" in sql:
            self._rows = list(self._conn.markets)
        elif "FROM daily_bars" in sql:
            self._rows = list(self._conn.bars)
        return self

    def fetchall(self):
        return self._rows

    def __iter__(self):
        return iter(self._rows)


class _FakeConn:
    def __init__(self, markets, bars):
        self.markets = markets
        self.bars = bars

    def cursor(self, name=None):
        return _FakeCursor(self, name)

    def close(self):
        pass


def _synthetic_db():
    """(market_rows, bar_rows, frames) — one source of truth for both paths."""
    from datetime import date, datetime, timezone

    def _bars(symbol, start, n, seed):
        rng = np.random.default_rng(seed)
        idx = pd.bdate_range(start=start, periods=n)
        close = np.maximum(100 + np.cumsum(rng.normal(0, 1, n)), 5.0)
        df = pd.DataFrame(
            {
                "open": close, "high": close * 1.01, "low": close * 0.99,
                "close": close,
                "volume": rng.integers(10_000, 1_000_000, n).astype(float),
            },
            index=idx,
        )
        return df

    specs = [
        ("AAA", "us", _bars("AAA", "2020-01-02", 1500, 1)),     # kept, spans cutoff
        ("BBB.PS", "ph", _bars("BBB.PS", "2020-01-02", 1500, 2)),  # kept PH
        ("THIN", "us", _bars("THIN", "2025-06-02", 100, 3)),    # dropped: thin
        ("NEWCO", "us", _bars("NEWCO", "2026-03-02", 600, 4)),  # dropped: no pre-cutoff
    ]
    markets = [(s, m) for s, m, _ in specs]
    bars = []
    for s, _m, df in specs:
        for ts, row in zip(
            (pd.Timestamp(t).tz_localize("UTC").timestamp() for t in df.index),
            df.itertuples(),
        ):
            bars.append((s, int(ts), row.open, row.high, row.low, row.close, row.volume))
    bars.sort(key=lambda r: (r[0], r[1]))
    frames = [(s, df, m) for s, m, df in specs]
    return markets, bars, frames


def test_streaming_build_matches_in_memory(tmp_path, monkeypatch):
    import psycopg

    import data.db

    markets, bars, frames = _synthetic_db()
    monkeypatch.setattr(data.db, "build_dsn", lambda: "dbname=fake")
    monkeypatch.setattr(psycopg, "connect", lambda *a, **k: _FakeConn(markets, bars))

    out_stream = tmp_path / "stream"
    report = kd.build_dataset(out_stream, train_cutoff="2026-01-01", min_bars=500)
    assert report["symbols_kept"] == 2
    assert report["symbols_dropped_thin"] == 2
    assert {s["name"] for s in
            __import__("json").loads((out_stream / kd.SYMBOLS_FILE).read_text())["symbols"]} == {"AAA", "BBB.PS"}

    out_mem = tmp_path / "mem"
    kd.build_from_frames(frames, out_mem, train_cutoff="2026-01-01", min_bars=500)

    for fname in (kd.FEATURES_FILE, kd.DAYS_FILE):
        a = (out_stream / fname).read_bytes()
        b = (out_mem / fname).read_bytes()
        assert len(a) == len(b) > 0
        assert a == b
    import json as _json

    ms = _json.loads((out_stream / kd.SYMBOLS_FILE).read_text())["symbols"]
    mm = _json.loads((out_mem / kd.SYMBOLS_FILE).read_text())["symbols"]
    assert [(s["name"], s["market"], s["offset"], s["count"],
             s["train_count"], s["val_count"]) for s in ms] == [
            (s["name"], s["market"], s["offset"], s["count"],
             s["train_count"], s["val_count"]) for s in mm]


def test_streaming_build_stale_filter(tmp_path, monkeypatch):
    import psycopg
    from datetime import date

    import data.db

    markets, bars, _frames = _synthetic_db()
    monkeypatch.setattr(data.db, "build_dsn", lambda: "dbname=fake")
    monkeypatch.setattr(psycopg, "connect", lambda *a, **k: _FakeConn(markets, bars))
    report = kd.build_dataset(
        tmp_path, train_cutoff="2026-01-01", min_bars=100,
        max_stale_days=30, reference_date=date(2026, 10, 15),
    )
    # AAA/BBB/THIN last bars are 2025-era (stale vs 2026-10-15);
    # NEWCO has no pre-cutoff bars (thin).
    assert report["symbols_dropped_stale"] == 3
    assert report["symbols_dropped_thin"] == 1
    assert report["symbols_kept"] == 0


def test_checkpoint_roundtrip_with_stub_torch(tmp_path, monkeypatch):
    import pickle
    import sys
    import types

    from core import kronos_finetune as kf

    fake = types.ModuleType("torch")
    fake.save = lambda obj, path: pickle.dump(obj, open(path, "wb"))
    fake.load = lambda path, **kw: pickle.load(open(path, "rb"))
    monkeypatch.setitem(sys.modules, "torch", fake)

    class _Param:
        device = "cpu"

    class _State:
        def __init__(self, tag):
            self.tag = tag
            self.loaded = None

        def parameters(self):
            return iter([_Param()])

        def state_dict(self):
            return {"tag": self.tag}

        def load_state_dict(self, state):
            self.loaded = state

    model, optim, sched = _State("m"), _State("o"), _State("s")
    ckpt = tmp_path / "last.pt"
    kf._save_last(ckpt, epoch=4, total_epochs=10, model=model,
                  optimizer=optim, scheduler=sched, best_val_loss=0.5)
    start, best = kf._load_last(ckpt, model=model, optimizer=optim,
                                scheduler=sched, total_epochs=10)
    assert (start, best) == (4, 0.5)
    assert model.loaded == {"tag": "m"}

    # Different --epochs: weights reused, schedule restarted.
    model2, optim2, sched2 = _State("m2"), _State("o2"), _State("s2")
    start, best = kf._load_last(ckpt, model=model2, optimizer=optim2,
                                scheduler=sched2, total_epochs=30)
    assert (start, best) == (0, 0.5)
    assert model2.loaded == {"tag": "m"}

    # Missing / corrupt checkpoints refuse cleanly.
    assert kf._load_last(tmp_path / "nope.pt", model=model, optimizer=optim,
                         scheduler=sched, total_epochs=10) is None
    (tmp_path / "junk.pt").write_bytes(b"not a checkpoint")
    assert kf._load_last(tmp_path / "junk.pt", model=model, optimizer=optim,
                         scheduler=sched, total_epochs=10) is None


def test_draw_fuzz_bounds_and_stamps(tmp_path):
    from core.kronos_finetune import LeanWindowDataset

    kd.build_from_frames(
        [("AAA", _frame(1200, seed=1), "us"), ("BBB", _frame(300, seed=2), "us")],
        tmp_path, train_cutoff="2026-01-01", min_bars=200,
    )
    for split in ("train", "val"):
        ds = LeanWindowDataset(tmp_path, 64, 10, 5.0, seed=123, n_iter=10_000,
                               split=split)
        ds.set_epoch_seed(9)
        for _ in range(3000):
            x, stamp = ds._draw()
            assert x.shape == (75, 6) and stamp.shape == (75, 5)
            assert x.dtype == np.float32 and stamp.dtype == np.float32
            assert np.isfinite(x).all()
            assert (stamp[:, 0] == 0).all() and (stamp[:, 1] == 0).all()
            assert stamp[:, 2].min() >= 0 and stamp[:, 2].max() <= 6
            assert stamp[:, 3].min() >= 1 and stamp[:, 3].max() <= 31
            assert stamp[:, 4].min() >= 1 and stamp[:, 4].max() <= 12


if __name__ == "__main__":
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        test_split_is_chronological_and_ratio(d)
    print("kronos_finetune tests OK")
