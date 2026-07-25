"""
core/kronos_finetune.py — orchestrates `python main.py --kronos-finetune`:
fine-tune Kronos-base's tokenizer and predictor on liquid tickers from
/home/r00t/stocks_data, so it stops being a pure zero-shot forecaster (see
`python main.py --kronos-test` — zero-shot loses to a flat "no change"
baseline on this data).

Ports the training loops from ~/Kronos/finetune/{train_tokenizer,train_predictor}.py:
same loss functions, same window normalization. Deliberately drops two
things from the original scripts:
  - DDP / torchrun — this box has one GPU, no cluster to coordinate.
  - QlibDataset's `from config import Config` — that import resolves
    against whatever's first on sys.path, and this repo already has its
    own top-level config.py (trading settings). Importing Kronos's
    finetune/config.py under the same module name would shadow it.
Everything else (window dataset, AdamW + OneCycleLR, checkpointing on best
val loss) matches the original.
"""

from __future__ import annotations

import math
import pickle
from pathlib import Path

import numpy as np
import pandas as pd
import torch
import torch.nn.functional as F
from torch.utils.data import DataLoader, Dataset

from core.kronos_eval import KRONOS_REPO_DIR, MODEL_PATH, TOKENIZER_PATH
from learn.dataset import DEFAULT_DATA_DIR, iter_ticker_frames
from utils.logger import log

FEATURE_LIST = ["open", "high", "low", "close", "volume", "amount"]
TIME_FEATURE_LIST = ["minute", "hour", "weekday", "day", "month"]

FINETUNE_DIR = KRONOS_REPO_DIR / "finetuned"
DATASET_DIR = FINETUNE_DIR / "dataset"
TOKENIZER_OUT = FINETUNE_DIR / "tokenizer" / "best_model"
PREDICTOR_OUT = FINETUNE_DIR / "predictor" / "best_model"


# ── Dataset prep ───────────────────────────────────────────────────────────

def _rank_liquid_symbols(data_dir: Path, n_symbols: int, min_bars: int, liquidity_window: int = 60):
    """Same dollar-volume ranking as `--kronos-test --kronos-liquid-only`."""
    candidates = [(s, df) for s, df in iter_ticker_frames(data_dir) if len(df) >= min_bars]

    def dollar_volume(df: pd.DataFrame) -> float:
        tail = df.tail(liquidity_window)
        return float((tail["close"] * tail["volume"]).mean())

    ranked = [(dollar_volume(df), s, df) for s, df in candidates]
    ranked = [r for r in ranked if not math.isnan(r[0])]
    ranked.sort(key=lambda r: r[0], reverse=True)
    return [(s, df) for _dv, s, df in ranked[:n_symbols]]


def prepare_dataset(
    data_dir: Path = DEFAULT_DATA_DIR,
    n_symbols: int = 1500,
    min_bars: int = 500,
    val_frac: float = 0.1,
) -> tuple[Path, Path]:
    """Build train_data.pkl / val_data.pkl — dict[symbol] -> DataFrame,
    split chronologically per-symbol so validation never leaks earlier
    prices into a later training window. Caches to disk; delete DATASET_DIR
    to force a rebuild after changing n_symbols/data_dir."""
    train_path = DATASET_DIR / "train_data.pkl"
    val_path = DATASET_DIR / "val_data.pkl"
    if train_path.exists() and val_path.exists():
        log.info(f"Kronos-finetune | reusing cached dataset at {DATASET_DIR}")
        return train_path, val_path

    log.info(f"Kronos-finetune | ranking top {n_symbols} liquid tickers from {data_dir} ...")
    ranked = _rank_liquid_symbols(data_dir, n_symbols, min_bars)
    log.info(f"Kronos-finetune | building dataset from {len(ranked)} symbols")

    train_data: dict[str, pd.DataFrame] = {}
    val_data: dict[str, pd.DataFrame] = {}
    for symbol, df in ranked:
        df = df.copy()
        # Same amount fallback KronosPredictor.predict() uses when a feed has no
        # real dollar-amount column — keeps train/serve feature semantics identical.
        df["amount"] = df["volume"] * df[["open", "high", "low", "close"]].mean(axis=1)
        df.index.name = "datetime"
        split = int(len(df) * (1 - val_frac))
        if split < 60 or len(df) - split < 20:
            continue
        train_data[symbol] = df.iloc[:split][FEATURE_LIST]
        val_data[symbol] = df.iloc[split:][FEATURE_LIST]

    DATASET_DIR.mkdir(parents=True, exist_ok=True)
    with train_path.open("wb") as f:
        pickle.dump(train_data, f)
    with val_path.open("wb") as f:
        pickle.dump(val_data, f)
    log.info(f"Kronos-finetune | dataset saved to {DATASET_DIR} ({len(train_data)} symbols)")
    return train_path, val_path


# ── Windowed dataset (ported from ~/Kronos/finetune/dataset.py::QlibDataset) ──

class _WindowDataset(Dataset):
    def __init__(
        self, pkl_path: Path, lookback_window: int, predict_window: int,
        clip: float, seed: int, n_iter: int,
    ):
        with pkl_path.open("rb") as f:
            self.data: dict[str, pd.DataFrame] = pickle.load(f)
        self.lookback_window = lookback_window
        self.window = lookback_window + predict_window + 1
        self.clip = clip
        self.py_rng = __import__("random").Random(seed)

        self.indices: list[tuple[str, int]] = []
        for symbol, df in list(self.data.items()):
            df = df.reset_index()
            n = len(df) - self.window + 1
            if n <= 0:
                del self.data[symbol]
                continue
            df["minute"] = df["datetime"].dt.minute
            df["hour"] = df["datetime"].dt.hour
            df["weekday"] = df["datetime"].dt.weekday
            df["day"] = df["datetime"].dt.day
            df["month"] = df["datetime"].dt.month
            self.data[symbol] = df[FEATURE_LIST + TIME_FEATURE_LIST]
            self.indices.extend((symbol, i) for i in range(n))

        self.n_samples = min(n_iter, len(self.indices))
        log.info(f"Kronos-finetune | dataset {pkl_path.name}: {len(self.indices)} windows, using {self.n_samples}/epoch")

    def set_epoch_seed(self, epoch: int) -> None:
        self.py_rng.seed(epoch)

    def __len__(self) -> int:
        return self.n_samples

    def __getitem__(self, _idx: int):
        symbol, start = self.indices[self.py_rng.randint(0, len(self.indices) - 1)]
        win = self.data[symbol].iloc[start:start + self.window]

        x = win[FEATURE_LIST].to_numpy(dtype=np.float32)
        x_stamp = win[TIME_FEATURE_LIST].to_numpy(dtype=np.float32)

        past = x[: self.lookback_window]
        x_mean, x_std = np.mean(past, axis=0), np.std(past, axis=0)
        x = np.clip((x - x_mean) / (x_std + 1e-5), -self.clip, self.clip)

        return torch.from_numpy(x), torch.from_numpy(x_stamp)


# ── Training loops (ported from ~/Kronos/finetune/train_{tokenizer,predictor}.py) ──

def _train_tokenizer(
    train_pkl: Path, val_pkl: Path, device: torch.device, epochs: int, batch_size: int,
    lr: float, lookback: int, predict_len: int, clip: float, seed: int,
) -> Path:
    from model import KronosTokenizer

    log.info(f"Kronos-finetune | tokenizer: loading base weights from {TOKENIZER_PATH}")
    model = KronosTokenizer.from_pretrained(str(TOKENIZER_PATH)).to(device)

    train_ds = _WindowDataset(train_pkl, lookback, predict_len, clip, seed, n_iter=2000 * batch_size)
    val_ds = _WindowDataset(val_pkl, lookback, predict_len, clip, seed, n_iter=400 * batch_size)
    train_loader = DataLoader(train_ds, batch_size=batch_size, num_workers=0, drop_last=True)
    val_loader = DataLoader(val_ds, batch_size=batch_size, num_workers=0)

    optimizer = torch.optim.AdamW(model.parameters(), lr=lr, weight_decay=0.1)
    scheduler = torch.optim.lr_scheduler.OneCycleLR(
        optimizer, max_lr=lr, steps_per_epoch=len(train_loader), epochs=epochs, pct_start=0.03, div_factor=10
    )

    best_val_loss = float("inf")
    TOKENIZER_OUT.parent.mkdir(parents=True, exist_ok=True)

    for epoch in range(epochs):
        model.train()
        train_ds.set_epoch_seed(epoch)
        for step, (x, _stamp) in enumerate(train_loader):
            x = x.to(device)
            zs, bsq_loss, _, _ = model(x)
            z_pre, z = zs
            recon_loss = F.mse_loss(z_pre, x) + F.mse_loss(z, x)
            loss = (recon_loss + bsq_loss) / 2

            optimizer.zero_grad()
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), max_norm=2.0)
            optimizer.step()
            scheduler.step()

            if (step + 1) % 50 == 0:
                log.info(f"Kronos-finetune | tokenizer epoch {epoch + 1}/{epochs} step {step + 1}/{len(train_loader)} loss={loss.item():.4f}")

        model.eval()
        val_ds.set_epoch_seed(0)
        val_loss_sum, val_n = 0.0, 0
        with torch.no_grad():
            for x, _stamp in val_loader:
                x = x.to(device)
                zs, _, _, _ = model(x)
                _, z = zs
                val_loss_sum += F.mse_loss(z, x).item() * x.size(0)
                val_n += x.size(0)
        avg_val_loss = val_loss_sum / val_n if val_n else float("inf")
        log.info(f"Kronos-finetune | tokenizer epoch {epoch + 1}/{epochs} val_loss={avg_val_loss:.4f}")

        if avg_val_loss < best_val_loss:
            best_val_loss = avg_val_loss
            model.save_pretrained(str(TOKENIZER_OUT))
            log.info(f"Kronos-finetune | tokenizer checkpoint saved (val_loss={best_val_loss:.4f}) -> {TOKENIZER_OUT}")

    return TOKENIZER_OUT


def _train_predictor(
    train_pkl: Path, val_pkl: Path, tokenizer_path: Path, device: torch.device,
    epochs: int, batch_size: int, lr: float, lookback: int, predict_len: int, clip: float, seed: int,
) -> Path:
    from model import Kronos, KronosTokenizer

    log.info(f"Kronos-finetune | predictor: loading base weights from {MODEL_PATH}, tokenizer from {tokenizer_path}")
    tokenizer = KronosTokenizer.from_pretrained(str(tokenizer_path)).to(device)
    tokenizer.eval()
    model = Kronos.from_pretrained(str(MODEL_PATH)).to(device)

    train_ds = _WindowDataset(train_pkl, lookback, predict_len, clip, seed, n_iter=2000 * batch_size)
    val_ds = _WindowDataset(val_pkl, lookback, predict_len, clip, seed, n_iter=400 * batch_size)
    train_loader = DataLoader(train_ds, batch_size=batch_size, num_workers=0, drop_last=True)
    val_loader = DataLoader(val_ds, batch_size=batch_size, num_workers=0)

    optimizer = torch.optim.AdamW(model.parameters(), lr=lr, betas=(0.9, 0.95), weight_decay=0.1)
    scheduler = torch.optim.lr_scheduler.OneCycleLR(
        optimizer, max_lr=lr, steps_per_epoch=len(train_loader), epochs=epochs, pct_start=0.03, div_factor=10
    )

    best_val_loss = float("inf")
    PREDICTOR_OUT.parent.mkdir(parents=True, exist_ok=True)

    def _step_loss(x, x_stamp):
        with torch.no_grad():
            tok0, tok1 = tokenizer.encode(x, half=True)
        tok_in = [tok0[:, :-1], tok1[:, :-1]]
        tok_out = [tok0[:, 1:], tok1[:, 1:]]
        logits = model(tok_in[0], tok_in[1], x_stamp[:, :-1, :])
        loss, _, _ = model.head.compute_loss(logits[0], logits[1], tok_out[0], tok_out[1])
        return loss

    for epoch in range(epochs):
        model.train()
        train_ds.set_epoch_seed(epoch)
        for step, (x, x_stamp) in enumerate(train_loader):
            x, x_stamp = x.to(device), x_stamp.to(device)
            loss = _step_loss(x, x_stamp)

            optimizer.zero_grad()
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), max_norm=3.0)
            optimizer.step()
            scheduler.step()

            if (step + 1) % 50 == 0:
                log.info(f"Kronos-finetune | predictor epoch {epoch + 1}/{epochs} step {step + 1}/{len(train_loader)} loss={loss.item():.4f}")

        model.eval()
        val_ds.set_epoch_seed(0)
        val_loss_sum, val_batches = 0.0, 0
        with torch.no_grad():
            for x, x_stamp in val_loader:
                x, x_stamp = x.to(device), x_stamp.to(device)
                val_loss_sum += _step_loss(x, x_stamp).item()
                val_batches += 1
        avg_val_loss = val_loss_sum / val_batches if val_batches else float("inf")
        log.info(f"Kronos-finetune | predictor epoch {epoch + 1}/{epochs} val_loss={avg_val_loss:.4f}")

        if avg_val_loss < best_val_loss:
            best_val_loss = avg_val_loss
            model.save_pretrained(str(PREDICTOR_OUT))
            log.info(f"Kronos-finetune | predictor checkpoint saved (val_loss={best_val_loss:.4f}) -> {PREDICTOR_OUT}")

    return PREDICTOR_OUT


def run_kronos_finetune(
    data_dir: Path = DEFAULT_DATA_DIR,
    n_symbols: int = 1500,
    min_bars: int = 500,
    lookback: int = 256,
    predict_len: int = 10,
    clip: float = 5.0,
    tokenizer_epochs: int = 10,
    predictor_epochs: int = 10,
    batch_size: int = 16,
    tokenizer_lr: float = 2e-4,
    predictor_lr: float = 4e-5,
    seed: int = 42,
    skip_tokenizer: bool = False,
) -> None:
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    log.info(
        f"Kronos-finetune | device={device} symbols={n_symbols} lookback={lookback} "
        f"predict_len={predict_len} batch_size={batch_size} "
        f"tokenizer_epochs={tokenizer_epochs} predictor_epochs={predictor_epochs}"
    )
    if device.type == "cpu":
        log.warning("Kronos-finetune | running on CPU — this will be extremely slow, install CUDA torch first")

    train_pkl, val_pkl = prepare_dataset(data_dir, n_symbols, min_bars)

    tokenizer_path = Path(TOKENIZER_PATH)
    if not skip_tokenizer:
        tokenizer_path = _train_tokenizer(
            train_pkl, val_pkl, device, tokenizer_epochs, batch_size, tokenizer_lr, lookback, predict_len, clip, seed
        )
    elif TOKENIZER_OUT.exists():
        tokenizer_path = TOKENIZER_OUT

    predictor_path = _train_predictor(
        train_pkl, val_pkl, tokenizer_path, device, predictor_epochs, batch_size,
        predictor_lr, lookback, predict_len, clip, seed,
    )

    print()
    print("=" * 60)
    print("  Kronos fine-tune complete")
    print("=" * 60)
    print(f"  tokenizer -> {tokenizer_path}")
    print(f"  predictor -> {predictor_path}")
    print("  Re-run `python main.py --kronos-test ... --kronos-use-finetuned` to compare accuracy.")
    print()
