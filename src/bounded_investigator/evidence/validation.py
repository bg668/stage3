"""Deterministic validation for model-produced EvidenceRef locations.

This module deliberately does NOT modify Claim/EvidenceRef schemas. Validation
is a sidecar concern: the model keeps emitting ``location + observation`` while
program code verifies that cited function/range references exist in supplied
source material.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Iterable

from ..schemas import EvidenceRef
from .snippets import parse_evidence_location

CodeIndex = dict[str, dict[int, tuple[str, str]]]


@dataclass(frozen=True)
class EvidenceReferenceCheck:
    owner_kind: str
    owner_id: str
    location: str
    observation: str
    function_id: str | None
    start_line: int | None
    end_line: int | None
    status: str
    reason: str
    source_packets: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["source_packets"] = list(self.source_packets)
        return value


def _validate_segment(
    *,
    owner_kind: str,
    owner_id: str,
    evidence: EvidenceRef,
    function_id: str,
    start_line: int,
    end_line: int,
    index: CodeIndex,
    missing_status: str,
) -> EvidenceReferenceCheck:
    lines = index.get(function_id)
    if not lines:
        return EvidenceReferenceCheck(
            owner_kind=owner_kind,
            owner_id=owner_id,
            location=evidence.location,
            observation=evidence.observation,
            function_id=function_id,
            start_line=start_line,
            end_line=end_line,
            status=missing_status,
            reason=(
                "referenced function is not present in the current packet"
                if missing_status == "not_in_current_packet"
                else "referenced function is absent from supplied source material"
            ),
        )
    if start_line < 1 or end_line < start_line:
        return EvidenceReferenceCheck(
            owner_kind=owner_kind,
            owner_id=owner_id,
            location=evidence.location,
            observation=evidence.observation,
            function_id=function_id,
            start_line=start_line,
            end_line=end_line,
            status="invalid_range",
            reason="line range is malformed",
            source_packets=tuple(sorted({source for _, source in lines.values()})),
        )
    requested = set(range(start_line, end_line + 1))
    present = requested.intersection(lines)
    if present != requested:
        available = sorted(lines)
        status = "not_in_current_packet" if missing_status == "not_in_current_packet" else "invalid_range"
        return EvidenceReferenceCheck(
            owner_kind=owner_kind,
            owner_id=owner_id,
            location=evidence.location,
            observation=evidence.observation,
            function_id=function_id,
            start_line=start_line,
            end_line=end_line,
            status=status,
            reason=(
                f"requested L{start_line}-L{end_line} is not fully present in this packet; "
                f"available range is L{available[0]}-L{available[-1]}; defer to full-material validation"
                if status == "not_in_current_packet"
                else f"requested L{start_line}-L{end_line} is not fully present; "
                     f"available range is L{available[0]}-L{available[-1]}"
            ),
            source_packets=tuple(sorted({source for _, source in lines.values()})),
        )
    return EvidenceReferenceCheck(
        owner_kind=owner_kind,
        owner_id=owner_id,
        location=evidence.location,
        observation=evidence.observation,
        function_id=function_id,
        start_line=start_line,
        end_line=end_line,
        status="valid",
        reason="referenced source range exists",
        source_packets=tuple(sorted({lines[n][1] for n in requested})),
    )


def validate_evidence_refs(
    evidence_refs: Iterable[EvidenceRef],
    *,
    owner_kind: str,
    owner_id: str,
    index: CodeIndex,
    missing_status: str = "invalid_function",
) -> list[EvidenceReferenceCheck]:
    checks: list[EvidenceReferenceCheck] = []
    for evidence in evidence_refs:
        segments = parse_evidence_location(evidence.location)
        if not segments:
            checks.append(EvidenceReferenceCheck(
                owner_kind=owner_kind,
                owner_id=owner_id,
                location=evidence.location,
                observation=evidence.observation,
                function_id=None,
                start_line=None,
                end_line=None,
                status="invalid_format",
                reason="no F-xxxx Lx[-Ly] source reference could be parsed",
            ))
            continue
        for function_id, start_line, end_line in segments:
            checks.append(_validate_segment(
                owner_kind=owner_kind,
                owner_id=owner_id,
                evidence=evidence,
                function_id=function_id,
                start_line=start_line,
                end_line=end_line,
                index=index,
                missing_status=missing_status,
            ))
    return checks


def _summary(checks: list[EvidenceReferenceCheck], *, scope: str) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for check in checks:
        counts[check.status] = counts.get(check.status, 0) + 1
    invalid_statuses = {"invalid_format", "invalid_function", "invalid_range"}
    invalid = sum(counts.get(status, 0) for status in invalid_statuses)
    return {
        "schema": "evidence_reference_validation_v1",
        "scope": scope,
        "total_segments": len(checks),
        "valid_segments": counts.get("valid", 0),
        "not_in_current_packet": counts.get("not_in_current_packet", 0),
        "invalid_segments": invalid,
        "counts_by_status": counts,
        "passed": invalid == 0,
        "checks": [check.to_dict() for check in checks],
    }


def validate_investigation_result(
    result: Any,
    *,
    index: CodeIndex,
    scope: str = "current_packet",
) -> dict[str, Any]:
    """Validate every evidence-bearing object without mutating the result."""
    missing_status = "not_in_current_packet" if scope == "current_packet" else "invalid_function"
    checks: list[EvidenceReferenceCheck] = []
    for idx, claim in enumerate(result.claims, 1):
        checks.extend(validate_evidence_refs(
            claim.evidence,
            owner_kind="claim",
            owner_id=f"C{idx:03d}",
            index=index,
            missing_status=missing_status,
        ))
    for idx, artifact in enumerate(result.artifacts, 1):
        checks.extend(validate_evidence_refs(
            artifact.evidence,
            owner_kind="artifact",
            owner_id=f"A{idx:03d}",
            index=index,
            missing_status=missing_status,
        ))
    for idx, need in enumerate(getattr(result, "material_needs", []), 1):
        checks.extend(validate_evidence_refs(
            need.evidence,
            owner_kind="material_need",
            owner_id=f"M{idx:03d}",
            index=index,
            missing_status=missing_status,
        ))
    return _summary(checks, scope=scope)


def validation_prompt_context(validation: dict[str, Any]) -> str:
    """Compact reviewer/reviser context. Deferred cross-packet refs are not errors."""
    lines = [
        f"scope: {validation.get('scope', 'unknown')}",
        f"valid_segments: {validation.get('valid_segments', 0)}",
        f"not_in_current_packet: {validation.get('not_in_current_packet', 0)}",
        f"invalid_segments: {validation.get('invalid_segments', 0)}",
    ]
    invalid = [
        x for x in validation.get("checks", [])
        if x.get("status") in {"invalid_format", "invalid_range", "invalid_function"}
    ]
    if not invalid:
        lines.append("invalid_references: none")
    else:
        lines.append("invalid_references:")
        for item in invalid[:20]:
            lines.append(
                f"- {item.get('owner_kind')} {item.get('owner_id')}: "
                f"{item.get('location')} [{item.get('status')}] {item.get('reason')}"
            )
    if validation.get("not_in_current_packet", 0):
        lines.append(
            "note: references outside the current packet are deferred to full-material validation; "
            "do not mark them unsupported solely for being absent here."
        )
    return "\n".join(lines)


def validate_canonical_references(canonical: Any, *, index: CodeIndex) -> dict[str, Any]:
    """Full-material audit for canonical claims/artifacts used by reporting."""
    checks: list[EvidenceReferenceCheck] = []
    owner_has_valid: dict[tuple[str, str], bool] = {}
    for claim in canonical.canonical_claims:
        owner = ("canonical_claim", claim.canonical_id)
        claim_checks = validate_evidence_refs(
            claim.evidence, owner_kind=owner[0], owner_id=owner[1], index=index
        )
        checks.extend(claim_checks)
        owner_has_valid[owner] = any(x.status == "valid" for x in claim_checks)
    for artifact in canonical.canonical_artifacts:
        owner = ("canonical_artifact", artifact.canonical_id)
        artifact_checks = validate_evidence_refs(
            artifact.evidence, owner_kind=owner[0], owner_id=owner[1], index=index
        )
        checks.extend(artifact_checks)
        owner_has_valid[owner] = any(x.status == "valid" for x in artifact_checks)

    summary = _summary(checks, scope="full_material")
    claims_without_valid = [
        claim.canonical_id for claim in canonical.canonical_claims
        if not owner_has_valid.get(("canonical_claim", claim.canonical_id), False)
    ]
    confirmed_without_valid = [
        claim.canonical_id for claim in canonical.canonical_claims
        if getattr(claim.evidence_level, "value", claim.evidence_level) == "confirmed"
        and claim.canonical_id in claims_without_valid
    ]
    artifacts_without_valid = [
        artifact.canonical_id for artifact in canonical.canonical_artifacts
        if not owner_has_valid.get(("canonical_artifact", artifact.canonical_id), False)
    ]
    summary.update({
        "claims_without_valid_evidence": claims_without_valid,
        "confirmed_claims_without_valid_evidence": confirmed_without_valid,
        "artifacts_without_valid_evidence": artifacts_without_valid,
    })
    # Any invalid reference is an integrity issue. Confirmed claims with no valid
    # source are called out separately because they are fatal for the report.
    summary["passed"] = (
        summary["invalid_segments"] == 0
        and not confirmed_without_valid
    )
    return summary
