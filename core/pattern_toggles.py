"""Enable/disable toggles for the v1 and patterns_v2 pattern detectors.

The Settings tab owns two comma-separated mute lists (``DISABLED_PATTERNS`` and
``DISABLED_PATTERNS_V2``); each list holds the patterns whose checkbox is left
unchecked. The v1 detectors take that list as a construction argument — the
scanner, backtester, explorer and paper books read
:func:`config.disabled_pattern_names` themselves — so a change lands on the next
scan or book start. patterns_v2 has no such argument: its detector package reads
a module-level ``DISABLED`` set at call time, so :func:`apply_patternsv2` writes
the live set into the loaded package.

Patterns that ship off cannot be switched on from the UI and are reported as
``forced``: a v1 detector whose class sets ``skipped`` (retired), a name in
``config.DISABLED_PATTERNS``, and the patterns_v2 names the detector package
itself excludes from every scan (``patterns_v2/patterns/common.py::DISABLED``).
Those stay muted whichever way the boxes are clicked, so the page never shows an
enabled pattern that the scanner will not run.

The catalogs come from the shipped source, never from the editor database —
the toggles must work on a host where PostgreSQL is down.
"""

from __future__ import annotations

import importlib
import re
from pathlib import Path

from config import (
    DISABLED_PATTERNS,
    disabled_pattern_names,
    disabled_patterns_v2_names,
)
from utils.logger import log

FIELD_V1 = "disabled_patterns"
FIELD_V2 = "disabled_patterns_v2"
FIELDS = (FIELD_V1, FIELD_V2)

_ROOT = Path(__file__).resolve().parent.parent

_V1_CACHE: list[dict] | None = None
_V2_CACHE: list[dict] | None = None
_V2_FORCED: frozenset[str] | None = None


def _label(name: str) -> str:
    """Readable label from a detector name ("inv_head_shoulders" included)."""
    text = re.sub(r"^inv\b", "inverse", name.replace("_", " ").strip())
    return text.title()


def _v1_catalog() -> list[dict]:
    """Every shipped ``patterns/NNN_*.py`` detector with its mute state."""
    global _V1_CACHE
    if _V1_CACHE is not None:
        return _V1_CACHE
    from patterns.base_pattern import BasePattern

    entries: list[dict] = []
    for source in sorted((_ROOT / "patterns").glob("[0-9]*.py")):
        try:
            module = importlib.import_module("patterns." + source.stem)
        except Exception:  # noqa: BLE001 - one broken detector must not hide the rest
            log.exception(f"Pattern toggles | could not import {source.name}")
            continue
        classes = [c for c in vars(module).values()
                   if isinstance(c, type) and c is not BasePattern
                   and issubclass(c, BasePattern) and c.__module__ == module.__name__]
        if len(classes) != 1:
            log.warning(f"Pattern toggles | {source.name} declares {len(classes)} detectors")
            continue
        detector = classes[0]()
        shipped = detector.name in DISABLED_PATTERNS
        entries.append({
            # The paired .md titles are inconsistent ("002 Double Top" vs
            # "Rounding Bottom Trading System"), so label from the source name.
            "id": detector.name,
            "label": _label(source.stem),
            "forced": bool(detector.skipped) or shipped,
            "reason": "Retired" if detector.skipped else ("Off by default" if shipped else ""),
        })
    _V1_CACHE = entries
    return entries


def _v2_module():
    """The loaded patterns_v2 detector package (raises when it cannot load)."""
    from patterns_v2 import loader

    return loader.detector_module()


def _v2_snapshot() -> tuple[list[dict], frozenset[str]]:
    """Catalog and the package's own mute set, captured before any apply.

    ``PATTERN_NAMES`` excludes the package's default-muted names and ``DISABLED``
    is the live set, so their union is every name the package can emit whichever
    toggles are in force. The shipped mute set is read exactly once, because
    :func:`apply_patternsv2` overwrites ``DISABLED`` in place.
    """
    global _V2_CACHE, _V2_FORCED
    module = _v2_module()
    if _V2_FORCED is None:
        _V2_FORCED = frozenset(str(n) for n in (getattr(module, "DISABLED", ()) or ()))
    if _V2_CACHE is None:
        names = set(getattr(module, "PATTERN_NAMES", ()) or ()) | set(_V2_FORCED)
        _V2_CACHE = [
            {
                "id": name,
                "label": _label(name),
                "forced": name in _V2_FORCED,
                "reason": "Off by default" if name in _V2_FORCED else "",
            }
            for name in sorted(names)
        ]
    return _V2_CACHE, _V2_FORCED


def catalog(field: str) -> list[dict]:
    """The toggle entries for one family: id, label, forced, reason."""
    if field == FIELD_V1:
        return _v1_catalog()
    if field == FIELD_V2:
        return _v2_snapshot()[0]
    raise KeyError(f"Unknown pattern toggle field: {field}")


def forced(field: str) -> list[str]:
    """Ids the page shows unchecked and disabled because they ship off."""
    return [entry["id"] for entry in catalog(field) if entry["forced"]]


def muted(field: str) -> list[str]:
    """Ids the family will skip right now, forced ones included."""
    if field == FIELD_V1:
        return sorted(set(forced(FIELD_V1)) | set(disabled_pattern_names()))
    if field == FIELD_V2:
        return sorted(set(forced(FIELD_V2)) | set(disabled_patterns_v2_names()))
    raise KeyError(f"Unknown pattern toggle field: {field}")


def apply(field: str | None = None) -> dict[str, list[str]]:
    """Push the toggles into the running process; returns the mutes per field.

    Only patterns_v2 needs pushing (the detector package reads a module global);
    v1 mutes are pulled by whoever constructs a scanner or backtester. Call at
    process start and after a Settings save.
    """
    fields = FIELDS if field is None else (field,)
    result: dict[str, list[str]] = {}
    for name in fields:
        result[name] = muted(name)
    if FIELD_V2 in fields:
        module = _v2_module()
        # The package's detect() reads its own module global on every call.
        setattr(module, "DISABLED", frozenset(result[FIELD_V2]))
    return result
