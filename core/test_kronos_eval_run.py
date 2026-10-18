"""End-to-end `scripts/kronos_eval_test.py::run` with a fake predictor.

CPU only, no weights, no DB, no torch: exercises candidate selection (with
tz-aware frames — the naive/aware comparison used to raise TypeError),
walk-forward windowing, scoring, markdown + JSON reports, and the
base-vs-finetuned premise that identical seeds score identical windows.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]


def _load_script_module():
    spec = importlib.util.spec_from_file_location(
        "kronos_eval_test_under_test", ROOT / "scripts" / "kronos_eval_test.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _aware_frame(n: int = 400, start: str = "2025-01-02", seed: int = 3) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(start=start, periods=n, tz="UTC")  # like the history facade
    close = np.maximum(100 + np.cumsum(rng.normal(0, 1, n)), 5.0)
    return pd.DataFrame(
        {
            "open": close * (1 + rng.normal(0, 0.002, n)),
            "high": close * 1.01,
            "low": close * 0.99,
            "close": close,
            "volume": rng.integers(10_000, 1_000_000, n).astype(float),
        },
        index=idx,
    )


class _FakePredictor:
    """Constant +1%/window drift — deterministic, exercises the full path."""

    def predict(self, *, df, x_timestamp, y_timestamp, pred_len, **_kw):
        last = float(df["close"].iloc[-1])
        return pd.DataFrame({"close": np.full(pred_len, last * 1.01)})


def _run_twice(tmp_path, monkeypatch, mod, **kw):
    frames = [
        ("AAA", _aware_frame(seed=11)),
        ("BBB", _aware_frame(seed=12)),
        ("CCC", _aware_frame(seed=13)),
    ]
    monkeypatch.setattr(mod, "iter_ticker_frames", lambda **_: list(frames))
    monkeypatch.setattr(mod, "_load_predictor", lambda **_: _FakePredictor())
    outs = []
    for i, tag in enumerate(("base", "ft")):
        out = mod.run(
            n_symbols=3, windows_per_symbol=2, lookback=60, stride=5,
            min_bars=50, seed=7, device="cpu", liquid_only=False,
            sample_count=1, start_date="2026-06-01", use_finetuned=(i == 1),
            min_move=0.03, n_boot=50,
            out_path=tmp_path / f"{tag}.md",
            train_cutoff="2026-01-01",
            json_out=tmp_path / f"{tag}.json",
            **kw,
        )
        outs.append(out)
    return outs


def test_eval_run_writes_reports(tmp_path, monkeypatch):
    mod = _load_script_module()
    outs = _run_twice(tmp_path, monkeypatch, mod)
    for tag, out in zip(("base", "ft"), outs):
        assert out == tmp_path / f"{tag}.md"
        text = out.read_text(encoding="utf-8")
        assert "## Findings" in text and "## Raw window data" in text
        assert "2026-01-01" in text  # train cutoff recorded in config table
        summary = json.loads((tmp_path / f"{tag}.json").read_text(encoding="utf-8"))
        for key in ("score_1d", "score_3d", "persistence_3d", "majority_sign",
                    "gate", "ci_all", "ci_gate"):
            assert key in summary, key
        assert summary["score_3d"]["n"] > 0
        assert summary["config"]["train_cutoff"] == "2026-01-01"


def test_same_seed_scores_same_windows(tmp_path, monkeypatch):
    """The base-vs-finetuned comparison premise: same seed ⇒ same windows."""
    mod = _load_script_module()
    _run_twice(tmp_path, monkeypatch, mod)
    base = json.loads((tmp_path / "base.json").read_text(encoding="utf-8"))
    ft = json.loads((tmp_path / "ft.json").read_text(encoding="utf-8"))
    assert base["config"]["symbols"] == ft["config"]["symbols"]
    assert base["score_3d"]["n"] == ft["score_3d"]["n"]
    # Same fake predictions ⇒ identical scores; a real fine-tune would differ.
    assert base["score_3d"] == ft["score_3d"]


def test_cutoff_guard_refuses_overlap(tmp_path, monkeypatch):
    import pytest

    mod = _load_script_module()
    monkeypatch.setattr(mod, "iter_ticker_frames", lambda **_: [])
    with pytest.raises(SystemExit, match="REFUSED"):
        mod.run(start_date="2025-06-01", train_cutoff="2026-01-01",
                out_path=tmp_path / "x.md")


def test_as_naive_timestamp():
    from core.kronos_eval import as_naive_timestamp

    assert as_naive_timestamp(pd.Timestamp("2026-06-01", tz="UTC")) == pd.Timestamp("2026-06-01")
    assert as_naive_timestamp(pd.Timestamp("2026-06-01")) == pd.Timestamp("2026-06-01")
    assert as_naive_timestamp(pd.Timestamp("2026-06-01").date()) == pd.Timestamp("2026-06-01")
