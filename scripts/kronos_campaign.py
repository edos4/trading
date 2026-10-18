"""
scripts/kronos_campaign.py — entire Kronos fine-tune campaign in one command.

Runs P0 (env) → P1 (dataset) → smoke → P2/P3 (training) → P4 (base +
fine-tuned eval + verdict) with GPU whenever CUDA is available. Every stage
is idempotent: completed work is detected and skipped, so re-running resumes
the campaign instead of redoing it.

    # Full campaign, detached (survives SSH disconnects):
    systemd-run --user --unit=kronos-campaign --collect \\
      --working-directory=/home/engine-33ai/codes/trading \\
      /home/engine-33ai/codes/trading/.venv/bin/python scripts/kronos_campaign.py

    # Foreground (small terminal test):
    .venv/bin/python scripts/kronos_campaign.py --epochs 1 --symbols 5

    # Skip stages:
    .venv/bin/python scripts/kronos_campaign.py --skip-env --skip-eval

Run this script with the APP venv (.venv) — it shells out to .venv-kronos
for the GPU stages itself. Needs the repo root as cwd (or --root).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

DEFAULT_ROOT = Path(__file__).resolve().parents[1]
KRONOS_REPO = Path.home() / "Kronos"
KRONOS_SHA = "67b630e67f6a18c9e9be918d9b4337c960db1e9a"


def sh(cmd: list[str], *, cwd: Path, log_path: Path) -> None:
    """Run cmd foreground, streaming to console AND the progress log."""
    print(f"$ {' '.join(cmd)}", flush=True)
    with open(log_path, "a", encoding="utf-8") as fh:
        fh.write(f"\n$ {' '.join(cmd)}\n")
        fh.flush()
        proc = subprocess.Popen(
            cmd, cwd=str(cwd), stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, bufsize=1,
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
            fh.write(line)
        proc.wait()
    if proc.returncode != 0:
        raise SystemExit(f"campaign | FAILED ({proc.returncode}): {' '.join(cmd)}")


def vram_free_mib() -> int | None:
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    try:
        return min(int(x) for x in out.stdout.strip().split() if x.strip().isdigit())
    except ValueError:
        return None


def preflight(args) -> dict:
    """Fail fast on anything that would waste GPU hours. Returns env facts."""
    facts: dict = {"root": str(args.root), "kronos_py": str(args.root / ".venv-kronos" / "bin" / "python")}
    free = vram_free_mib()
    facts["vram_free_mib"] = free
    if free is None:
        raise SystemExit("campaign | no NVIDIA GPU visible (nvidia-smi failed) — aborting")
    print(f"campaign | VRAM free: {free} MiB", flush=True)
    if free < 10_000 and not args.ignore_vram:
        raise SystemExit(
            f"campaign | only {free} MiB VRAM free — stop the model server first "
            "(`systemctl --user stop freetoken.service`) or pass --ignore-vram"
        )
    avail = shutil.disk_usage(args.root).free // (1024 ** 3)
    print(f"campaign | disk free at repo: {avail} GiB", flush=True)
    if avail < 10:
        raise SystemExit("campaign | <10 GiB disk free — aborting")
    return facts


def ensure_env(args, facts: dict, log_path: Path) -> None:
    """P0: .venv-kronos + CUDA torch + ~/Kronos + weights. Skips what exists."""
    root = args.root
    kpy = root / ".venv-kronos" / "bin" / "python"
    pip = root / ".venv-kronos" / "bin" / "pip"
    if not kpy.exists():
        sh([sys.executable, "-m", "venv", ".venv-kronos"], cwd=root, log_path=log_path)
    else:
        print("campaign | .venv-kronos exists — skipping creation", flush=True)

    out = subprocess.run([str(kpy), "-c",
                          "import torch; print(torch.cuda.is_available())"],
                         capture_output=True, text=True, timeout=120)
    if "True" not in out.stdout:
        sh([str(pip), "install", "torch>=2.10",
            "--index-url", "https://download.pytorch.org/whl/cu128"],
           cwd=root, log_path=log_path)
        sh([str(pip), "install", "-r", "requirements-kronos.txt"],
           cwd=root, log_path=log_path)
        sh([str(pip), "install", "numpy", "pandas", "matplotlib"],
           cwd=root, log_path=log_path)
        out = subprocess.run(
            [str(kpy), "-c",
             "import torch; assert torch.cuda.is_available(); "
             "print(torch.cuda.get_device_capability())"],
            capture_output=True, text=True, timeout=120)
        if out.returncode != 0:
            raise SystemExit(f"campaign | CUDA check failed:\n{out.stdout}\n{out.stderr}")
        print(f"campaign | CUDA OK {out.stdout.strip()}", flush=True)
    else:
        print("campaign | torch+CUDA present — skipping installs", flush=True)

    if not (KRONOS_REPO / "model").exists():
        sh(["git", "clone", "--depth", "1",
            "https://github.com/shiyu-coder/Kronos.git", str(KRONOS_REPO)],
           cwd=root, log_path=log_path)
    else:
        print("campaign | ~/Kronos exists — skipping clone", flush=True)

    weights = [KRONOS_REPO / "weights" / "Kronos-base" / "model.safetensors",
               KRONOS_REPO / "weights" / "Kronos-Tokenizer-base" / "model.safetensors"]
    if not all(w.exists() for w in weights):
        helper = (
            "import sys; sys.path.insert(0, %r);"
            "from model import Kronos, KronosTokenizer;"
            "KronosTokenizer.from_pretrained('NeoQuasar/Kronos-Tokenizer-base')"
            ".save_pretrained('%s/weights/Kronos-Tokenizer-base');"
            "Kronos.from_pretrained('NeoQuasar/Kronos-base')"
            ".save_pretrained('%s/weights/Kronos-base')"
            % (str(KRONOS_REPO), str(KRONOS_REPO), str(KRONOS_REPO))
        )
        sh([str(kpy), "-c", helper], cwd=root, log_path=log_path)
    else:
        print("campaign | weights cached — skipping fetch", flush=True)
    facts["kronos_py"] = str(kpy)


def run_finetune(args, facts: dict, log_path: Path) -> None:
    """P1–P3 via scripts/kronos_finetune.py (dataset cached ⇒ resumes training)."""
    cmd = [facts["kronos_py"], "scripts/kronos_finetune.py",
           "--all-symbols", "--epochs", str(args.epochs),
           "--train-cutoff", args.train_cutoff, "--min-bars", str(args.min_bars),
           "--batch-size", str(args.batch_size), "--lookback", str(args.lookback)]
    if args.smoke_steps:
        cmd += ["--smoke-steps", str(args.smoke_steps)]
    if args.resume:
        cmd.append("--resume")
    if args.skip_tokenizer:
        cmd.append("--skip-tokenizer")
    if args.predictor_epochs is not None:
        cmd += ["--predictor-epochs", str(args.predictor_epochs)]
    if args.predictor_lr is not None:
        cmd += ["--predictor-lr", str(args.predictor_lr)]
    sh(cmd, cwd=args.root, log_path=log_path)


def run_eval(args, facts: dict, log_path: Path) -> Path:
    """P4: base + fine-tuned eval over identical windows; prints verdict table."""
    outs = {}
    for tag, extra in (("base", []), ("ft", ["--use-finetuned"])):
        out = args.eval_out / f"kronos_{tag}.md"
        js = args.eval_out / f"kronos_{tag}.json"
        sh([facts["kronos_py"], "scripts/kronos_eval_test.py",
            "--symbols", str(args.symbols), "--liquid-only",
            "--sample-count", str(args.sample_count),
            "--start-date", args.eval_start, "--train-cutoff", args.train_cutoff,
            "--out", str(out), "--json-out", str(js)] + extra,
           cwd=args.root, log_path=log_path)
        outs[tag] = js
    base = json.loads(outs["base"].read_text(encoding="utf-8"))
    ft = json.loads(outs["ft"].read_text(encoding="utf-8"))
    assert base["config"]["symbols"] == ft["config"]["symbols"], "window mismatch!"
    lines = ["", "=" * 72, "  CAMPAIGN VERDICT — identical windows, "
             f"n={base['score_3d']['n']}", "=" * 72]
    rows = []
    for key, label in (("score_1d", "+1d"), ("score_3d", "+3d"), ("gate", "gate@3%")):
        b, f = base[key], ft[key]
        rows.append((label,
                     f"{b.get('directional_accuracy_pct', float('nan')):.1f}%",
                     f"{f.get('directional_accuracy_pct', float('nan')):.1f}%",
                     f"{b.get('mae_pct', float('nan')):.2f}%",
                     f"{f.get('mae_pct', float('nan')):.2f}%"))
    lines.append(f"  {'slice':8s} {'base_dir':>9s} {'ft_dir':>9s} {'base_mae':>9s} {'ft_mae':>9s}")
    for r in rows:
        lines.append("  %-8s %9s %9s %9s %9s" % r)
    lines += ["=" * 72,
              f"  reports: {args.eval_out}/kronos_base.md + kronos_ft.md",
              f"  full log: {log_path}", ""]
    print("\n".join(lines), flush=True)
    with open(log_path, "a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return log_path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    ap.add_argument("--epochs", type=int, default=10)
    ap.add_argument("--predictor-epochs", type=int, default=None)
    ap.add_argument("--predictor-lr", type=float, default=None)
    ap.add_argument("--train-cutoff", default="2026-01-01")
    ap.add_argument("--min-bars", type=int, default=500)
    ap.add_argument("--batch-size", type=int, default=16)
    ap.add_argument("--lookback", type=int, default=256)
    ap.add_argument("--smoke-steps", type=int, default=100)
    ap.add_argument("--symbols", type=int, default=50)
    ap.add_argument("--sample-count", type=int, default=3)
    ap.add_argument("--eval-start", default="2026-06-01")
    ap.add_argument("--eval-out", type=Path, default=Path("/tmp"))
    ap.add_argument("--skip-env", action="store_true")
    ap.add_argument("--skip-eval", action="store_true")
    ap.add_argument("--skip-tokenizer", action="store_true")
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--ignore-vram", action="store_true")
    ap.add_argument("--preflight-only", action="store_true")
    args = ap.parse_args(argv)
    args.eval_out.mkdir(parents=True, exist_ok=True)
    log_path = args.eval_out / "kronos_campaign.log"

    print("campaign | progress: tail -f logs/bot.log | grep -E 'smoke|epoch|val_loss|complete|CAMPAIGN'",
          flush=True)
    print(f"campaign | full transcript: {log_path}", flush=True)
    facts = preflight(args)
    if args.preflight_only:
        print("campaign | preflight OK", flush=True)
        return 0
    if not args.skip_env:
        ensure_env(args, facts, log_path)
    else:
        facts["kronos_py"] = str(args.root / ".venv-kronos" / "bin" / "python")
    run_finetune(args, facts, log_path)
    if not args.skip_eval:
        run_eval(args, facts, log_path)
    print("campaign | DONE", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
