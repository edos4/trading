"""Renders ``README.md`` for the Documentation tab.

Heading ids are the GitHub slugs (lowercase, punctuation dropped, spaces to
hyphens), so the in-README links like ``#configuration-env`` keep working here.

``markdown-it-py`` is imported lazily: it arrives transitively via ``rich``
today, but a clone that never reinstalled dependencies still serves every other
tab, and only this one degrades.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

README_PATH = Path(__file__).resolve().parent.parent / "README.md"

# Cache the parsed document until README.md changes on disk.
_cache: dict[str, Any] = {"mtime": None, "doc": None}

_PUNCT = re.compile(r"[^\w\s-]", re.UNICODE)
_SPACE = re.compile(r"\s+")


def _slugify(text: str) -> str:
    slug = _SPACE.sub("-", _PUNCT.sub("", text.strip().lower())).strip("-")
    return slug or "section"


def _inline_text(token: Any) -> str:
    """Heading text without inline markers (backticks, emphasis)."""
    parts = []
    for child in token.children or []:
        if child.type in ("text", "code_inline"):
            parts.append(child.content)
        elif child.type == "softbreak":
            parts.append(" ")
    return "".join(parts).strip()


def _tree(flat: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Nest headings by level so the TOC can render as nested lists."""
    root: list[dict[str, Any]] = []
    stack: list[tuple[int, list[dict[str, Any]]]] = [(0, root)]
    for entry in flat:
        while stack and stack[-1][0] >= entry["level"]:
            stack.pop()
        if not stack:
            stack = [(0, root)]
        stack[-1][1].append(entry)
        stack.append((entry["level"], entry["children"]))
    return root


def build_document(path: Optional[Path] = None) -> dict[str, Any]:
    """Parse the README once per change. Raises ImportError without markdown-it."""
    from markdown_it import MarkdownIt

    source = path or README_PATH
    mtime = source.stat().st_mtime
    if _cache["doc"] is not None and _cache["mtime"] == mtime:
        return _cache["doc"]

    text = source.read_text(encoding="utf-8")
    md = MarkdownIt("commonmark", {"html": False, "linkify": False, "typographer": False})
    md.enable(["table", "strikethrough"])

    tokens = md.parse(text)
    flat: list[dict[str, Any]] = []
    used: dict[str, int] = {}
    for index, token in enumerate(tokens):
        if token.type != "heading_open":
            continue
        title = _inline_text(tokens[index + 1])
        slug = _slugify(title)
        if slug in used:
            used[slug] += 1
            slug = f"{slug}-{used[slug]}"
        else:
            used[slug] = 0
        token.attrSet("id", slug)
        flat.append({
            "level": int(token.tag[1]),
            "title": title,
            "slug": slug,
            "children": [],
        })

    doc = {
        "html": md.renderer.render(tokens, md.options, {}),
        "toc": _tree(flat),
        "headings": len(flat),
        "path": source.name,
        "updated": datetime.fromtimestamp(mtime, timezone.utc).isoformat(),
        "updated_text": datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M"),
    }
    _cache["mtime"] = mtime
    _cache["doc"] = doc
    return doc
