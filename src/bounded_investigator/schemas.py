from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, model_validator

SCHEMA_VERSION = "11.0"


class DeliveryStatus(str, Enum):
    SUCCESS = "success"
    SUCCESS_AFTER_REPAIR = "success_after_repair"
    PARTIAL_VALIDATED = "partial_validated"
    RAW_ONLY = "raw_only"
    EXECUTION_FAILED = "execution_failed"


class Confidence(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class DetectionSpecificity(str, Enum):
    """How useful the artifact is as an infection/detection indicator, independent of its behavior role."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    NONE = "none"


class EvidenceLevel(str, Enum):
    """Explicit epistemic claim status, independent of confidence.

    UNCLASSIFIED is deliberately the default for old saved data. It is never
    automatically upgraded to CONFIRMED using confidence or an API-name guess.
    """

    CONFIRMED = "confirmed"
    SUSPECTED = "suspected"
    LEAD = "lead"
    UNCLASSIFIED = "unclassified"


class InvestigationStatus(str, Enum):
    COMPLETE = "complete"
    COMPLETE_WITH_GAPS = "complete_with_gaps"
    BLOCKED = "blocked"


class EvidenceRef(BaseModel):
    location: str = Field(description="Source location in F-XXXX Lx[-Ly] form, e.g. F-0482 L39-L93. Reuse original locations from dependencies; never cite summary labels or insert function names between the ID and line range.")
    observation: str = Field(description="What is directly visible at this location")


class BehaviorClaim(BaseModel):
    """One bounded source->action->sink behavior claim."""

    behavior: str
    condition: str = Field(description="Trigger/precondition; use 'none observed' if unconditional")
    source: str = Field(description="Input/data origin")
    action: str = Field(description="Core operation")
    sink: str = Field(description="Observable side effect/data destination")
    evidence: list[EvidenceRef] = Field(min_length=1)
    confidence: Confidence
    evidence_level: EvidenceLevel = EvidenceLevel.UNCLASSIFIED
    limitations: list[str] = Field(default_factory=list)


ArtifactRole = Literal[
    "created_output",
    "created_mutex",
    "persistence_value",
    "config_marker",
    "read_source",
    "environment_probe",
    "system_path",
    "parser_marker",
    "other",
]


class ArtifactFinding(BaseModel):
    kind: Literal[
        "file", "mutex", "registry", "resource", "string", "guid",
        "process", "network", "other"
    ]
    value: str
    role: ArtifactRole
    detection_specificity: DetectionSpecificity = Field(
        description=(
            "Detection value independent of role. high=strong sample/family/infection indicator; "
            "medium=useful with context/combination; low=weak/generic; none=normal/common source/path/probe."
        )
    )
    meaning: str
    evidence: list[EvidenceRef] = Field(min_length=1)
    confidence: Confidence


class GapItem(BaseModel):
    gap_type: Literal["investigation", "evidence", "capability", "report"] = "investigation"
    question: str
    reason: str
    scope: Literal["packet_internal", "outside_packet"]
    impact: Literal["critical", "major", "minor"]


class MaterialTarget(BaseModel):
    """One exact function/symbol and optional requested source-line interval.

    An interval applies to the ORIGINAL function, not the packet-local Markdown.
    """

    identifier: str = Field(min_length=1)
    start_line: int | None = Field(default=None, ge=1)
    end_line: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def range_is_well_formed(self) -> "MaterialTarget":
        if (self.start_line is None) != (self.end_line is None):
            raise ValueError("start_line and end_line must be supplied together")
        if self.start_line is not None and self.start_line > self.end_line:
            raise ValueError("start_line cannot exceed end_line")
        return self


class MaterialNeed(BaseModel):
    """An evidence-backed request for missing/unread material, never a presumed answer.

    Prefer ``targets`` for function_code: one function per element, with original
    source ranges when known. ``target`` remains for scalar data/legacy outputs;
    a legacy multi-function string can be split deterministically at projection.
    """

    kind: Literal["function_code", "resource_bytes", "data_bytes", "runtime_trace", "other"]
    target: str = ""
    targets: list[MaterialTarget] = Field(default_factory=list)
    reason: str = Field(min_length=1)
    impact: Literal["critical", "major", "minor"] = "major"
    evidence: list[EvidenceRef] = Field(min_length=1)

    @model_validator(mode="after")
    def require_a_target(self) -> "MaterialNeed":
        if not self.target.strip() and not self.targets:
            raise ValueError("material request needs target or targets")
        if self.kind != "function_code" and self.targets:
            raise ValueError("structured targets are only supported for function_code")
        return self

    def display_targets(self) -> str:
        if not self.targets:
            return self.target
        return ", ".join(
            f"{target.identifier} L{target.start_line}-L{target.end_line}"
            if target.start_line is not None else target.identifier
            for target in self.targets
        )


class MaterialSuggestion(BaseModel):
    """Program-derived recommendation from a recorded material request.

    A suggestion is *not* evidence that the malware sample lacks this item.
    """

    scope_id: str
    target: str
    kind: str
    availability: Literal["available", "partial", "not_in_input", "unavailable", "unknown"]
    action: Literal["provide", "submit_for_analysis", "confirm_or_provide"]
    recommendation: str
    expected_benefit: str
    impact: Literal["critical", "major", "minor"] = "major"
    requested_by_packets: list[str] = Field(default_factory=list)
    request_evidence: list[str] = Field(default_factory=list)


class HandoffItem(BaseModel):
    target: str
    reason: str
    expected_value: str
    priority: Literal["high", "medium", "low"]


class InvestigationResult(BaseModel):
    """Compact final bounded analysis of one material packet."""

    status: InvestigationStatus
    summary: str
    claims: list[BehaviorClaim] = Field(default_factory=list)
    artifacts: list[ArtifactFinding] = Field(default_factory=list)
    gaps: list[GapItem] = Field(default_factory=list)
    handoffs: list[HandoffItem] = Field(default_factory=list)
    material_needs: list[MaterialNeed] = Field(default_factory=list)

    @model_validator(mode="after")
    def status_matches_gaps(self) -> "InvestigationResult":
        if self.status == InvestigationStatus.COMPLETE and (self.gaps or self.handoffs or self.material_needs):
            raise ValueError("status=complete requires gaps, handoffs and material_needs to be empty")
        return self


class PartialInvestigationResult(BaseModel):
    """Independently validated subset recovered from an invalid InvestigationResult."""

    summary: str | None = None
    claims: list[BehaviorClaim] = Field(default_factory=list)
    artifacts: list[ArtifactFinding] = Field(default_factory=list)
    rejected_claims: int = 0
    rejected_artifacts: int = 0


class RevisionFix(BaseModel):
    """Correction intent; the program, not the Reviewer, grants field permissions."""

    type: Literal[
        "reference_format", "evidence_revision", "claim_revision", "artifact_classification",
        "gap_revision", "append_item", "delete_item",
    ]
    targets: list[str] = Field(min_length=1, description="Original-result object paths, using zero-based indices")


class ReviewIssue(BaseModel):
    issue_id: str = Field(description="Stable short id such as R1, R2")
    scope: Literal["packet_internal", "outside_packet"]
    severity: Literal["critical", "major", "minor"]
    kind: Literal[
        "omitted_claim", "wrong_claim", "artifact_role_error", "artifact_specificity_error",
        "omitted_artifact", "unsupported_claim", "evidence_mismatch", "boundary_violation",
        "gap_error", "other"
    ]
    description: str
    evidence: list[EvidenceRef] = Field(default_factory=list)
    required_fix: str
    fixes: list[RevisionFix] = Field(
        default_factory=list,
        description=(
            "Correction types bound to their own target objects. Every critical/major "
            "packet_internal issue must supply fixes; multiple types may target one object."
        ),
    )


class CoverageReview(BaseModel):
    requires_revision: bool
    issues: list[ReviewIssue] = Field(default_factory=list)
    review_summary: str

    @model_validator(mode="after")
    def normalize_revision_flag(self) -> "CoverageReview":
        substantive_internal = any(
            issue.scope == "packet_internal" and issue.severity in {"critical", "major"}
            for issue in self.issues
        )
        if self.requires_revision != substantive_internal:
            raise ValueError(
                "requires_revision must be true iff there is a critical/major packet_internal issue"
            )
        return self


class UsageSummary(BaseModel):
    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    visible_output_tokens: int = 0
    total_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: str | None = None


class FormatRepairStats(BaseModel):
    triggered: int = 0
    successful: int = 0
    failed: int = 0
    attempts: int = 0
    stage_attempts: dict[str, int] = Field(default_factory=dict)
    usage: UsageSummary = Field(default_factory=UsageSummary)


class RunEnvelope(BaseModel):
    packet_name: str
    elapsed_seconds: float
    revision_performed: bool
    delivery_status: DeliveryStatus = DeliveryStatus.SUCCESS
    schema_version: str = SCHEMA_VERSION
    stage_models: dict[str, str] = Field(default_factory=dict)
    stage_statuses: dict[str, DeliveryStatus] = Field(default_factory=dict)
    stage_usage: dict[str, UsageSummary] = Field(default_factory=dict)
    format_repair: FormatRepairStats = Field(default_factory=FormatRepairStats)
    usage: UsageSummary
    dependency_ids: list[str] = Field(default_factory=list)
    dependency_context_chars: int = 0
    review_completed: bool = True
    revision_failed: bool = False
    review: CoverageReview | None = None
    result: InvestigationResult | None = None
    partial_result: PartialInvestigationResult | None = None
    raw_output_paths: dict[str, str] = Field(default_factory=dict)
    validation_errors: dict[str, list[dict[str, object]]] = Field(default_factory=dict)

class ReconciliationStatus(str, Enum):
    COMPLETE = "complete"
    COMPLETE_WITH_CONFLICTS = "complete_with_conflicts"
    COMPLETE_WITH_MISSING_INPUTS = "complete_with_missing_inputs"


class ClaimRelationDecision(BaseModel):
    claim_id: str = Field(description="Reduced claim id such as R:C001")
    target_claim_id: str = Field(description="Target reduced claim id; target must be kept")
    reason: str = ""


class ClaimUnresolvedDecision(BaseModel):
    claim_id: str = Field(description="Reduced claim id such as R:C001")
    related_claim_ids: list[str] = Field(default_factory=list)
    reason: str = ""


class ArtifactConflictDecision(BaseModel):
    """Choose metadata from one real source artifact for an exact-value conflict group."""

    artifact_group_id: str = Field(description="Reduced artifact group id such as R:A001")
    selected_source_artifact_id: str
    reason: str = ""


class GlobalReconciliationDecisionResult(BaseModel):
    """Compact model output: relationship buckets only, never rewritten malware facts."""

    keep_claim_ids: list[str] = Field(default_factory=list)
    merge_into: list[ClaimRelationDecision] = Field(default_factory=list)
    superseded_by: list[ClaimRelationDecision] = Field(default_factory=list)
    unresolved: list[ClaimUnresolvedDecision] = Field(default_factory=list)
    artifact_conflict_decisions: list[ArtifactConflictDecision] = Field(default_factory=list)


class CanonicalClaim(BaseModel):
    canonical_id: str = Field(description="Stable global id such as G:C001")
    behavior: str
    condition: str
    source: str
    action: str
    sink: str
    evidence: list[EvidenceRef] = Field(min_length=1)
    confidence: Confidence
    evidence_level: EvidenceLevel = EvidenceLevel.UNCLASSIFIED
    limitations: list[str] = Field(default_factory=list)
    source_claim_ids: list[str] = Field(min_length=1)
    superseded_claim_ids: list[str] = Field(default_factory=list)
    resolution: Literal["unchanged", "merged", "superseded_weaker_claim"]
    resolution_reason: str


class CanonicalArtifact(BaseModel):
    canonical_id: str = Field(description="Stable global id such as G:A001")
    kind: Literal[
        "file", "mutex", "registry", "resource", "string", "guid",
        "process", "network", "other"
    ]
    value: str
    role: ArtifactRole
    detection_specificity: DetectionSpecificity
    meaning: str
    evidence: list[EvidenceRef] = Field(min_length=1)
    confidence: Confidence
    source_artifact_ids: list[str] = Field(min_length=1)


class UnresolvedConflict(BaseModel):
    # One id is allowed for deterministic coverage fallback when the resolver omitted a claim.
    claim_ids: list[str] = Field(min_length=1)
    description: str
    reason: str
    impact: Literal["major", "minor"]


class GlobalGap(BaseModel):
    gap_id: str | None = None
    gap_type: Literal["investigation", "evidence", "capability", "report"] = "investigation"
    question: str
    reason: str
    source_packets: list[str] = Field(default_factory=list)
    impact: Literal["critical", "major", "minor"]


class GlobalReconciliationResult(BaseModel):
    status: ReconciliationStatus
    summary: str
    canonical_claims: list[CanonicalClaim] = Field(default_factory=list)
    canonical_artifacts: list[CanonicalArtifact] = Field(default_factory=list)
    unresolved_conflicts: list[UnresolvedConflict] = Field(default_factory=list)
    gaps: list[GlobalGap] = Field(default_factory=list)
    unavailable_packets: list[str] = Field(default_factory=list)
    unaccounted_input_claim_ids: list[str] = Field(default_factory=list)
    unaccounted_input_artifact_ids: list[str] = Field(default_factory=list)


class GlobalReconciliationEnvelope(BaseModel):
    delivery_status: DeliveryStatus
    elapsed_seconds: float
    result: GlobalReconciliationResult | None = None
    usage: UsageSummary = Field(default_factory=UsageSummary)
    format_repair: FormatRepairStats = Field(default_factory=FormatRepairStats)
    raw_output_path: str | None = None
    validation_errors: list[dict[str, object]] = Field(default_factory=list)
    source_claim_count: int = 0
    source_artifact_count: int = 0
    reduced_claim_count: int = 0
    reduced_artifact_count: int = 0
    artifact_conflict_count: int = 0
    provenance_valid: bool = False
    source_gap_count: int = 0
