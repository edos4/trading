"""Entry permissions shared by the scanner and every paper entry path."""

from __future__ import annotations

from config import settings


def resolve_dii_gate_mode(mode: str | None, *, paper: bool) -> str:
    """Freeze a run's explicit choice, falling back to configured defaults."""
    if mode is None:
        mode = ("shadow" if settings.dii_gate_shadow else
                settings.paper_dii_gate_mode if paper else "filter")
    mode = mode.strip().lower()
    if mode not in ("shadow", "filter"):
        raise ValueError(f"Unsupported DII gate mode: {mode}")
    return mode


def describe_short_entry_rejection(action: str, *, indicators_only: bool = False) -> str | None:
    """Apply the pattern-short switch without interfering with position exits."""
    if action == "SELL" and not indicators_only and not settings.short_entries_enabled:
        return (
            "Short entries are disabled (SHORT_ENTRIES_ENABLED=false). "
            "Set it true to re-enable SELL/short pattern entries."
        )
    return None
