"""learn/model.py — save/load for the trained swing-win LightGBM model."""

from __future__ import annotations

import json
from pathlib import Path

import lightgbm as lgb

MODEL_DIR = Path("models")
MODEL_PATH = MODEL_DIR / "pattern_012_ml_signal.txt"
META_PATH = MODEL_DIR / "pattern_012_ml_signal_meta.json"


def save_model(booster: lgb.Booster, meta: dict) -> None:
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    booster.save_model(str(MODEL_PATH))
    META_PATH.write_text(json.dumps(meta, indent=2))


def load_model() -> lgb.Booster:
    return lgb.Booster(model_file=str(MODEL_PATH))


def load_meta() -> dict:
    return json.loads(META_PATH.read_text())
