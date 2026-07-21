"""
learn/ — offline ML training for the swing-win signal used by
patterns/012_ml_signal.py.

    python main.py --learn

ingests the historical daily OHLCV CSVs at settings.learn_data_dir, builds a
feature/label matrix (learn/features.py + learn/labels.py — the same feature
function the live pattern uses, so train and serve never drift), trains a
LightGBM classifier (learn/train.py), and saves it under models/ (learn/model.py).
"""
