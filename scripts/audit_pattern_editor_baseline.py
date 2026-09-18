"""Read-only P01 source inventory; prints JSON, never opens an editor registry.

This is an audit, not the P03 atomic bootstrap/import command. Run with the
project Python and required Settings environment (WATCHLIST, TV_SCREENER,
TV_EXCHANGE). Imports only trusted working-tree detector code for metadata.
"""
from __future__ import annotations

import ast
import hashlib
import importlib
import importlib.metadata
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def inventory():
    from config import DISABLED_PATTERNS
    from core.pattern_versions import collect_sources
    from patterns.base_pattern import BasePattern

    files = {}
    entries = []
    for source in sorted((ROOT / "patterns").glob("[0-9]*.py")):
        relative = source.relative_to(ROOT).as_posix()
        captured = collect_sources(ROOT, relative)
        for path, data in captured.items():
            if path in files and files[path] != data:
                raise RuntimeError("Source changed during audit: " + path)
            files[path] = data
        module = importlib.import_module("patterns." + source.stem)
        classes = [c for c in vars(module).values() if isinstance(c, type)
                   and c is not BasePattern and issubclass(c, BasePattern)
                   and c.__module__ == module.__name__]
        if len(classes) != 1:
            raise RuntimeError("Expected one detector: " + relative)
        detector = classes[0]()
        entries.append({
            "source": relative, "documentation": source.with_suffix(".md").relative_to(ROOT).as_posix(),
            "pattern_id": detector.name, "class_name": classes[0].__name__,
            "skipped": detector.skipped, "configured_disabled": detector.name in DISABLED_PATTERNS,
            "timeframes": detector.timeframes, "min_bars": getattr(detector, "MIN_BARS", 2),
            "horizon_bars": getattr(detector, "HORIZON_BARS", 5),
            "max_open_per_symbol": getattr(detector, "MAX_OPEN_PER_SYMBOL", None),
            "static_repository_dependencies": sorted(captured),
        })
    # collect_sources walks AST imports, but cannot resolve this dynamic mapping.
    rationale = ast.parse((ROOT / "patterns/_rationale.py").read_bytes())
    dynamic = next(ast.literal_eval(node.value) for node in rationale.body
                   if isinstance(node, ast.Assign)
                   and any(isinstance(t, ast.Name) and t.id == "_MODULES" for t in node.targets))
    for path, data in files.items():
        if (ROOT / path).read_bytes() != data:
            raise RuntimeError("Source changed during audit: " + path)
    tracked_changes = subprocess.check_output(
        ["git", "diff", "--name-only", "HEAD"], cwd=ROOT, text=True).splitlines()
    return {
        "purpose": "P01 audit only; not a verified or published bootstrap snapshot",
        "git_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "tracked_changed_paths": tracked_changes,
        "untracked_paths": subprocess.check_output(
            ["git", "ls-files", "--others", "--exclude-standard"], cwd=ROOT, text=True).splitlines(),
        "disabled_patterns": DISABLED_PATTERNS,
        "python_tag": sys.implementation.cache_tag,
        "packages": {p: importlib.metadata.version(p) for p in
                     ("numpy", "pandas", "pydantic", "psycopg", "pytest", "mcp", "tradingview-screener")},
        "entries": entries,
        "dynamic_rationale_modules": dynamic,
        "files": {path: {"sha256": hashlib.sha256(data).hexdigest(), "size_bytes": len(data)}
                  for path, data in sorted(files.items())},
    }


if __name__ == "__main__":
    print(json.dumps(inventory(), indent=2, sort_keys=True))
