"""Immutable Laya application contracts (docs/laya.md section 6-7).

These are application field names, NOT claims about the upstream Laya SDK
fields. Frozen Pydantic models: no extra fields, no NaN/Infinity. Editing
labels, question wording, language, model, or calibration creates a new
schema/policy identity and invalidates old policy calibration.
"""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import (
    AwareDatetime, BaseModel, ConfigDict, Field, StrictBool, StrictFloat,
    StrictInt, StrictStr, model_validator,
)

Identifier = Annotated[str, Field(min_length=1, max_length=200)]
Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
NonNegativeInt = Annotated[int, Field(strict=True, ge=0)]
Scalar = StrictStr | StrictBool | StrictInt | StrictFloat | None

LAYA_SCHEMA_VERSION = 1
LAYA_FEATURE_SCHEMA_VERSION = "laya-features-v1"
LAYA_APP_VERSION = "laya-app-v1"

LayaMode = Literal["off", "research", "shadow", "filter"]
LayaOutcome = Literal["allow", "veto", "abstain"]
LayaJobState = Literal[
    "queued", "loading", "running", "succeeded", "failed",
    "cancelled", "interrupted",
]


class Contract(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


class LayaQuestion(Contract):
    """One ordered, versioned question sent to the model."""

    qid: Identifier
    kind: Literal["choice", "score", "noul"]
    text: Annotated[str, Field(min_length=1, max_length=2000)]
    # For choice/score: ordered option labels. Pinned order — never reorder.
    options: tuple[str, ...] = ()
    # For score: ordered rubric levels, worst -> best.
    rubric: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _options_for_kind(self):
        if self.kind == "noul" and (self.options or self.rubric):
            raise ValueError("noul questions carry no options/rubric")
        if self.kind == "choice" and not self.options:
            raise ValueError("choice questions need ordered options")
        if self.kind == "score" and not self.rubric:
            raise ValueError("score questions need an ordered rubric")
        return self


# Two independent task families v1. Do not combine a long article and a full
# candidate snapshot into every call.
CANDIDATE_REVIEW_V1: tuple[LayaQuestion, ...] = (
    LayaQuestion(
        qid="evidence_alignment", kind="choice",
        text="Given ONLY the supplied candidate evidence, how does it align with the proposed direction?",
        options=("supports", "mixed", "conflicts", "insufficient"),
    ),
    LayaQuestion(
        qid="evidence_strength", kind="score",
        text="Rate the strength of the SUPPLIED evidence (not promised profitability).",
        rubric=("insufficient", "weak", "moderate", "strong"),
    ),
    LayaQuestion(
        qid="material_conflict", kind="noul",
        text="Does the supplied evidence explicitly contradict the proposed direction?",
    ),
)

EVENT_REVIEW_V1: tuple[LayaQuestion, ...] = (
    LayaQuestion(
        qid="event_type", kind="choice",
        text="What event does the source describe for the named issuer? Keep answers short.",
        options=("earnings", "guidance", "financing", "corporate_action",
                  "regulatory", "operations", "other", "insufficient"),
    ),
    LayaQuestion(
        qid="directional_tone", kind="choice",
        text="Tone of the source relative to the named issuer.",
        options=("positive", "negative", "mixed", "neutral", "insufficient"),
    ),
    LayaQuestion(
        qid="material_risk_mentioned", kind="noul",
        text="Does the source explicitly describe a material adverse event?",
    ),
)

TASK_PRESETS: dict[str, dict] = {
    "candidate-review-v1": {
        "task_id": "candidate-review-v1",
        "input_mode": "candidate",
        "questions": CANDIDATE_REVIEW_V1,
        "description": "Review supplied candidate evidence; do not infer missing news.",
    },
    "event-review-v1": {
        "task_id": "event-review-v1",
        "input_mode": "text",
        "questions": EVENT_REVIEW_V1,
        "description": "Classify a pasted announcement for the named issuer.",
    },
}


class LayaDocument(Contract):
    source_id: Identifier
    issuer_symbol: Identifier | None = None
    title: Annotated[str, Field(max_length=500)] = ""
    publisher: Annotated[str, Field(max_length=200)] = ""
    language: Annotated[str, Field(min_length=1, max_length=16)] = "en"
    text_sha256: Sha256
    text_bytes: NonNegativeInt
    publication_time: AwareDatetime | None = None
    available_at: AwareDatetime | None = None
    ingested_at: AwareDatetime | None = None
    revision_id: Identifier | None = None
    operator_supplied: bool = True


class LayaCandidate(Contract):
    direction: Literal["BUY", "SELL"]
    detector: Identifier
    pattern_version_id: Identifier | None = None
    detector_confidence: Annotated[float, Field(ge=0, le=1)]
    entry: Annotated[float, Field(gt=0)] | None = None
    stop: Annotated[float, Field(gt=0)] | None = None
    target: Annotated[float, Field(gt=0)] | None = None
    reward_risk: float | None = None
    engine_eligibility: Literal["passed", "failed", "unknown"] = "unknown"


class LayaFeatures(Contract):
    schema_version: str = LAYA_FEATURE_SCHEMA_VERSION
    close_above_20d_mean: StrictBool | None = None
    volume_ratio_20d: float | None = None
    reward_risk: float | None = None
    volatility_bucket: Literal["low", "medium", "high"] | None = None
    trend: Annotated[str, Field(max_length=64)] = ""
    units: Annotated[str, Field(max_length=200)] = "price:ccy; ratios:unitless"
    lookback_bars: NonNegativeInt = 20
    missing: tuple[str, ...] = ()
    calculation_version: Annotated[str, Field(max_length=64)] = "laya-features-v1"


class LayaRuntime(Contract):
    package_version: Annotated[str, Field(max_length=64)] = "unpinned"
    model_id: Annotated[str, Field(max_length=300)] = "unpinned"
    model_revision: Annotated[str, Field(max_length=200)] = "unpinned"
    tokenizer_revision: Annotated[str, Field(max_length=200)] = "unpinned"
    routing_policy: Annotated[str, Field(max_length=200)] = "single-resident"
    calibration_id: Annotated[str, Field(max_length=200)] = "uncalibrated"
    backend: Annotated[str, Field(max_length=64)] = "cpu"
    source_sha: Annotated[str, Field(max_length=200)] = "unrecorded"
    artifact_hash: Annotated[str, Field(max_length=200)] = "unrecorded"


class LayaAssessmentInput(Contract):
    assessment_id: Identifier
    schema_version: NonNegativeInt = LAYA_SCHEMA_VERSION
    market: Literal["us", "ph"]
    symbol: Identifier
    exchange: Identifier
    timeframe: Literal["1d", "1W"] = "1d"
    candidate_id: Identifier | None = None
    signal_id: Identifier | None = None
    pattern_version_id: Identifier | None = None
    decision_at: AwareDatetime
    bar_close_at: AwareDatetime
    observed_at: AwareDatetime
    as_of: AwareDatetime
    candidate: LayaCandidate | None = None
    features: LayaFeatures = LayaFeatures()
    documents: tuple[LayaDocument, ...] = ()
    context_text: Annotated[str, Field(min_length=1, max_length=8000)]
    content_hash: Sha256
    feature_schema_version: str = LAYA_FEATURE_SCHEMA_VERSION
    question_schema_id: Identifier
    question_schema_hash: Sha256
    questions: tuple[LayaQuestion, ...]
    runtime: LayaRuntime = LayaRuntime()


class LayaAnswer(Contract):
    qid: Identifier
    kind: Literal["choice", "score", "noul"]
    # choice: winning option label; score: winning rubric label; noul: yes/no.
    answer: Annotated[str, Field(min_length=1, max_length=200)]
    # choice/score: distribution over options/rubric in pinned order.
    distribution: tuple[float, ...] = ()
    # Required for application thresholds. Entropy-derived `confidence` is
    # stored separately and NEVER silently substituted.
    answer_confidence: Annotated[float, Field(ge=0, le=1)] | None = None
    confidence: Annotated[float, Field(ge=0, le=1)] | None = None
    yes_probability: Annotated[float, Field(ge=0, le=1)] | None = None
    score_value: float | None = None


class LayaAssessmentResult(Contract):
    assessment_id: Identifier
    input_hash: Sha256
    answers: tuple[LayaAnswer, ...]
    raw: dict[str, Scalar | list | dict] | None = None  # raw SDK output, versioned
    raw_version: Annotated[str, Field(max_length=64)] = LAYA_APP_VERSION
    calibration_status: Literal["calibrated", "uncalibrated"] = "uncalibrated"
    elapsed_ms: NonNegativeInt = 0
    warnings: tuple[str, ...] = ()
    truncated: bool = False
    options_collapsed: bool = False

    @model_validator(mode="after")
    def _gate_eligible(self):
        if self.truncated or self.options_collapsed:
            # Preserved as data; policy layer must refuse gate eligibility.
            pass
        for a in self.answers:
            if a.answer_confidence is not None and not (0.0 <= a.answer_confidence <= 1.0):
                raise ValueError("answer_confidence out of bounds")
            for p in a.distribution:
                if not (0.0 <= p <= 1.0):
                    raise ValueError("distribution probability out of bounds")
        return self


class LayaPolicy(Contract):
    policy_id: Identifier
    version: NonNegativeInt = 1
    task_id: Identifier
    model_id: Annotated[str, Field(max_length=300)]
    calibration_id: Annotated[str, Field(max_length=200)] = "uncalibrated"
    markets: tuple[Literal["us", "ph"], ...] = ("us", "ph")
    # Thresholds are evaluation outputs, locked before final testing.
    # Candidate-review (default-closed gate: entries need affirmative support):
    min_answer_confidence: Annotated[float, Field(ge=0, le=1)] = 0.0
    require_alignment: tuple[str, ...] = ("supports",)
    veto_on_material_conflict_yes_prob: Annotated[float, Field(ge=0, le=1)] | None = None
    min_evidence_score: Annotated[str, Field(max_length=64)] | None = None
    coverage_floor: Annotated[float, Field(ge=0, le=1)] | None = None
    # Event-review (veto-only gate: entries are open by default and close only
    # on affirmative danger signals; below-threshold evidence allows with a
    # reason rather than abstaining, since absence of danger is the normal
    # state. Unvalidated starting points, not calibrated thresholds):
    veto_on_material_risk_yes_prob: Annotated[float, Field(ge=0, le=1)] = 0.6
    veto_financing_min_confidence: Annotated[float, Field(ge=0, le=1)] = 0.35


class LayaPolicyDecision(Contract):
    assessment_id: Identifier
    policy_id: Identifier
    candidate_id: Identifier | None = None
    book_id: Identifier | None = None
    run_id: Identifier | None = None
    mode: LayaMode
    outcome: LayaOutcome
    reasons: tuple[str, ...] = ()
    evaluated_at: AwareDatetime
    decision_hash: Sha256
