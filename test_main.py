"""CLI entry point: an operator interrupt is a clean stop, not a traceback."""

from __future__ import annotations

import asyncio

import pytest


def test_cancelled_entry_point_stops_quietly(monkeypatch):
    """Ctrl-C cancels the running task; that must not surface as a crash."""
    import main as main_module

    async def cancelled(_args):
        raise asyncio.CancelledError()

    monkeypatch.setattr(main_module, "main", cancelled)
    assert main_module._run_async_entry(object()) == 130


def test_keyboard_interrupt_stops_quietly(monkeypatch):
    import main as main_module

    async def interrupted(_args):
        raise KeyboardInterrupt()

    monkeypatch.setattr(main_module, "main", interrupted)
    assert main_module._run_async_entry(object()) == 130


def test_a_normal_run_reports_success(monkeypatch):
    import main as main_module

    ran = []

    async def ok(args):
        ran.append(args)

    monkeypatch.setattr(main_module, "main", ok)
    marker = object()
    assert main_module._run_async_entry(marker) == 0
    assert ran == [marker]


def test_real_failures_still_propagate(monkeypatch):
    import main as main_module

    async def boom(_args):
        raise RuntimeError("real failure")

    monkeypatch.setattr(main_module, "main", boom)
    with pytest.raises(RuntimeError, match="real failure"):
        main_module._run_async_entry(object())
