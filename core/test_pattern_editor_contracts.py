"""Boundary tests for immutable, portable editor requests; no database access."""
import pickle
from datetime import date

import pytest
from pydantic import ValidationError

from core.pattern_editor_contracts import (
    BacktestPreset, BacktestRequest, BacktestSettings, ContentRef, EditRequest,
    ExecutionSettings, Parameter, ReplayWindow, SourceFile, VersionSelection,
)


def settings():
    return BacktestSettings(
        mode="historical-stream", market="us", symbols=("AAPL",),
        window=ReplayWindow(start_date=date(2026, 1, 5), session_count=30, warmup_bars=121),
        execution=ExecutionSettings(
            initial_capital=100000, sizing_mode="fixed-notional", position_notional=10000,
            txn_cost_pct=0.001, slippage_pct=0, pattern_only=True, volume_gate=False,
            kronos_gate=False, kronos_rank=False, collect_first=0, end_policy="keep-open",
            parameters=(Parameter(name="session_timezone", value="America/New_York"),),
        ),
    )


def request():
    return BacktestRequest(
        idempotency_key="submit-1",
        versions=(VersionSelection(pattern_id="pattern_002_double_top", version_id="version-1"),),
        preset=BacktestPreset(preset_id="preset-1", generation=2, name="Control", settings=settings()),
    )


def test_request_json_and_worker_pickle_preserve_frozen_inputs():
    original = request()
    assert BacktestRequest.model_validate_json(original.model_dump_json()) == original
    assert pickle.loads(pickle.dumps(original)) == original
    with pytest.raises(ValidationError):
        original.preset.settings.execution.parameters[0].value = "UTC"
    with pytest.raises(TypeError):
        original.versions[0] = original.versions[0]


@pytest.mark.parametrize("window", [
    {"end_date": None, "session_count": None},
    {"end_date": "2026-02-01", "session_count": 10},
    {"end_date": "2026-01-01", "session_count": None},
    {"session_count": 0},
    {"session_count": True},
])
def test_replay_rejects_ambiguous_or_invalid_window(window):
    with pytest.raises(ValidationError):
        ReplayWindow(start_date="2026-01-05", warmup_bars=121, **window)


@pytest.mark.parametrize("changes", [
    {"window": None}, {"symbols": (), "universe": None},
    {"symbols": ("AAPL", "AAPL")}, {"timeframe": "1h"},
    {"unexpected_setting": 1},
])
def test_settings_reject_unsupported_requests(changes):
    payload = settings().model_dump()
    with pytest.raises(ValidationError):
        BacktestSettings.model_validate({**payload, **changes})


def test_pins_cannot_select_two_versions_of_same_pattern():
    payload = request().model_dump()
    payload["versions"] += ({"pattern_id": "pattern_002_double_top", "version_id": "version-2"},)
    with pytest.raises(ValidationError, match="one version per pattern"):
        BacktestRequest.model_validate(payload)


@pytest.mark.parametrize("path", ["/etc/passwd", "../pattern.py", "patterns/../a.py", "a\\b.py", ".", "a//b.py"])
def test_source_paths_cannot_escape_materialization(path):
    with pytest.raises(ValidationError):
        SourceFile(path=path, role="detector", content=ContentRef(
            sha256="a" * 64, size_bytes=1, media_type="text/plain"))


@pytest.mark.parametrize("bad_cost", [-0.1, 1, float("nan"), float("inf")])
def test_effective_costs_are_bounded_and_finite(bad_cost):
    payload = settings().execution.model_dump()
    with pytest.raises(ValidationError):
        ExecutionSettings.model_validate({**payload, "txn_cost_pct": bad_cost})


def test_edit_requires_instruction_and_stream_preset():
    payload = dict(idempotency_key="edit-1", pattern_id="pattern_002_double_top",
                   base_version_id="version-1", instruction="Confirm neckline", preset=request().preset)
    assert EditRequest(**payload).base_version_id == "version-1"
    with pytest.raises(ValidationError, match="blank"):
        EditRequest(**{**payload, "instruction": "  "})
    offline = settings().model_dump()
    offline.update(mode="offline", window=None)
    offline["execution"]["end_policy"] = "offline-legacy"
    preset = BacktestPreset(preset_id="offline", generation=0, name="Offline",
                            settings=BacktestSettings.model_validate(offline))
    with pytest.raises(ValidationError, match="historical-stream"):
        EditRequest(**{**payload, "preset": preset})
