"""Stable stage-2 domain objects and closed enums."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

ROUND1_TEMPLATE_ID = "triage.r1.v3"
ROUND2_TEMPLATE_ID = "triage.r2.v2"

Decision = Literal["ANALYZE", "SKIP", "UNCERTAIN"]
Round2Decision = Literal["ANALYZE", "SKIP"]
FailureKind = Literal[
    "SCHEMA_FAILED",
    "ID_MISMATCH",
    "BUDGET_EXCEEDED",
    "PROVIDER_ERROR",
    "PROVIDER_TIMEOUT",
    "ASSEMBLY_FAILED",
]
TaskStatus = Literal["PENDING", "OK", "FAILED"]
AttemptStatus = Literal[
    "OK",
    "SCHEMA_FAILED",
    "ID_MISMATCH",
    "BUDGET_EXCEEDED",
    "PROVIDER_ERROR",
    "PROVIDER_TIMEOUT",
]

SKIP_REASONS = frozenset(
    {
        "S_WRAPPER",
        "S_PURE_COMPUTE",
        "S_ERROR_PATH",
        "S_RUNTIME_HELPER",
    }
)
ANALYZE_REASONS = frozenset(
    {
        "anti-analysis",
        "collection",
        "communication",
        "data-manipulation",
        "host-interaction",
        "impact",
        "linking",
        "load-code",
        "persistence",
        "other",
    }
)
UNCERTAIN_REASONS = frozenset(
    {
        "U_TRUNCATED",
        "U_CALLEE_OPAQUE",
        "U_INDIRECT",
        "U_CALLER_CONTEXT",
        "U_DATA_OPAQUE",
    }
)
ALL_REASONS = SKIP_REASONS | ANALYZE_REASONS | UNCERTAIN_REASONS
BINARY_REASONS = SKIP_REASONS | ANALYZE_REASONS

Reason = Literal[
    "S_WRAPPER",
    "S_PURE_COMPUTE",
    "S_ERROR_PATH",
    "S_RUNTIME_HELPER",
    "anti-analysis",
    "collection",
    "communication",
    "data-manipulation",
    "host-interaction",
    "impact",
    "linking",
    "load-code",
    "persistence",
    "other",
    "U_TRUNCATED",
    "U_CALLEE_OPAQUE",
    "U_INDIRECT",
    "U_CALLER_CONTEXT",
    "U_DATA_OPAQUE",
]
BinaryReason = Literal[
    "S_WRAPPER",
    "S_PURE_COMPUTE",
    "S_ERROR_PATH",
    "S_RUNTIME_HELPER",
    "anti-analysis",
    "collection",
    "communication",
    "data-manipulation",
    "host-interaction",
    "impact",
    "linking",
    "load-code",
    "persistence",
    "other",
]


def reason_matches_decision(decision: str, reason: str) -> bool:
    if decision == "SKIP":
        return reason in SKIP_REASONS
    if decision == "ANALYZE":
        return reason in ANALYZE_REASONS
    if decision == "UNCERTAIN":
        return reason in UNCERTAIN_REASONS
    return False


class FunctionVerdict(BaseModel):
    """A scheduling verdict. No evidence/behavior/relation fields are allowed."""

    model_config = ConfigDict(extra="forbid")

    ref: str = Field(pattern=r"^F-\d+$")
    decision: Decision
    reason: Reason
    note: str = Field(default="", max_length=120)

    @model_validator(mode="after")
    def validate_pair(self) -> "FunctionVerdict":
        if not reason_matches_decision(self.decision, self.reason):
            raise ValueError(
                "reason %s 与 decision %s 组配非法" % (self.reason, self.decision)
            )
        return self


class Round2FunctionVerdict(BaseModel):
    """Round-2 verdict schema: UNCERTAIN is structurally impossible."""

    model_config = ConfigDict(extra="forbid")

    ref: str = Field(pattern=r"^F-\d+$")
    decision: Round2Decision
    reason: BinaryReason
    note: str = Field(default="", max_length=120)

    @model_validator(mode="after")
    def validate_pair(self) -> "Round2FunctionVerdict":
        if not reason_matches_decision(self.decision, self.reason):
            raise ValueError(
                "reason %s 与 decision %s 组配非法" % (self.reason, self.decision)
            )
        return self

    def to_function_verdict(self) -> FunctionVerdict:
        return FunctionVerdict.model_validate(self.model_dump())


class FinalVerdict(BaseModel):
    """One authoritative Stage-2 terminal verdict for a Stage-1 UNDECIDED function."""

    model_config = ConfigDict(extra="forbid")

    ref: str = Field(pattern=r"^F-\d+$")
    final_decision: Round2Decision
    reason: BinaryReason
    note: str = Field(default="", max_length=120)
    source_round: Literal[1, 2]
    forced_analyze: bool = False
    failure_kind: FailureKind | None = None

    @model_validator(mode="after")
    def validate_terminal_state(self) -> "FinalVerdict":
        if not reason_matches_decision(self.final_decision, self.reason):
            raise ValueError(
                "reason %s 与 final_decision %s 组配非法"
                % (self.reason, self.final_decision)
            )
        if self.source_round == 1 and (self.forced_analyze or self.failure_kind is not None):
            raise ValueError("Round1 final verdict 不能带 forced_analyze/failure_kind")
        if self.forced_analyze and self.final_decision != "ANALYZE":
            raise ValueError("forced_analyze 只能对应 ANALYZE")
        return self


class Usage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    requests: int = Field(default=0, ge=0)
    latency_ms: int = Field(default=0, ge=0)

    def add(self, other: "Usage") -> "Usage":
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            requests=self.requests + other.requests,
            latency_ms=self.latency_ms + other.latency_ms,
        )


class TriageTask(BaseModel):
    """Terminal runtime state for one effective task."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    task_id: str
    parent_task_id: str | None = None
    unit_id: str
    round: Literal[1, 2]
    subject_refs: list[str]
    input_sha256: str
    template_id: str
    status: TaskStatus = "PENDING"
    failure_kind: FailureKind | None = None
    verdicts: list[FunctionVerdict] = Field(default_factory=list)
    model_id: str | None = None
    usage: Usage = Field(default_factory=Usage)
    attempts: int = Field(default=0, ge=0)
    error: str | None = None
    forced_analyze: bool = False


class TriageConfig(BaseModel):
    """Effective runtime configuration persisted without secret values."""

    model_config = ConfigDict(extra="forbid")

    # Round 1 model configuration. Existing field names are retained for compatibility.
    provider: str = "scripted"
    provider_name: str | None = None
    llm_task: str = "triage.round1"
    model_profile: str | None = None
    model_id: str = "scripted/v1"
    temperature: float = 0.0
    reasoning_effort: str | None = None
    max_output_tokens: int | None = Field(default=None, ge=1)
    output_mode: str | None = None
    usage_limits: dict[str, int | None] = Field(default_factory=dict)

    # Round 2 may use a different vendor/model/profile.
    round2_provider_name: str | None = None
    round2_llm_task: str = "triage.round2"
    round2_model_profile: str | None = None
    round2_model_id: str | None = None
    round2_temperature: float | None = None
    round2_reasoning_effort: str | None = None
    round2_max_output_tokens: int | None = Field(default=None, ge=1)
    round2_output_mode: str | None = None
    round2_usage_limits: dict[str, int | None] = Field(default_factory=dict)

    max_concurrency: int = Field(default=3, ge=1)
    max_attempts_per_task: int = Field(default=2, ge=1)
    # Runtime health/deadline settings.  Preflight is enabled by real-LLM composition roots.
    provider_preflight_enabled: bool = False
    provider_probe_timeout_seconds: float = Field(default=30.0, gt=0)
    provider_request_timeout_seconds: float = Field(default=120.0, gt=0)
    # Run-time provider outage recovery. Real providers enable this at composition time.
    provider_recovery_enabled: bool = False
    provider_recovery_max_attempts: int = Field(default=6, ge=1)
    provider_recovery_initial_backoff_seconds: float = Field(default=2.0, gt=0)
    provider_recovery_max_backoff_seconds: float = Field(default=20.0, gt=0)
    # V4 supplement budget. SUBJECT full code is never counted or truncated here.
    second_hop_max_chars: int = Field(default=48_000, ge=1)
    second_hop_max_related_functions: int = Field(default=8, ge=1)
    second_hop_max_code_chars_per_related_function: int = Field(default=12_000, ge=1)
    round2_enabled: bool = False
    template_id: str = ROUND1_TEMPLATE_ID
    round2_template_id: str = ROUND2_TEMPLATE_ID


class ProviderResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verdicts: list[FunctionVerdict]
    model_id: str
    usage: Usage = Field(default_factory=Usage)


class AttemptTrace(BaseModel):
    """One attributable business-level provider attempt."""

    model_config = ConfigDict(extra="forbid")

    task_id: str
    parent_task_id: str | None = None
    unit_id: str
    attempt: int = Field(ge=1)
    round: Literal[1, 2]
    refs: list[str]
    status: AttemptStatus
    model_profile: str | None = None
    model_id: str | None = None
    input_sha256: str
    usage: Usage = Field(default_factory=Usage)
    error: str | None = None
