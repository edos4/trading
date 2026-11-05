"""Entry permissions shared by the scanner and every paper entry path."""

from __future__ import annotations

from config import settings


def describe_short_entry_rejection(action: str, *, indicators_only: bool = False) -> str | None:
    """Apply the pattern-short switch without interfering with position exits."""
    if action == "SELL" and not indicators_only and not settings.short_entries_enabled:
        return (
            "Short entries are disabled (SHORT_ENTRIES_ENABLED=false). "
            "Set it true to re-enable SELL/short pattern entries."
        )
    return None
