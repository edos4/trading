"""Chart indicators discovered the same way the explorer discovers patterns."""

from __future__ import annotations

import importlib
from pathlib import Path

from indicators._signal import BaseIndicator


def discover() -> list[BaseIndicator]:
    found: list[BaseIndicator] = []
    for path in sorted(Path(__file__).parent.glob("*.py")):
        if path.name.startswith("_") or path.name.startswith("test"):
            continue
        module = importlib.import_module(f"indicators.{path.stem}")
        for obj in vars(module).values():
            if (
                isinstance(obj, type)
                and issubclass(obj, BaseIndicator)
                and obj is not BaseIndicator
                and obj.__module__ == module.__name__
            ):
                found.append(obj())
    return found
