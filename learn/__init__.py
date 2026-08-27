"""
learn/ — offline ML training for the swing-win signal used by
patterns/012_ml_signal.py.

    python main.py --learn

ingests daily OHLCV from GET /api/history, builds a
feature/label matrix (learn/features.py + learn/labels.py — the same feature
function the live pattern uses, so train and serve never drift), trains a
LightGBM classifier (learn/train.py), and saves it under models/ (learn/model.py).
"""
