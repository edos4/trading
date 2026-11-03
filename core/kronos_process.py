"""core/kronos_process.py — shared GPU subprocess plumbing for Kronos.

The app venv is deliberately torch-free, so every model call happens inside
``.venv-kronos`` via a script in ``scripts/``. The web Kronos tab and the
scanner confirmation gate share this module so one GPU lock covers both: two
model loads on a 16 GB card must never overlap.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import threading
from pathlib import Path

from utils.logger import log

ROOT = Path(__file__).resolve().parent.parent
VENV_PYTHON = ROOT / ".venv-kronos" / "bin" / "python"

GPU_LOCK = threading.Lock()


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
        GPU_LOCK.release()
        try:
            tmp_path.unlink()
        except OSError:
            pass
