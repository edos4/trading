"""
core/kronos_finetune.py — fine-tune Kronos-base's tokenizer + predictor.

Implements docs/kronos-finetune.md §6 + P2-01: restores the training loops
deleted in 89240d9 (`git show 89240d9^:core/kronos_finetune.py`) and rewires
them to the lean corpus from `learn/kronos_dataset.py`.

Key changes vs the deleted version:

  - No `prepare_dataset` pickle path by default (`--all-symbols`): the old
    `dict[str, DataFrame]` pickle + `list[(symbol, i)]` window index are
    OOM-adjacent at 17,719 symbols. The lean path memory-maps one float32
    array and samples via per-symbol `window_count` + cumulative offsets
    (~70 KB instead of ~2.6–2.9 GB).
  - Torch is imported lazily inside functions so the app venv (torch-free
    by policy) can still import this module and run its unit tests.
  - New flags: `--all-symbols` (lean path, default), `--train-cutoff`,
    `--smoke-steps` (real forward+backward calibration, both stages),
    `--resume` (restart from per-epoch `last.pt`: model + optimizer +
    scheduler state, not just best weights).

Training math is unchanged from upstream `finetune/train_{tokenizer,
predictor}.py`: same window normalization, AdamW + OneCycleLR, checkpoint
on best val loss. No DDP/torchrun (one GPU), no Qlib (its `config` module
would shadow this repo's top-level `config.py`).
"""

from __future__ import annotations

import json
import random
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np

from learn import kronos_dataset as kd
from utils.logger import log

FEATURE_LIST = list(kd.FEATURE_COLS)
TIME_FEATURE_LIST = list(kd.STAMP_COLS)

KRONOS_REPO_DIR = Path.home() / "Kronos"
if str(KRONOS_REPO_DIR) not in sys.path:
    sys.path.insert(0, str(KRONOS_REPO_DIR))

TOKENIZER_PATH = KRONOS_REPO_DIR / "weights" / "Kronos-Tokenizer-base"
MODEL_PATH = KRONOS_REPO_DIR / "weights" / "Kronos-base"

FINETUNE_DIR = KRONOS_REPO_DIR / "finetuned"
DATASET_DIR = FINETUNE_DIR / "dataset"
TOKENIZER_OUT = FINETUNE_DIR / "tokenizer" / "best_model"
PREDICTOR_OUT = FINETUNE_DIR / "predictor" / "best_model"
# Per-epoch training state for --resume (model + optimizer + scheduler +
# epoch + best val loss). best_model stays the artifact of record; last.pt
# is what a killed run restarts from.
TOKENIZER_LAST = FINETUNE_DIR / "tokenizer" / "last.pt"
PREDICTOR_LAST = FINETUNE_DIR / "predictor" / "last.pt"

try:
    from torch.utils.data import Dataset as _TorchDataset
except ImportError:  # app venv is torch-free by policy
    _TorchDataset = object  # type: ignore[assignment,misc]


def default_dataset_dir() -> Path:
    return DATASET_DIR


def _cached_cutoff(dataset_dir: Path) -> str | None:
    try:
        meta = json.loads((dataset_dir / kd.SYMBOLS_FILE).read_text(encoding="utf-8"))
        return meta.get("train_cutoff")
    except (OSError, ValueError, KeyError):
        return None


def prepare_dataset(
    *,
    train_cutoff: str = kd.DEFAULT_TRAIN_CUTOFF,
    min_bars: int = 500,
    max_stale_days: int | None = None,
    dataset_dir: str | Path = DATASET_DIR,
    force_rebuild: bool = False,
) -> Path:
    """Build (or reuse) the lean corpus. Returns the dataset directory."""
    import pandas as _pandas

    d = Path(dataset_dir)
    feats, days, syms = d / kd.FEATURES_FILE, d / kd.DAYS_FILE, d / kd.SYMBOLS_FILE
    if not force_rebuild and feats.exists() and days.exists() and syms.exists():
        want = _pandas.Timestamp(train_cutoff).date().isoformat()
        got = _cached_cutoff(d)
        if got == want:
            log.info(f"Kronos-finetune | reusing cached dataset at {d} (cutoff={got})")
            return d
        log.warning(
            f"Kronos-finetune | cached dataset cutoff={got} != requested {want} — rebuilding"
        )
    log.info(
        "Kronos-finetune | streaming stocks_history → lean corpus "
        f"(cutoff={train_cutoff} min_bars={min_bars}) ..."
    )
    report = kd.build_dataset(
        d, train_cutoff=train_cutoff, min_bars=min_bars,
        max_stale_days=max_stale_days,
    )
    log.info(
        f"Kronos-finetune | dataset saved to {d} "
        f"({report['symbols_kept']} symbols, {report['bars_total']} bars, "
        f"train={report['bars_train']} val={report['bars_val']})"
    )
    return d


class LeanWindowDataset(_TorchDataset):  # type: ignore[valid-type,misc]
    """Torch Dataset over the lean memmap corpus (train or val split).

    Sampling is pure-numpy + `random.Random(seed + epoch)` for determinism;
    `__getitem__` ignores its index and draws uniformly from the flat window
    pool via cumulative offsets (`np.searchsorted`). Time stamps are derived
    from stored day-ordinals: minute/hour are constant 0 for daily bars
    (model contract stays 5-dim).
    """

    def __init__(
        self,
        dataset_dir: str | Path,
        lookback_window: int,
        predict_window: int,
        clip: float,
        seed: int,
        n_iter: int,
        *,
        split: str = "train",
    ):
        try:
            import torch
        except ImportError:
            # App venv is torch-free by policy: sampling helpers (_draw,
            # set_epoch_seed) still work and return numpy; only the torch
            # tensor conversion in __getitem__ needs the kronos venv.
            torch = None
        self._torch = torch
        self.lookback_window = lookback_window
        self.predict_window = predict_window
        self.window = lookback_window + predict_window + 1
        self.clip = clip
        self.seed = seed
        self.split = split
        self.py_rng = random.Random(seed)

        self.feats, self.days, self.meta = kd.load_corpus(dataset_dir)
        self.symbols, self.cum = kd.window_counts(dataset_dir, lookback_window, predict_window)
        if split == "val":
            # Validation draws from each symbol's val slice instead of train.
            # window_count still gates on train geometry; val slices are
            # shorter, so clamp per-symbol val windows here.
            self._val_windows = [
                max(0, s["val_count"] - self.window + 1) for s in self.symbols
            ]
            self._val_cum = np.zeros(len(self._val_windows) + 1, dtype=np.int64)
            np.cumsum(np.array(self._val_windows, dtype=np.int64), out=self._val_cum[1:])
            total = int(self._val_cum[-1])
        else:
            total = int(self.cum[-1])
        if total <= 0:
            raise ValueError(
                f"kronos_finetune | no {split} windows (lookback={lookback_window} "
                f"predict={predict_window}) — corpus too thin?"
            )
        self.total_windows = total
        self.n_samples = min(n_iter, total)
        log.info(
            f"Kronos-finetune | {split} windows: {total} pooled, "
            f"using {self.n_samples}/epoch"
        )

    def set_epoch_seed(self, epoch: int) -> None:
        self.py_rng.seed(self.seed + epoch)

    def __len__(self) -> int:
        return self.n_samples

    def _draw(self) -> tuple[np.ndarray, np.ndarray]:
        if self.split == "val":
            g = self.py_rng.randrange(self.total_windows)
            sym_pos = int(np.searchsorted(self._val_cum, g, side="right")) - 1
            local = g - int(self._val_cum[sym_pos])
            meta = self.symbols[sym_pos]
            start = meta["val_offset"] + local
        else:
            g = self.py_rng.randrange(self.total_windows)
            sym_pos = int(np.searchsorted(self.cum, g, side="right")) - 1
            local = g - int(self.cum[sym_pos])
            meta = self.symbols[sym_pos]
            start = meta["offset"] + local
        x = np.array(self.feats[start:start + self.window], dtype=np.float32)
        ords = np.array(self.days[start:start + self.window], dtype=np.int32)
        x = kd.normalize_window(x, self.lookback_window, self.clip)
        stamp = kd.stamp_for_ordinals(ords)
        return x, stamp

    def __getitem__(self, _idx: int):
        x, stamp = self._draw()
        if self._torch is None:
            return x, stamp
        torch = self._torch
        return torch.from_numpy(x), torch.from_numpy(stamp)


# Backwards-compatible alias for the deleted `_WindowDataset` name.
_WindowDataset = LeanWindowDataset


def _resolve_device():
    import torch

    return torch.device("cuda" if torch.cuda.is_available() else "cpu")


def _save_last(path: Path, *, epoch: int, total_epochs: int, model,
               optimizer, scheduler, best_val_loss: float) -> None:
    import torch

    path.parent.mkdir(parents=True, exist_ok=True)
    torch.save(
        {
            "epoch": epoch,  # next epoch to run
            "total_epochs": total_epochs,
            "model_state": model.state_dict(),
            "optim_state": optimizer.state_dict(),
            "sched_state": scheduler.state_dict(),
            "best_val_loss": best_val_loss,
        },
        str(path),
    )


def _load_last(path: Path, *, model, optimizer, scheduler,
               total_epochs: int) -> tuple[int, float] | None:
    """Returns (start_epoch, best_val_loss), or None if no usable checkpoint."""
    import torch

    if not path.exists():
        return None
    try:
        dev = str(next(model.parameters()).device)
    except Exception:
        dev = "cpu"
    try:
        ckpt = torch.load(str(path), map_location=dev, weights_only=False)
    except Exception as exc:
        log.warning(f"Kronos-finetune | ignoring unreadable checkpoint {path} ({exc})")
        return None
    if ckpt.get("total_epochs") != total_epochs:
        log.warning(
            f"Kronos-finetune | checkpoint was for {ckpt.get('total_epochs')} epochs, "
            f"requested {total_epochs} — restarting weights from it with a fresh schedule"
        )
        try:
            model.load_state_dict(ckpt["model_state"])
        except Exception as exc:
            log.warning(f"Kronos-finetune | weight reload failed ({exc}) — from base")
            return None
        return 0, float(ckpt.get("best_val_loss", float("inf")))
    try:
        model.load_state_dict(ckpt["model_state"])
        optimizer.load_state_dict(ckpt["optim_state"])
        scheduler.load_state_dict(ckpt["sched_state"])
    except Exception as exc:
        log.warning(f"Kronos-finetune | state reload failed ({exc}) — from base")
        return None
    return int(ckpt.get("epoch", 0)), float(ckpt.get("best_val_loss", float("inf")))


def smoke_benchmark(
    dataset_dir: str | Path,
    *,
    lookback: int = 256,
    predict_len: int = 10,
    clip: float = 5.0,
    batch_size: int = 16,
    seed: int = 42,
    smoke_steps: int = 100,
    model=None,
    tokenizer=None,
    kind: str = "pipe",
) -> dict[str, Any]:
    """Measure real per-step time → projected epoch/total wall clock.

    `kind="pipe"` times the data pipeline only (dataloader + H2D copy) — a
    floor. `kind="tokenizer"` / `"predictor"` run the actual forward +
    backward (no optimizer step) and are the numbers §7 wants published.
    Callers pass the already-loaded models so weight-load time is excluded.
    """
    import torch
    from torch.utils.data import DataLoader

    device = _resolve_device()
    ds = LeanWindowDataset(dataset_dir, lookback, predict_len, clip, seed,
                           n_iter=max(smoke_steps, batch_size), split="train")
    loader = DataLoader(ds, batch_size=batch_size, num_workers=0, drop_last=True)

    def _pipe_step(x, _s):
        return x.float().mean()

    def _tok_step(x, _s):
        import torch.nn.functional as _F

        x = x.to(device)
        zs, bsq_loss, _, _ = model(x)
        z_pre, z = zs
        loss = (_F.mse_loss(z_pre, x) + _F.mse_loss(z, x) + bsq_loss) / 2
        loss.backward()
        model.zero_grad(set_to_none=True)
        return loss

    def _pred_step(x, x_stamp):
        x, x_stamp = x.to(device), x_stamp.to(device)
        with torch.no_grad():
            tok0, tok1 = tokenizer.encode(x, half=True)
        logits = model(tok0[:, :-1], tok1[:, :-1], x_stamp[:, :-1, :])
        loss, _, _ = model.head.compute_loss(
            logits[0], logits[1], tok0[:, 1:], tok1[:, 1:])
        loss.backward()
        model.zero_grad(set_to_none=True)
        return loss

    step_fn = {"pipe": _pipe_step, "tokenizer": _tok_step,
               "predictor": _pred_step}[kind]
    if model is not None and kind != "pipe":
        model.train()
    t0 = time.perf_counter()
    steps = 0
    for x, x_stamp in loader:
        if kind == "pipe":
            with torch.no_grad():
                _ = step_fn(x.to(device), x_stamp)
        else:
            _ = step_fn(x, x_stamp)
        steps += 1
        if steps >= smoke_steps:
            break
    if device.type == "cuda":
        torch.cuda.synchronize()
    dt = time.perf_counter() - t0
    per_step = dt / max(1, steps)
    steps_per_epoch = 2000
    return {
        "device": str(device),
        "kind": kind,
        "smoke_steps": steps,
        "smoke_seconds": dt,
        "secs_per_step": per_step,
        "projected_epoch_minutes": per_step * steps_per_epoch / 60,
        "projected_10epoch_hours": per_step * steps_per_epoch * 10 / 3600,
    }


def _fmt_eta(seconds: float) -> str:
    """H:MM:SS (or M:SS) remaining — the number the operator watches."""
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def _log_smoke(label: str, bench: dict[str, Any]) -> None:
    log.info(
        f"Kronos-finetune | smoke[{bench['kind']}] {label}: "
        f"{bench['secs_per_step']:.3f}s/step on {bench['device']} → "
        f"~{bench['projected_epoch_minutes']:.1f} min/epoch, "
        f"~{bench['projected_10epoch_hours']:.2f} h/10 epochs"
    )


def _train_tokenizer(
    dataset_dir: Path, device, epochs: int, batch_size: int,
    lr: float, lookback: int, predict_len: int, clip: float, seed: int,
    *,
    resume: bool = False,
    smoke_steps: int = 0,
) -> Path:
    import torch
    import torch.nn.functional as F
    from torch.utils.data import DataLoader

    from model import KronosTokenizer

    init_path = TOKENIZER_OUT if (resume and TOKENIZER_OUT.exists()) else TOKENIZER_PATH
    log.info(f"Kronos-finetune | tokenizer: loading weights from {init_path}")
    model = KronosTokenizer.from_pretrained(str(init_path)).to(device)

    if smoke_steps:
        _log_smoke(
            "tokenizer",
            smoke_benchmark(dataset_dir, lookback=lookback,
                            predict_len=predict_len, clip=clip,
                            batch_size=batch_size, seed=seed,
                            smoke_steps=smoke_steps,
                            model=model, kind="tokenizer"),
        )

    train_ds = LeanWindowDataset(dataset_dir, lookback, predict_len, clip, seed,
                                 n_iter=2000 * batch_size, split="train")
    val_ds = LeanWindowDataset(dataset_dir, lookback, predict_len, clip, seed,
                               n_iter=400 * batch_size, split="val")
    train_loader = DataLoader(train_ds, batch_size=batch_size, num_workers=0, drop_last=True)
    val_loader = DataLoader(val_ds, batch_size=batch_size, num_workers=0)

    optimizer = torch.optim.AdamW(model.parameters(), lr=lr, weight_decay=0.1)
    scheduler = torch.optim.lr_scheduler.OneCycleLR(
        optimizer, max_lr=lr, steps_per_epoch=len(train_loader), epochs=epochs,
        pct_start=0.03, div_factor=10,
    )

    best_val_loss = float("inf")
    start_epoch = 0
    if resume:
        loaded = _load_last(TOKENIZER_LAST, model=model, optimizer=optimizer,
                            scheduler=scheduler, total_epochs=epochs)
        if loaded is not None:
            start_epoch, best_val_loss = loaded
            log.info(
                f"Kronos-finetune | tokenizer resumed at epoch {start_epoch + 1}/{epochs} "
                f"(best val_loss={best_val_loss:.4f})"
            )
    TOKENIZER_OUT.parent.mkdir(parents=True, exist_ok=True)

    steps_per_epoch = len(train_loader)
    steps_total = steps_per_epoch * epochs
    t_run = time.perf_counter()
    for epoch in range(start_epoch, epochs):
        model.train()
        train_ds.set_epoch_seed(epoch)
        t_ep = time.perf_counter()
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
                now = time.perf_counter()
                ep_avg = (now - t_ep) / (step + 1)
                done = epoch * steps_per_epoch + (step + 1)
                run_avg = (now - t_run) / done
                log.info(
                    f"Kronos-finetune | tokenizer epoch {epoch + 1}/{epochs} "
                    f"step {step + 1}/{steps_per_epoch} loss={loss.item():.4f} "
                    f"{ep_avg:.3f}s/step "
                    f"epoch-ETA {_fmt_eta(ep_avg * (steps_per_epoch - step - 1))} "
                    f"run-ETA {_fmt_eta(run_avg * (steps_total - done))}"
                )

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
            log.info(
                f"Kronos-finetune | tokenizer checkpoint saved "
                f"(val_loss={best_val_loss:.4f}) -> {TOKENIZER_OUT}"
            )
        _save_last(TOKENIZER_LAST, epoch=epoch + 1, total_epochs=epochs,
                   model=model, optimizer=optimizer, scheduler=scheduler,
                   best_val_loss=best_val_loss)

    return TOKENIZER_OUT


def _train_predictor(
    dataset_dir: Path, tokenizer_path: Path, device,
    epochs: int, batch_size: int, lr: float, lookback: int, predict_len: int,
    clip: float, seed: int, *,
    resume: bool = False,
    smoke_steps: int = 0,
) -> Path:
    import torch
    from torch.utils.data import DataLoader

    from model import Kronos, KronosTokenizer

    init_pred = PREDICTOR_OUT if (resume and PREDICTOR_OUT.exists()) else MODEL_PATH
    log.info(
        f"Kronos-finetune | predictor: loading weights from {init_pred}, "
        f"tokenizer from {tokenizer_path}"
    )
    tokenizer = KronosTokenizer.from_pretrained(str(tokenizer_path)).to(device)
    tokenizer.eval()
    model = Kronos.from_pretrained(str(init_pred)).to(device)

    if smoke_steps:
        _log_smoke(
            "predictor",
            smoke_benchmark(dataset_dir, lookback=lookback,
                            predict_len=predict_len, clip=clip,
                            batch_size=batch_size, seed=seed,
                            smoke_steps=smoke_steps,
                            model=model, tokenizer=tokenizer,
                            kind="predictor"),
        )

    train_ds = LeanWindowDataset(dataset_dir, lookback, predict_len, clip, seed,
                                 n_iter=2000 * batch_size, split="train")
    val_ds = LeanWindowDataset(dataset_dir, lookback, predict_len, clip, seed,
                               n_iter=400 * batch_size, split="val")
    train_loader = DataLoader(train_ds, batch_size=batch_size, num_workers=0, drop_last=True)
    val_loader = DataLoader(val_ds, batch_size=batch_size, num_workers=0)

    optimizer = torch.optim.AdamW(model.parameters(), lr=lr, betas=(0.9, 0.95), weight_decay=0.1)
    scheduler = torch.optim.lr_scheduler.OneCycleLR(
        optimizer, max_lr=lr, steps_per_epoch=len(train_loader), epochs=epochs,
        pct_start=0.03, div_factor=10,
    )

    best_val_loss = float("inf")
    start_epoch = 0
    if resume:
        loaded = _load_last(PREDICTOR_LAST, model=model, optimizer=optimizer,
                            scheduler=scheduler, total_epochs=epochs)
        if loaded is not None:
            start_epoch, best_val_loss = loaded
            log.info(
                f"Kronos-finetune | predictor resumed at epoch {start_epoch + 1}/{epochs} "
                f"(best val_loss={best_val_loss:.4f})"
            )
    PREDICTOR_OUT.parent.mkdir(parents=True, exist_ok=True)

    def _step_loss(x, x_stamp):
        with torch.no_grad():
            tok0, tok1 = tokenizer.encode(x, half=True)
        tok_in = [tok0[:, :-1], tok1[:, :-1]]
        tok_out = [tok0[:, 1:], tok1[:, 1:]]
        logits = model(tok_in[0], tok_in[1], x_stamp[:, :-1, :])
        loss, _, _ = model.head.compute_loss(logits[0], logits[1], tok_out[0], tok_out[1])
        return loss

    steps_per_epoch = len(train_loader)
    steps_total = steps_per_epoch * epochs
    t_run = time.perf_counter()
    for epoch in range(start_epoch, epochs):
        model.train()
        train_ds.set_epoch_seed(epoch)
        t_ep = time.perf_counter()
        for step, (x, x_stamp) in enumerate(train_loader):
            x, x_stamp = x.to(device), x_stamp.to(device)
            loss = _step_loss(x, x_stamp)

            optimizer.zero_grad()
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), max_norm=3.0)
            optimizer.step()
            scheduler.step()

            if (step + 1) % 50 == 0:
                now = time.perf_counter()
                ep_avg = (now - t_ep) / (step + 1)
                done = epoch * steps_per_epoch + (step + 1)
                run_avg = (now - t_run) / done
                log.info(
                    f"Kronos-finetune | predictor epoch {epoch + 1}/{epochs} "
                    f"step {step + 1}/{steps_per_epoch} loss={loss.item():.4f} "
                    f"{ep_avg:.3f}s/step "
                    f"epoch-ETA {_fmt_eta(ep_avg * (steps_per_epoch - step - 1))} "
                    f"run-ETA {_fmt_eta(run_avg * (steps_total - done))}"
                )

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
            log.info(
                f"Kronos-finetune | predictor checkpoint saved "
                f"(val_loss={best_val_loss:.4f}) -> {PREDICTOR_OUT}"
            )
        _save_last(PREDICTOR_LAST, epoch=epoch + 1, total_epochs=epochs,
                   model=model, optimizer=optimizer, scheduler=scheduler,
                   best_val_loss=best_val_loss)

    return PREDICTOR_OUT


def run_kronos_finetune(
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
    *,
    base_tokenizer: bool = False,
    all_symbols: bool = True,
    train_cutoff: str = kd.DEFAULT_TRAIN_CUTOFF,
    max_stale_days: int | None = None,
    dataset_dir: str | Path = DATASET_DIR,
    smoke_steps: int = 0,
    resume: bool = False,
    force_rebuild: bool = False,
) -> None:
    import torch

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    log.info(
        f"Kronos-finetune | device={device} all_symbols={all_symbols} "
        f"lookback={lookback} predict_len={predict_len} batch_size={batch_size} "
        f"tokenizer_epochs={tokenizer_epochs} predictor_epochs={predictor_epochs} "
        f"cutoff={train_cutoff}"
    )
    if device.type == "cpu":
        log.warning("Kronos-finetune | running on CPU — extremely slow, install CUDA torch first")
    if not all_symbols:
        log.warning(
            "Kronos-finetune | all_symbols=False is legacy (top-n liquid path removed); "
            "continuing with the lean full-universe corpus"
        )
    _ = n_symbols  # legacy knob; universe size changes sampling diversity, not step count

    d = prepare_dataset(
        train_cutoff=train_cutoff, min_bars=min_bars,
        max_stale_days=max_stale_days, dataset_dir=dataset_dir,
        force_rebuild=force_rebuild,
    )

    tokenizer_path = Path(TOKENIZER_PATH)
    if skip_tokenizer and not base_tokenizer and TOKENIZER_OUT.exists():
        tokenizer_path = TOKENIZER_OUT
        log.info(f"Kronos-finetune | predictor: reusing fine-tuned tokenizer {tokenizer_path}")
    if not skip_tokenizer:
        tokenizer_path = _train_tokenizer(
            d, device, tokenizer_epochs, batch_size, tokenizer_lr,
            lookback, predict_len, clip, seed,
            resume=resume, smoke_steps=smoke_steps,
        )

    predictor_path = _train_predictor(
        d, tokenizer_path, device, predictor_epochs, batch_size,
        predictor_lr, lookback, predict_len, clip, seed,
        resume=resume, smoke_steps=smoke_steps,
    )

    print()
    print("=" * 60)
    print("  Kronos fine-tune complete")
    print("=" * 60)
    print(f"  tokenizer -> {tokenizer_path}")
    print(f"  predictor -> {predictor_path}")
    print("  Run scripts/kronos_eval_test.py --use-finetuned to compare accuracy.")
    print()
