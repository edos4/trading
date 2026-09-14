"""P0 wire contracts for the editor; persistence/runtime wiring starts in P1/P2.

JSON dictionaries are additive to existing payloads. TypedDict is documentation
and static typing, NOT input validation; service boundaries must validate in P3.
See docs/pattern-edit-contracts.md for identity and compatibility invariants.
"""
from __future__ import annotations

from typing import Literal, NotRequired, TypedDict

SCHEMA_VERSION = 1


class ArtifactRef(TypedDict):
    path: str  # registry-relative, allowlisted; never a client filesystem path
    sha256: str
    media_type: str
    size_bytes: int


class CandleIdentity(TypedDict):
    dataset_sha256: str
    symbol: str
    market: Literal["us", "ph"]
    timeframe: str
    timestamp: str  # timezone-aware ISO 8601; daily session label is separate
    session_date: str
    dataset_index: int  # absolute in frozen dataset, never viewport-relative


class SemanticAnchor(TypedDict):
    anchor_id: str  # stable role + occurrence, not translated display label
    role: str
    candle: CandleIdentity
    snap: Literal["open", "high", "low", "close", "price"]
    price: float
    origin: Literal["historical", "baseline", "candidate", "user"]


class DatasetManifest(TypedDict):
    schema_version: int
    candles: ArtifactRef
    indicators: ArtifactRef | None
    symbol: str
    market: Literal["us", "ph"]
    timeframe: str
    session_timezone: str
    start_timestamp: str
    end_timestamp: str
    detection_cutoff: CandleIdentity
    chart_cutoff: CandleIdentity
    scan_semantics: Literal["causal_prefix_v1", "legacy_full_history_v1"]
    external_inputs: list[ArtifactRef]  # e.g. frozen earnings data


class ExitHookRef(TypedDict):
    version_id: str
    artifact: ArtifactRef
    callable_name: str
    abi: Literal["exit_v1"]


class ResolvedRules(TypedDict):
    schema_version: int
    engine_sha256: str  # compatible trusted engine bundle; not current-file alias
    stage: Literal["signal", "fill"]
    action: Literal["BUY", "SELL", "CLOSE"]
    price: float
    qty: float
    entry_mode: Literal["signal_close", "next_bar_close"]
    max_open_per_symbol: int | None
    position_notional: float
    fractional_qty: bool
    lot_size: int
    txn_cost_pct: float
    slippage_pct: float
    stop_loss: float | None
    stop_loss_on_close: bool
    take_profit: float | None
    trailing_stop_pct: float | None
    trailing_stop_mode: Literal["highest_close", "lowest_close", "highest_high", "lowest_low", "highest_low", "lowest_high"] | None
    trailing_stop_on_close: bool
    stop_loss_pct_cap: float | None
    reclaim_exit: bool
    reclaim_lower_rail: list[float] | None
    exit_fill_at_close: bool
    trailing_ref_after_check: bool
    exit_order: Literal["stop_first", "trail_first"]
    neckline: float | None
    neckline_break_direction: Literal["below", "above"] | None
    exit_bars_after_neckline_break: int | None
    exit_bars_after_entry: int | None
    time_exit_only_unfavorable: bool
    time_exit_min_mfe_pct: float | None
    trailing_activation_pct: float | None
    exit_hook: ExitHookRef | None


class TradeProvenance(TypedDict):
    trade_id: str
    signal_id: str | None
    pattern_id: str
    pattern_version_id: str | None
    provenance: Literal["versioned", "legacy_unknown"]
    signal_candle: CandleIdentity | None
    requested_rules: ResolvedRules | None
    resolved_rules: ResolvedRules | None


class VersionManifest(TypedDict):
    schema_version: int
    pattern_id: str
    version_id: str
    version_number: int
    parent_version_id: str | None
    restored_from_version_id: str | None
    created_at: str
    actor: str
    description: str
    explanation: str
    source: ArtifactRef
    documentation: ArtifactRef
    dependencies: list[ArtifactRef]
    runtime_sha256: str
    python_abi: str
    base_interface_sha256: str
    candidate_sha256: str
    validation_report_id: str
    session_id: str


SessionState = Literal["draft", "generating", "validating", "ready", "needs-revision", "failed", "applying", "activation-pending", "applied", "cancelled", "discarded", "stale-base"]


class EditMessage(TypedDict):
    message_id: str
    role: Literal["user", "assistant"]
    text: str
    created_at: str
    anchors: list[SemanticAnchor]
    images: list[ArtifactRef]


class EditSession(TypedDict):
    schema_version: int
    session_id: str
    pattern_id: str
    trade: TradeProvenance
    base_version_id: str
    source_mirror_sha256: str
    dataset: DatasetManifest
    messages: list[EditMessage]
    state: SessionState
    current_revision_id: str | None
    job_ids: list[str]
    generation: int  # optimistic concurrency token for mutable session
    created_at: str
    updated_at: str


class CandidateRevision(TypedDict):
    schema_version: int
    revision_id: str
    session_id: str
    parent_revision_id: str | None
    base_version_id: str
    candidate_sha256: str
    context_sha256: str
    message_ids: list[str]
    files: list[ArtifactRef]
    patch: ArtifactRef
    description: str
    explanation: str
    anchor_expectations: list[SemanticAnchor]
    unresolved_questions: list[str]
    provider: Literal["deepseek"]
    requested_model: str
    returned_model: str | None
    provider_request_id: str | None
    created_at: str


class ValidationCheck(TypedDict):
    name: str
    required: bool
    outcome: Literal["passed", "failed", "unavailable", "not_run"]
    detail: str
    evidence: NotRequired[ArtifactRef]


class ValidationReport(TypedDict):
    schema_version: int
    report_id: str
    revision_id: str
    candidate_sha256: str
    dataset_sha256: str
    dependency_sha256: str
    validator_sha256: str
    tests_sha256: str
    checks: list[ValidationCheck]
    preview: ArtifactRef | None
    comparison: ArtifactRef | None  # None means Not run, never passed
    created_at: str


class ActivationStatus(TypedDict):
    activation_id: str
    pattern_id: str
    previous_version_id: str
    registered_version_id: str
    revision_id: str
    validation_report_id: str
    idempotency_key: str
    state: Literal["staged", "registered", "worker-pending", "active", "failed"]
    required_worker_ids: list[str]
    acknowledged_worker_ids: list[str]
    stopped_worker_ids: list[str]
    error: str | None
