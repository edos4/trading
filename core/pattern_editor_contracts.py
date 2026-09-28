"""Storage-independent contracts for the PostgreSQL pattern editor.

No settings, database, detector, or UI imports: these models are safe to pass to
spawned workers and serialize at API boundaries. Frozen models/tuples describe
immutable snapshots, including snapshots of mutable lifecycle rows. Repository
transactions must enforce cross-record invariants; model validation cannot.
Source bytes live in content blobs, never in paths supplied by a caller.
"""
from __future__ import annotations

from datetime import date
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import (
    AwareDatetime, BaseModel, ConfigDict, Field, StrictBool, StrictFloat,
    StrictInt, StrictStr, model_validator,
)

Identifier = Annotated[str, Field(min_length=1, max_length=200)]
Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
NonNegativeInt = Annotated[int, Field(strict=True, ge=0)]
PositiveInt = Annotated[int, Field(strict=True, gt=0)]
Scalar = StrictStr | StrictBool | StrictInt | StrictFloat | None


class Contract(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


class Parameter(Contract):
    """Named effective setting; consumers validate supported names and ranges."""
    name: Identifier
    value: Scalar


class ContentRef(Contract):
    sha256: Sha256
    size_bytes: NonNegativeInt
    media_type: str


class SourceFile(Contract):
    path: str
    content: ContentRef
    role: Literal["detector", "documentation", "helper", "trusted-runtime"]

    @model_validator(mode="after")
    def relative_path(self):
        from pathlib import PurePosixPath
        path = PurePosixPath(self.path)
        if (not self.path or self.path == "." or path.is_absolute()
                or ".." in path.parts or "\\" in self.path
                or str(path) != self.path):
            raise ValueError("Source path must be a normalized relative path")
        return self


class RuntimeIdentity(Contract):
    python_tag: str
    engine_sha256: Sha256
    packages: tuple[Parameter, ...]
    trusted_files: tuple[SourceFile, ...]


class PatternMetadata(Contract):
    class_name: Identifier
    module_name: Identifier
    timeframes: tuple[str, ...]
    min_bars: PositiveInt
    horizon_bars: PositiveInt
    max_open_per_symbol: PositiveInt | None
    skipped: bool


class PatternRecord(Contract):
    pattern_id: Identifier
    display_name: str
    enabled: bool
    default_version_id: Identifier | None  # None only while unpublished.
    generation: NonNegativeInt
    published: bool


class ProviderProvenance(Contract):
    provider: Literal["deepseek"] = "deepseek"
    requested_model: str = "deepseek-flash"
    returned_model: str | None
    request_id: str | None


class PatternVersion(Contract):
    version_id: Identifier
    pattern_id: Identifier
    number: PositiveInt
    parent_version_id: Identifier | None
    created_at: AwareDatetime
    actor: Literal["file-import", "ai-edit"]
    source_path: str
    files: tuple[SourceFile, ...]
    content_sha256: Sha256
    runtime: RuntimeIdentity
    metadata: PatternMetadata | None  # Invalid AI versions may have none.
    instruction: str | None
    explanation: str
    provider: ProviderProvenance | None
    import_batch_id: Identifier | None
    edit_job_id: Identifier | None


class ImportEntry(Contract):
    pattern_id: Identifier
    metadata: PatternMetadata
    configured_disabled: bool
    files: tuple[SourceFile, ...]


class ImportManifest(Contract):
    batch_id: Identifier
    captured_at: AwareDatetime
    git_commit: str | None
    git_dirty: bool
    snapshot_sha256: Sha256
    runtime: RuntimeIdentity
    entries: tuple[ImportEntry, ...]


class ValidationState(StrEnum):
    PENDING = "pending"
    PASSED = "passed"
    FAILED = "failed"
    BLOCKED = "blocked"


class VersionLifecycle(Contract):
    version_id: Identifier
    generation: NonNegativeInt
    validation: ValidationState
    validation_report_id: Identifier | None
    baseline_verification_report_id: Identifier | None
    successful_backtest_run_id: Identifier | None
    archived_at: AwareDatetime | None


class ReplayWindow(Contract):
    start_date: date
    end_date: date | None = None
    session_count: PositiveInt | None = None
    warmup_bars: NonNegativeInt

    @model_validator(mode="after")
    def bounds(self):
        if (self.end_date is None) == (self.session_count is None):
            raise ValueError("Specify exactly one of end_date or session_count")
        if self.end_date is not None and self.end_date < self.start_date:
            raise ValueError("end_date precedes start_date")
        return self


class ExecutionSettings(Contract):
    initial_capital: Annotated[float, Field(gt=0)]
    sizing_mode: Literal["fixed-notional", "paper-risk"]
    position_notional: Annotated[float, Field(gt=0)]
    txn_cost_pct: Annotated[float, Field(ge=0, lt=1)]
    slippage_pct: Annotated[float, Field(ge=0, lt=1)]
    pattern_only: bool
    volume_gate: bool
    kronos_gate: bool
    kronos_rank: bool
    collect_first: NonNegativeInt
    end_policy: Literal["keep-open", "force-close", "offline-legacy"]
    # Freeze all additional engine/risk/gate settings; never reread globals mid-run.
    parameters: tuple[Parameter, ...] = ()


class BacktestSettings(Contract):
    mode: Literal["offline", "historical-stream"]
    market: Literal["us", "ph"]
    timeframe: Literal["1d"] = "1d"
    symbols: tuple[Identifier, ...] = ()
    universe: Identifier | None = None
    window: ReplayWindow | None = None
    execution: ExecutionSettings

    @model_validator(mode="after")
    def supported_combination(self):
        if not self.symbols and self.universe is None:
            raise ValueError("Specify symbols or a universe")
        if len(set(self.symbols)) != len(self.symbols):
            raise ValueError("Duplicate symbols")
        if self.mode == "historical-stream":
            if self.window is None or self.execution.end_policy == "offline-legacy":
                raise ValueError("Stream requires a replay window and stream end policy")
        elif self.window is not None or self.execution.end_policy != "offline-legacy":
            raise ValueError("Offline mode uses its existing full-tape/end policy")
        return self


class BacktestPreset(Contract):
    preset_id: Identifier
    generation: NonNegativeInt
    name: str
    settings: BacktestSettings


class VersionSelection(Contract):
    pattern_id: Identifier
    version_id: Identifier


class BacktestRequest(Contract):
    idempotency_key: Identifier
    versions: Annotated[tuple[VersionSelection, ...], Field(min_length=1)]
    preset: BacktestPreset
    retry_of_run_id: Identifier | None = None

    @model_validator(mode="after")
    def unique_patterns(self):
        ids = [v.pattern_id for v in self.versions]
        if len(ids) != len(set(ids)):
            raise ValueError("A run must select exactly one version per pattern")
        return self


class ChartCandle(Contract):
    time: str
    open: float
    high: float
    low: float
    close: float


class ChartEditContext(BaseModel):
    """Only actual chart data goes to the provider; forecasts are excluded."""
    model_config = ConfigDict(allow_inf_nan=False)
    symbol: Identifier
    pattern: Identifier
    pattern_version_id: Identifier | None = None
    market: Literal["us", "ph"]
    timeframe: Identifier = "1d"
    candles: Annotated[list[ChartCandle], Field(min_length=2, max_length=10000)]
    volume: Annotated[list[dict], Field(max_length=10000)] = []
    segments: Annotated[list[dict], Field(max_length=1000)] = []
    markers: Annotated[list[dict], Field(max_length=1000)] = []
    levels: Annotated[list[dict], Field(max_length=1000)] = []


class EditRequest(Contract):
    idempotency_key: Identifier
    pattern_id: Identifier
    base_version_id: Identifier
    instruction: Annotated[str, Field(min_length=1, max_length=16000)]
    preset: BacktestPreset
    chart_context: ChartEditContext | None = None

    @model_validator(mode="after")
    def usable_edit(self):
        if not self.instruction.strip():
            raise ValueError("Instruction is blank")
        if self.chart_context and self.chart_context.pattern != self.pattern_id:
            raise ValueError("Chart pattern does not match the selected pattern")
        if self.preset.settings.mode != "historical-stream":
            raise ValueError("Automatic edits require a historical-stream preset")
        return self


class DefaultChange(Contract):
    pattern_id: Identifier
    version_id: Identifier
    expected_generation: NonNegativeInt
    idempotency_key: Identifier


class ArchiveVersion(Contract):
    pattern_id: Identifier
    version_id: Identifier
    replacement_default_version_id: Identifier | None
    expected_generation: NonNegativeInt
    idempotency_key: Identifier


class JobState(StrEnum):
    QUEUED = "queued"
    GENERATING = "generating"
    VALIDATING = "validating"
    BACKTESTING = "backtesting"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    BLOCKED = "blocked"
    INTERRUPTED = "interrupted"


class ErrorCode(StrEnum):
    INVALID_REQUEST = "invalid-request"
    NOT_FOUND = "not-found"
    CONFLICT = "conflict"
    DATABASE_UNAVAILABLE = "database-unavailable"
    BOOTSTRAP_REQUIRED = "bootstrap-required"
    CORRUPT_CONTENT = "corrupt-content"
    RUNTIME_INCOMPATIBLE = "runtime-incompatible"
    VERSION_INELIGIBLE = "version-ineligible"
    SANDBOX_UNAVAILABLE = "sandbox-unavailable"
    PROVIDER_FAILED = "provider-failed"
    DATA_UNAVAILABLE = "data-unavailable"
    VALIDATION_FAILED = "validation-failed"
    EXECUTION_FAILED = "execution-failed"


class DomainError(Contract):
    code: ErrorCode
    message: str  # Safe user-facing message, never arbitrary stderr/DSNs.
    retryable: bool
    report_id: Identifier | None = None


class JobProgress(Contract):
    job_id: Identifier
    attempt: PositiveInt
    state: JobState
    completed_units: NonNegativeInt
    total_units: NonNegativeInt | None
    unit: Literal["stages", "symbols", "sessions"]
    updated_at: AwareDatetime
    version_id: Identifier | None
    run_id: Identifier | None
    error: DomainError | None

    @model_validator(mode="after")
    def progress_bounds(self):
        if self.total_units is not None and self.completed_units > self.total_units:
            raise ValueError("Progress exceeds total")
        return self


class FrozenRunInputs(Contract):
    request: BacktestRequest
    resolved_symbols: Annotated[tuple[Identifier, ...], Field(min_length=1)]
    dataset: ContentRef
    runtime: RuntimeIdentity
    effective_parameters: tuple[Parameter, ...]
    inputs_sha256: Sha256


class BacktestMetrics(Contract):
    trade_count: NonNegativeInt
    win_rate: Annotated[float, Field(ge=0, le=1)] | None
    realized_pnl: float
    unrealized_pnl: float
    net_pnl: float
    fees: Annotated[float, Field(ge=0)]
    max_drawdown_pct: Annotated[float, Field(ge=0)]
    open_position_count: NonNegativeInt
    currency: Literal["USD", "PHP"]


class BacktestResult(Contract):
    """Completed attempt only; failures/interruption are represented by job state.

    Artifact references point to immutable PostgreSQL JSON blobs. Detailed row
    schemas/adapters belong to P05; preserve current trade fields and provenance.
    """
    run_id: Identifier
    completed_at: AwareDatetime
    inputs: FrozenRunInputs
    metrics: BacktestMetrics
    trades: ContentRef
    signals: ContentRef
    equity_curve: ContentRef
    open_positions: ContentRef
    logs: ContentRef
    base_comparison_run_id: Identifier | None
