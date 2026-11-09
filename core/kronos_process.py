"""core/kronos_process.py — shared GPU subprocess plumbing for Kronos.

The app venv is deliberately torch-free, so every model call happens inside
``.venv-kronos`` via a script in ``scripts/``. The web Kronos tab and the
scanner confirmation gate share this module so one GPU lock covers both: two
model loads on a 16 GB card must never overlap.
"""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import tempfile
import threading
from pathlib import Path

from utils.logger import log

ROOT = Path(__file__).resolve().parent.parent
VENV_PYTHON = ROOT / ".venv-kronos" / "bin" / "python"

GPU_LOCK = threading.Lock()
GPU_LOCK_FILE = Path(tempfile.gettempdir()) / "kronos_gpu.lock"


class KronosUnavailable(RuntimeError):
    """The isolated venv or worker script is missing on this host."""


class KronosBusy(RuntimeError):
    """Another Kronos run already holds the GPU lock."""


class KronosError(RuntimeError):
    """The Kronos subprocess failed or returned unusable output."""


def available(script: Path | None = None) -> bool:
    """True when the Kronos venv (and, if given, the worker script) exists."""
    if not VENV_PYTHON.exists():
        return False
    return script is None or Path(script).exists()


def run_worker(
    script: Path,
    args: list[str],
    *,
    timeout_s: float,
    label: str = "kronos",
) -> dict:
    """Run one worker in ``.venv-kronos`` and return its JSON payload.

    ``args`` are appended after the script path; the caller must not pass
    ``--out`` — this function supplies a private temp file and reads it back.
    Raises KronosUnavailable / KronosBusy / KronosError.
    """
    script = Path(script)
    if not available(script):
        raise KronosUnavailable(
            f"Kronos is unavailable on this host: expected {VENV_PYTHON} and {script}."
        )
    if not GPU_LOCK.acquire(blocking=False):
        raise KronosBusy("A Kronos forecast is already running.")
    # The in-process lock does not cover a separate script (e.g. the Experiment 2
    # backfill) running at the same time, so also take an OS-level lock.
    lock_fh = None
    try:
        lock_fh = open(GPU_LOCK_FILE, "w")
        fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        if lock_fh is not None:
            lock_fh.close()
        GPU_LOCK.release()
        raise KronosBusy("A Kronos forecast is already running.")

    # Settings are read from .env by this process only; the worker is a plain
    # subprocess, so forward its device choice explicitly. An operator's
    # exported KRONOS_DEVICE wins over the .env value.
    env = os.environ.copy()
    env["KRONOS_GPU_LOCK_HELD"] = "1"
    if "KRONOS_DEVICE" not in env:
        try:
            from config import settings

            env["KRONOS_DEVICE"] = settings.kronos_device
        except Exception:
            env["KRONOS_DEVICE"] = "auto"

    tmp = tempfile.NamedTemporaryFile(
        prefix=f"{label}_", suffix=".json", delete=False,
    )
    tmp_path = Path(tmp.name)
    tmp.close()
    try:
        proc = subprocess.run(
            [str(VENV_PYTHON), str(script), *args, "--out", str(tmp_path)],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=timeout_s,
            env=env,
        )
        if proc.returncode != 0 or not tmp_path.exists() or tmp_path.stat().st_size == 0:
            detail = (proc.stderr or proc.stdout or "").strip().splitlines()
            tail = " | ".join(detail[-3:]) if detail else "no output"
            log.error(f"Kronos | {label} failed: {tail}")
            raise KronosError(f"Kronos {label} failed: {tail}")
        payload = json.loads(tmp_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise KronosError(f"Kronos {label} returned an unexpected payload.")
        return payload
    except subprocess.TimeoutExpired as exc:
        raise KronosError(f"Kronos {label} timed out after {timeout_s}s.") from exc
    except json.JSONDecodeError as exc:
        raise KronosError(f"Kronos {label} produced unreadable output.") from exc
    finally:
        if lock_fh is not None:
            try:
                fcntl.flock(lock_fh, fcntl.LOCK_UN)
                lock_fh.close()
            except OSError:
                pass
        GPU_LOCK.release()
        try:
            tmp_path.unlink()
        except OSError:
            pass


def locked_main(main):
    """Protect direct CLI invocations; worker subprocesses inherit the parent's lock."""
    if os.environ.get("KRONOS_GPU_LOCK_HELD") == "1":
        return main()
    from core.kronos_weekly import exclusive_lock
    with exclusive_lock(GPU_LOCK_FILE):
        return main()
