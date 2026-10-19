"""
web/kronos.py — GPU forecast bridge for the web UI's Kronos tab.

The app venv (``.venv``) is deliberately torch-free, so the model only resolves
inside the isolated ``.venv-kronos`` venv. This module shells out to
``scripts/kronos_web_forecast.py`` there, one run at a time (two model loads on
a 16 GB GPU must not overlap), and returns the parsed JSON payload. One run
handles every requested symbol — each model is loaded once and reused — so a
long comma-separated symbol list still costs just the two loads.
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
FORECAST_SCRIPT = ROOT / "scripts" / "kronos_web_forecast.py"

MAX_DAYS = 60
MAX_SYMBOLS = 12
TIMEOUT_S = 900

# One forecast at a time: each run loads Kronos-base + tokenizer twice onto the
# GPU, and overlapping runs would thrash or OOM the card.
_LOCK = threading.Lock()


class KronosUnavailable(RuntimeError):
    """The isolated venv or forecast script is missing on this host."""


class KronosBusy(RuntimeError):
    """A forecast is already running."""


class KronosError(RuntimeError):
    """The forecast subprocess failed."""


def available() -> bool:
    return VENV_PYTHON.exists() and FORECAST_SCRIPT.exists()


def forecast(symbols: list[str], days: int) -> dict:
    """Run base + fine-tuned forecasts for every symbol; return the JSON payload.

    Raises KronosUnavailable / KronosBusy / KronosError.
    """
    if not available():
        raise KronosUnavailable(
            "Kronos forecasting is unavailable on this host: "
            f"expected {VENV_PYTHON} and {FORECAST_SCRIPT}."
        )
    if not _LOCK.acquire(blocking=False):
        raise KronosBusy("A Kronos forecast is already running.")
    tmp = tempfile.NamedTemporaryFile(
        prefix="kronos_web_", suffix=".json", delete=False,
    )
    tmp_path = Path(tmp.name)
    tmp.close()
    label = ",".join(symbols)
    try:
        proc = subprocess.run(
            [
                str(VENV_PYTHON), str(FORECAST_SCRIPT),
                "--symbols", *symbols, "--days", str(int(days)),
                "--out", str(tmp_path),
            ],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=TIMEOUT_S,
        )
        if proc.returncode != 0 or not tmp_path.exists() or tmp_path.stat().st_size == 0:
            detail = (proc.stderr or proc.stdout or "").strip().splitlines()
            tail = " | ".join(detail[-3:]) if detail else "no output"
            log.error(f"Kronos-web | {label} +{days}d failed: {tail}")
            raise KronosError(f"Kronos forecast failed: {tail}")
        payload = json.loads(tmp_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise KronosError("Kronos forecast returned an unexpected payload.")
        return payload
    except subprocess.TimeoutExpired as exc:
        raise KronosError(
            f"Kronos forecast timed out after {TIMEOUT_S}s."
        ) from exc
    except json.JSONDecodeError as exc:
        raise KronosError("Kronos forecast produced unreadable output.") from exc
    finally:
        _LOCK.release()
        try:
            tmp_path.unlink()
        except OSError:
            pass
