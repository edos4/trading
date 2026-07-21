"""
patterns/012_ml_signal.py — ML swing-win probability signal.

Loads the LightGBM model trained via `python main.py --learn` (see learn/)
on historical daily OHLCV. It predicts P(price reaches the model's
target_pct before its stop_pct within horizon_days) using the exact same
feature function training used (learn/features.py), so there's no
train/serve skew. Emits BUY when predicted probability clears
settings.ml_confidence_threshold.

No model trained yet → `skipped` is True, so the scanner/backtester ignore
this pattern entirely rather than erroring.
"""

from __future__ import annotations

from patterns.base_pattern import BasePattern, TradeSignal
from data.tv_client import MarketSnapshot
from data.ohlcv_store import OHLCVStore
from learn.features import FEATURE_NAMES, MIN_BARS_REQUIRED, compute_features
from learn.model import MODEL_PATH, load_meta, load_model
from config import settings
from utils.logger import log


class MLSignalPattern(BasePattern):
    SHARES = 25

    def __init__(self):
        self._booster = None
        self._meta: dict | None = None
        self._load_failed = False

    @property
    def name(self) -> str:
        return "pattern_012_ml_signal"

    @property
    def timeframes(self) -> list[str]:
        return ["1d"]

    @property
    def skipped(self) -> bool:
        return not MODEL_PATH.exists()

    @property
    def chart_description(self) -> str:
        return "ML-predicted swing-win probability signal — no chart shape to describe."

    def _ensure_loaded(self) -> bool:
        if self._booster is not None:
            return True
        if self._load_failed:
            return False
        try:
            self._booster = load_model()
            self._meta = load_meta()
        except Exception:
            log.exception(f"{self.name} | failed to load model — disabling for this run")
            self._load_failed = True
            return False
        return True

    def analyze(self, snapshot: MarketSnapshot, store: OHLCVStore) -> TradeSignal | None:
        if not self._ensure_loaded():
            return None

        df = store.get_df(snapshot.symbol, snapshot.timeframe, min_bars=MIN_BARS_REQUIRED)
        if df is None:
            return None

        feats = compute_features(df)
        row = feats.iloc[-1]
        if row.isna().any():
            return None

        prob = float(self._booster.predict(row[FEATURE_NAMES].to_numpy().reshape(1, -1))[0])
        if prob < settings.ml_confidence_threshold:
            return None

        price = float(df["close"].iloc[-1])
        target_pct = self._meta["target_pct"]
        stop_pct = self._meta["stop_pct"]
        return TradeSignal(
            symbol=snapshot.symbol,
            action="BUY",
            pattern=self.name,
            timeframe=snapshot.timeframe,
            confidence=prob,
            price=price,
            qty=self.SHARES,
            stop_loss=price * (1 - stop_pct),
            take_profit=price * (1 + target_pct),
            notes=(
                f"ML P(win)={prob:.2f} over {self._meta['horizon_days']}d horizon "
                f"(val AUC={self._meta['val_auc']:.3f})"
            ),
        )
