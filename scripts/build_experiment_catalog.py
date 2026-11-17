"""Bundle ignored indicator-vides writeups/plans for deployable experiments.

Run with .venv/bin/python scripts/build_experiment_catalog.py after editing
the archive. This copies documentation only; Markdown is never executable.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def build(root: Path = ROOT) -> dict:
    entries = []
    for path in sorted((root / "indicator-vides").rglob("writeup.md")):
        plan_path = path.with_name("33ai-integration.md")
        if not plan_path.is_file():
            raise ValueError(f"Missing integration plan: {path.parent.name}")
        writeup, plan = path.read_text(), plan_path.read_text()
        parts = re.split(r"^## (.+)\n", writeup, flags=re.M)
        sections = dict(zip(parts[1::2], parts[2::2]))
        overview = sections.get("What it does", sections.get("What the video does", "")).strip()
        gap_sections = [body.strip() for heading, body in sections.items()
                        if any(word in heading.lower() for word in (
                            "missing", "unknown", "unspecified", "ambigu", "not supplied",
                            "cannot", "remain", "limits", "limitations", "major omission"))]
        gaps = "\n\n".join(gap_sections)
        entries.append({
            "id": path.parent.name,
            "number": int(path.parent.name.split("-")[0]),
            "title": writeup.splitlines()[0].lstrip("# "),
            "source_path": str(path.relative_to(root)),
            "plan_path": str(plan_path.relative_to(root)),
            "writeup_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "plan_sha256": hashlib.sha256(plan_path.read_bytes()).hexdigest(),
            "overview": overview,
            "gaps": gaps,
            "writeup": writeup,
            "plan": plan,
        })
    if not entries:
        raise ValueError("No indicator writeups found")
    return {"schema_version": 1, "sources": entries}


if __name__ == "__main__":
    output = ROOT / "core/experiments/sources.json"
    manifest = build()
    output.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    print(f"Bundled {len(manifest['sources'])} source folders in {output}")
