"""Pattern registry & AI pattern-editor tools."""

from __future__ import annotations

from typing import Any, Optional

from mcp_server import config as mcp_config
from mcp_server.api_client import get_client


def _selection_params(
    selected: Optional[dict[str, str]] = None,
    disabled: Optional[list[str]] = None,
) -> list[tuple[str, str]]:
    params: list[tuple[str, str]] = []
    for pattern_id, version_id in (selected or {}).items():
        params.append(("selected", f"{pattern_id}:{version_id}"))
    for name in disabled or []:
        params.append(("disabled", name))
    return params


def list_patterns() -> dict:
    """List the pattern catalog (id, display name, enabled, default version)."""
    return get_client().get("/api/patterns")


def get_patterns_pinned(
    selected: Optional[dict[str, str]] = None,
    disabled: Optional[list[str]] = None,
) -> dict:
    """Resolve eligibility-checked pinned versions for a pattern selection."""
    return get_client().get(
        "/api/patterns/pinned",
        params=_selection_params(selected, disabled),
    )


def get_patterns_bundle(
    selected: Optional[dict[str, str]] = None,
    disabled: Optional[list[str]] = None,
) -> dict:
    """Pinned versions plus the bytes of every file a loader must execute."""
    return get_client().get(
        "/api/patterns/bundle",
        params=_selection_params(selected, disabled),
    )


def list_pattern_versions(pattern_id: str, include_archived: bool = False) -> dict:
    """List the stored versions of one pattern."""
    return get_client().get(
        f"/api/patterns/{pattern_id}/versions",
        params={"include_archived": include_archived},
    )


def get_pattern_version(version_id: str) -> dict:
    """Detail for one stored pattern version."""
    return get_client().get(f"/api/patterns/versions/{version_id}")


def get_pattern_version_source(version_id: str) -> dict:
    """Source files for one stored pattern version."""
    return get_client().get(f"/api/patterns/versions/{version_id}/source")


def diff_pattern_version(version_id: str, base_version_id: Optional[str] = None) -> dict:
    """Unified diff of one version against its base."""
    params = {"base_version_id": base_version_id} if base_version_id else None
    return get_client().get(f"/api/patterns/versions/{version_id}/diff", params=params)


def get_pattern_version_bundle(version_id: str) -> dict:
    """Single-version execution bundle for a version that just landed."""
    return get_client().get(f"/api/patterns/versions/{version_id}/bundle")


def get_pattern_provider() -> dict:
    """AI provider configuration and account balance (informational)."""
    return get_client().get("/api/patterns/provider")


def submit_pattern_edit(
    pattern_id: str,
    base_version_id: str,
    instruction: str,
    settings: Optional[dict] = None,
    preset_id: Optional[str] = None,
    preset_name: Optional[str] = None,
    chart_context: Optional[dict] = None,
    idempotency_key: Optional[str] = None,
) -> dict:
    """Submit an AI edit against an explicit immutable base version.

    Returns {"job_id", "state"}; poll with get_pattern_edit_job. Repeat the same
    idempotency_key to safely retry.
    """
    body: dict[str, Any] = {
        "pattern_id": pattern_id,
        "base_version_id": base_version_id,
        "instruction": instruction,
    }
    for key, value in (
        ("settings", settings),
        ("preset_id", preset_id),
        ("preset_name", preset_name),
        ("chart_context", chart_context),
        ("idempotency_key", idempotency_key),
    ):
        if value is not None:
            body[key] = value
    return get_client().post("/api/patterns/edits", json=body)


def get_pattern_edit_job(job_id: str) -> dict:
    """Status/detail of an AI pattern edit job."""
    return get_client().get(f"/api/patterns/edits/{job_id}")


def cancel_pattern_edit(job_id: str) -> dict:
    """Cancel an AI pattern edit job."""
    return get_client().post(f"/api/patterns/edits/{job_id}/cancel")


def retry_pattern_edit(job_id: str) -> dict:
    """Retry a terminal AI pattern edit job; returns {"job_id", "state"}."""
    return get_client().post(f"/api/patterns/edits/{job_id}/retry")


def backtest_pattern_version(version_id: str, settings: dict) -> dict:
    """Backtest a specific pattern version with posted settings."""
    return get_client().post(
        f"/api/patterns/versions/{version_id}/backtest", json=settings,
    )


def get_pattern_run(run_id: str) -> dict:
    """Payload of a recorded pattern run."""
    return get_client().get(f"/api/patterns/runs/{run_id}")


def get_pattern_run_chart(run_id: str, detection: int) -> dict:
    """Chart for one detection within a recorded pattern run."""
    return get_client().get(
        f"/api/patterns/runs/{run_id}/chart", params={"detection": detection},
    )


def set_pattern_default(
    version_id: str,
    expected_generation: int,
    idempotency_key: Optional[str] = None,
) -> dict:
    """Promote a version to a pattern's default (destructive/optimistic-locked).

    expected_generation must match the pattern's current generation.
    """
    mcp_config.require_destructive()
    body: dict[str, Any] = {"expected_generation": expected_generation}
    if idempotency_key is not None:
        body["idempotency_key"] = idempotency_key
    return get_client().post(f"/api/patterns/versions/{version_id}/default", json=body)


def archive_pattern_version(
    version_id: str,
    expected_generation: int,
    replacement_default_version_id: Optional[str] = None,
    idempotency_key: Optional[str] = None,
) -> dict:
    """Archive a pattern version (destructive/optimistic-locked)."""
    mcp_config.require_destructive()
    body: dict[str, Any] = {"expected_generation": expected_generation}
    if replacement_default_version_id is not None:
        body["replacement_default_version_id"] = replacement_default_version_id
    if idempotency_key is not None:
        body["idempotency_key"] = idempotency_key
    return get_client().post(f"/api/patterns/versions/{version_id}/archive", json=body)
