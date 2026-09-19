"""P10 desktop Patterns dialog: same shared facade and rules as the web tab.

The dialog is driven offscreen (an X display is required); it talks to the real
durable services with a deterministic provider and sandbox double. A display-free
test also asserts the desktop and web build the same edit request.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

BARCA = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "barcache"
PATTERN = "pattern_003_double_bottom"
SYMBOL = "CDNS"

requires_display = pytest.mark.skipif(
    not os.environ.get("DISPLAY"),
    reason="the desktop dialog needs an X display (run under Xvfb)")


def rows():
    from data.barcache import load as load_barcache

    candles = load_barcache("us", SYMBOL, root=BARCA)
    return [[c.timestamp.isoformat(), float(c.open), float(c.high), float(c.low),
             float(c.close), float(c.volume or 0.0)] for c in candles]


@pytest.fixture
def desktop_editor(published_pattern_catalog, edit_doubles, sandbox_double):
    from core.backtest_service import BacktestService
    from core.pattern_edit_service import PatternEditService
    from core.pattern_editor_api import PatternEditor

    FakeProvider, Runner = edit_doubles
    store = published_pattern_catalog
    provider = FakeProvider()
    service = PatternEditService(
        store, provider=provider, runner=Runner(store.root),
        backtests=BacktestService(store, dataset_root=BARCA), dataset_root=BARCA)
    return PatternEditor(service=service), service, provider, store


def base_version(store) -> str:
    from core.pattern_versions import PatternVersions

    return next(p["active"] for p in PatternVersions(store).catalog()
                if p["id"] == PATTERN)


def pump(root, predicate, timeout=240):
    deadline = time.time() + timeout
    while time.time() < deadline:
        root.update()
        if predicate():
            return True
        time.sleep(0.05)
    return False


# ── display-free: one request builder for both frontends ─────────────────
def test_desktop_and_web_build_the_same_edit_request(desktop_editor):
    from core.backtest_params import settings_from_values
    from core.pattern_editor_api import edit_request_from_values

    editor, _service, _provider, store = desktop_editor
    values = {
        "mode": "historical-stream", "timeframe": "1d", "market": "us",
        "symbols": ["CDNS"], "universe": None,
        "start_date": rows()[30][0][:10], "end_date": rows()[70][0][:10],
        "session_count": 0, "warmup_bars": 30, "initial_capital": 100000.0,
        "sizing_mode": "fixed-notional", "position_notional": 10000.0,
        "txn_cost_pct": 0.001, "slippage_pct": 0.0, "pattern_only": False,
        "volume_gate": False, "kronos_gate": False, "kronos_rank": False,
        "collect_first": 0, "end_policy": "keep-open",
    }
    base = base_version(store)
    desktop = edit_request_from_values(
        editor, values, pattern_id=PATTERN, base_version_id=base,
        instruction="Require two closes above the neckline.", preset_name="desktop smoke")

    web_settings = settings_from_values({
        **{k: v for k, v in values.items() if k not in ("symbols", "universe")},
        "symbols": "CDNS",
    })
    assert desktop.preset.settings == web_settings
    assert desktop.pattern_id == PATTERN and desktop.base_version_id == base
    assert desktop.instruction.strip()


# ── offscreen workflow ───────────────────────────────────────────────────
@requires_display
def test_desktop_dialog_submits_and_renders_results(desktop_editor):
    import tkinter as tk
    from ui.patterns_dialog import PatternsDialog

    editor, service, provider, store = desktop_editor
    root = tk.Tk()
    root.withdraw()
    dialog = PatternsDialog(root, editor=editor)
    try:
        assert pump(root, lambda: bool(dialog._patterns)), "catalog did not load"
        label = next(k for k, v in dialog._pattern_values.items() if v == PATTERN)
        dialog._pattern_combo.set(label)
        dialog._on_pattern_change()
        assert pump(root, lambda: bool(dialog._versions)), "versions did not load"
        assert service.store is store
        assert pump(root, lambda: "USD" in dialog._balance_var.get()), \
            f"balance not shown: {dialog._balance_var.get()!r}"

        dialog._instruction.insert("1.0", "Require two closes above the neckline.")
        dialog._preset_name.insert(0, "desktop smoke")
        dialog._vars["symbols"].set(SYMBOL)
        dialog._vars["start_date"].set(rows()[30][0][:10])
        dialog._vars["end_date"].set(rows()[70][0][:10])
        dialog._vars["warmup_bars"].set(30.0)
        dialog._submit()

        assert pump(root, lambda: bool(dialog.job_id)), \
            f"job was not created: {dialog._status_var.get()!r}"
        job_id = dialog.job_id
        assert pump(root, lambda: service.status(job_id)["state"] in (
            "completed", "failed", "cancelled", "blocked", "interrupted"))
        assert service.status(job_id)["state"] == "completed"
        assert pump(root, lambda: "Candidate:" in dialog._comparison_text.get("1.0", tk.END))
        detail = service.detail(job_id)
        assert detail["explanation"] == "Tightened the entry rule."
        # the dialog never auto-promotes a generated version
        assert base_version(store) == next(
            v["version_id"] for v in dialog._versions if v["number"] == 1)
        assert provider.calls == 1
    finally:
        dialog._on_close()
        root.destroy()


@requires_display
def test_desktop_closing_during_job_does_not_change_it(desktop_editor, monkeypatch):
    import tkinter as tk
    from ui.patterns_dialog import PatternsDialog

    editor, service, _provider, store = desktop_editor
    monkeypatch.setattr(editor, "_start", lambda job_id: None)  # keep the job queued
    root = tk.Tk()
    root.withdraw()
    dialog = PatternsDialog(root, editor=editor)
    try:
        assert pump(root, lambda: bool(dialog._patterns))
        label = next(k for k, v in dialog._pattern_values.items() if v == PATTERN)
        dialog._pattern_combo.set(label)
        dialog._on_pattern_change()
        assert pump(root, lambda: bool(dialog._versions))

        dialog._instruction.insert("1.0", "Require two closes above the neckline.")
        dialog._preset_name.insert(0, "desktop close")
        dialog._vars["symbols"].set(SYMBOL)
        dialog._vars["start_date"].set(rows()[30][0][:10])
        dialog._vars["end_date"].set(rows()[70][0][:10])
        dialog._vars["warmup_bars"].set(30.0)
        dialog._submit()
        assert pump(root, lambda: bool(dialog.job_id))
        job_id = dialog.job_id

        dialog._on_close()  # closing the window must not cancel/complete the job
        for _ in range(10):
            root.update()
            time.sleep(0.02)
        assert service.status(job_id)["state"] == "queued"
    finally:
        root.destroy()


@requires_display
def test_desktop_reconnects_to_a_durable_job_after_reopen(desktop_editor, monkeypatch):
    import tkinter as tk
    from ui.patterns_dialog import PatternsDialog

    editor, service, _provider, store = desktop_editor
    monkeypatch.setattr(editor, "_start", lambda job_id: None)
    root = tk.Tk()
    root.withdraw()
    first = PatternsDialog(root, editor=editor)
    try:
        assert pump(root, lambda: bool(first._patterns))
        label = next(k for k, v in first._pattern_values.items() if v == PATTERN)
        first._pattern_combo.set(label)
        first._on_pattern_change()
        assert pump(root, lambda: bool(first._versions))
        first._instruction.insert("1.0", "Reconnect check.")
        first._preset_name.insert(0, "desktop reconnect")
        first._vars["symbols"].set(SYMBOL)
        first._vars["start_date"].set(rows()[30][0][:10])
        first._vars["end_date"].set(rows()[70][0][:10])
        first._vars["warmup_bars"].set(30.0)
        first._submit()
        assert pump(root, lambda: bool(first.job_id))
        job_id = first.job_id
    finally:
        first._on_close()

    reopened = PatternsDialog(root, editor=editor, start_job_id=job_id)
    try:
        assert reopened.job_id == job_id
        assert pump(root, lambda: "queued" in reopened._status_var.get())
        assert service.status(job_id)["state"] == "queued"
    finally:
        reopened._on_close()
        root.destroy()


@requires_display
def test_desktop_shows_postgresql_error_without_crashing(published_pattern_catalog):
    import tkinter as tk
    from core.pattern_editor_api import PatternEditor
    from core.pattern_edit_store import EditStore
    from ui.patterns_dialog import PatternsDialog

    # a broken DSN: the dialog must surface an actionable error, never fall back
    bad = EditStore(dsn="postgresql:///missing?host=/tmp/pattern-editor-pg/socket&port=1")
    root = tk.Tk()
    root.withdraw()
    dialog = PatternsDialog(root, editor=PatternEditor(store=bad))
    try:
        assert pump(root, lambda: "unavailable" in dialog._status_var.get()
                    or "Error" in dialog._status_var.get())
    finally:
        dialog._on_close()
        root.destroy()


@requires_display
def test_desktop_generation_conflict_when_default_changed_elsewhere(desktop_editor):
    """A default changed from another frontend produces a reload, not an overwrite."""
    import tkinter as tk
    from ui.patterns_dialog import PatternsDialog

    editor, service, _provider, store = desktop_editor
    root = tk.Tk()
    root.withdraw()
    dialog = PatternsDialog(root, editor=editor)
    try:
        assert pump(root, lambda: bool(dialog._patterns))
        label = next(k for k, v in dialog._pattern_values.items() if v == PATTERN)
        dialog._pattern_combo.set(label)
        dialog._on_pattern_change()
        assert pump(root, lambda: bool(dialog._versions))
        stale_generation = dialog._current_generation()

        # another actor (e.g. the web tab) changes the pattern default first
        import time as _time
        from core.pattern_versions import PatternVersions
        from core.pattern_editor_contracts import DefaultChange

        versions = PatternVersions(store)
        other = next(v["version_id"] for v in dialog._versions if v["number"] == 1)
        PatternVersions(store).set_default(DefaultChange(
            pattern_id=PATTERN, version_id=other,
            expected_generation=stale_generation, idempotency_key=f"other-{_time.time()}"))

        dialog._set_default()
        assert pump(root, lambda: dialog._status_var.get().startswith("Conflict"))
    finally:
        dialog._on_close()
        root.destroy()
