"""Load patterns_v2/patterns without replacing this repo's patterns package.

Those modules are the technical-pattern-detector patterns. They are loaded as
``tpd_patterns`` so ``import patterns`` keeps meaning the built-in setups.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import threading
from pathlib import Path

_PKG = "tpd_patterns"
_LOCK = threading.Lock()
_DETECT = None


def detector_root() -> Path:
    """Directory that contains the Patternsv2 ``patterns`` package."""
    env = os.environ.get("PATTERNS_V2_ROOT", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    return Path(__file__).resolve().parent


def detect_fn():
    """Return ``detect(high, low, close)`` from patterns_v2/patterns."""
    global _DETECT
    if _DETECT is not None:
        return _DETECT
    with _LOCK:
        if _DETECT is not None:
            return _DETECT
        root = detector_root()
        init = root / "patterns" / "__init__.py"
        if not init.is_file():
            raise FileNotFoundError(
                f"Patternsv2 patterns not found at {init}. "
                "Expected patterns_v2/patterns, or set PATTERNS_V2_ROOT to a "
                "technical-pattern-detector checkout."
            )
        spec = importlib.util.spec_from_file_location(
            _PKG,
            init,
            submodule_search_locations=[str(init.parent)],
        )
        if spec is None or spec.loader is None:
            raise ImportError(f"Cannot load Patternsv2 from {init}")
        module = importlib.util.module_from_spec(spec)
        module.__package__ = _PKG
        module.__path__ = [str(init.parent)]
        sys.modules[_PKG] = module
        try:
            spec.loader.exec_module(module)
        except ModuleNotFoundError as exc:
            sys.modules.pop(_PKG, None)
            missing = exc.name or "dependency"
            raise ImportError(
                f"Patternsv2 needs {missing}. Install it in this environment "
                f"(the detector uses numpy and scipy)."
            ) from exc
        except Exception:
            sys.modules.pop(_PKG, None)
            raise
        _DETECT = module.detect
        return _DETECT
