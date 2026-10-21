from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .artifact_normalization import split_artifact

from .schemas import (
    ArtifactFinding,
    BehaviorClaim,
    CanonicalArtifact,
    CanonicalClaim,
    Confidence,
    EvidenceLevel,
    DetectionSpecificity,
    GlobalGap,
    GapReconciliationDecisionResult,
    GlobalReconciliationDecisionResult,
    GlobalReconciliationResult,
    ReconciliationStatus,
    RunEnvelope,
    UnresolvedConflict,
)


_CONF_RANK = {Confidence.LOW: 0, Confidence.MEDIUM: 1, Confidence.HIGH: 2}
_SPEC_RANK = {
    DetectionSpecificity.NONE: 0,
    DetectionSpecificity.LOW: 1,
    DetectionSpecificity.MEDIUM: 2,
    DetectionSpecificity.HIGH: 3,
}


@dataclass(frozen=True)
class ReducedClaimGroup:
    group_id: str
    source_claim_ids: tuple[str, ...]
    claim: BehaviorClaim


@dataclass(frozen=True)
class ReducedArtifactGroup:
    group_id: str
    source_artifact_ids: tuple[str, ...]
    representative: ArtifactFinding
    metadata_conflict: bool


@dataclass(frozen=True)
class ReconciliationProjection:
    text: str
    claims: dict[str, BehaviorClaim]
    artifacts: dict[str, ArtifactFinding]
    reduced_claims: dict[str, ReducedClaimGroup]
    reduced_artifacts: dict[str, ReducedArtifactGroup]
    unavailable_packets: list[str]
    packet_ids: list[str]
    gaps: list[GlobalGap]


class ReconciliationProvenanceError(ValueError):
    pass


def _clean(value: str) -> str:
    return " ".join(value.split())


def _evidence_key(evidence) -> tuple[str, str]:
    return evidence.location, evidence.observation


def _merge_evidence(items: Iterable) -> list:
    out = []
    seen: set[tuple[str, str]] = set()
    for evidence in items:
        key = _evidence_key(evidence)
        if key not in seen:
            seen.add(key)
            out.append(evidence)
    return out


def _highest_confidence(items: Iterable[Confidence]) -> Confidence:
    return max(items, key=lambda item: _CONF_RANK[item])


def _conservative_evidence_level(levels: Iterable[EvidenceLevel]) -> EvidenceLevel:
    """Reconcile explicit epistemic judgements; never infer them from confidence.

    Old unclassified records do not override a real explicit judgement, but if
    two explicit statuses disagree the weaker explicit conclusion wins.
    """
    explicit = [x for x in levels if x != EvidenceLevel.UNCLASSIFIED]
    if not explicit:
        return EvidenceLevel.UNCLASSIFIED
    order = {EvidenceLevel.LEAD: 0, EvidenceLevel.SUSPECTED: 1, EvidenceLevel.CONFIRMED: 2}
    return min(explicit, key=order.__getitem__)


def _lowest_specificity(items: Iterable[DetectionSpecificity]) -> DetectionSpecificity:
    return min(items, key=lambda item: _SPEC_RANK[item])


def _claim_exact_key(claim: BehaviorClaim) -> tuple[str, str, str, str, str]:
    return tuple(_clean(v) for v in (
        claim.behavior, claim.condition, claim.source, claim.action, claim.sink
    ))


def _artifact_exact_key(artifact: ArtifactFinding) -> tuple[str, str]:
    # Exact-value merge only. Deliberately do not case-fold paths/strings here.
    return artifact.kind, artifact.value.strip()


def _collect_global_gaps(completed: dict[str, RunEnvelope]) -> list[GlobalGap]:
    """Retain original gap types: investigation TODOs are NOT report gaps.

    The legacy collector silently promoted every non-handoff gap to ``report``.
    That was incorrect and made final assessments inherit investigation noise.
    """
    grouped: dict[tuple[str, str, str, str], set[str]] = {}
    for packet_id, env in sorted(completed.items()):
        result = env.result
        if result is None:
            continue
        for gap in result.gaps:
            key = (gap.gap_type, gap.question, gap.reason, gap.impact)
            grouped.setdefault(key, set()).add(packet_id)
        for handoff in result.handoffs:
            impact = "major" if handoff.priority == "high" else "minor"
            question = f"Follow up {handoff.target}"
            reason = f"{handoff.reason}; expected={handoff.expected_value}"
            key = ("investigation", question, reason, impact)
            grouped.setdefault(key, set()).add(packet_id)
    ordered = sorted(
        ((gap_type, q, r, impact, sorted(packets))
         for (gap_type, q, r, impact), packets in grouped.items()),
        key=lambda item: (item[3], item[0], item[1], item[2], item[4]),
    )
    return [
        GlobalGap(
            gap_id=f"R:G{idx:03d}",
            gap_type=gap_type,
            question=q,
            reason=reason,
            impact=impact,
            source_packets=packets,
        )
        for idx, (gap_type, q, reason, impact, packets) in enumerate(ordered, 1)
    ]


def _build_original_maps(
    completed: dict[str, RunEnvelope], failures: dict[str, str]
) -> tuple[dict[str, BehaviorClaim], dict[str, ArtifactFinding], list[str], list[str]]:
    claims: dict[str, BehaviorClaim] = {}
    artifacts: dict[str, ArtifactFinding] = {}
    unavailable = set(failures)
    packet_ids = sorted(set(completed) | set(failures))

    for packet_id in packet_ids:
        if packet_id in failures:
            continue
        env = completed[packet_id]
        result = env.result
        partial = env.partial_result
        if result is None and partial is None:
            unavailable.add(packet_id)
            continue
        packet_claims = result.claims if result is not None else partial.claims
        packet_artifacts = result.artifacts if result is not None else partial.artifacts
        for idx, claim in enumerate(packet_claims, 1):
            claims[f"{packet_id}:C{idx:03d}"] = claim
        for idx, artifact in enumerate(packet_artifacts, 1):
            source_id = f"{packet_id}:A{idx:03d}"
            atomic = split_artifact(artifact)
            for j, item in enumerate(atomic, 1):
                # Legacy v1.4 envelopes may have several values in one artifact.
                # Preserve the original artifact source ID with an item suffix.
                item_id = source_id if len(atomic) == 1 else f"{source_id}:P{j:02d}"
                artifacts[item_id] = item
    return claims, artifacts, sorted(unavailable), packet_ids


def _reduce_claims(claims: dict[str, BehaviorClaim]) -> dict[str, ReducedClaimGroup]:
    grouped: dict[tuple[str, str, str, str, str], list[tuple[str, BehaviorClaim]]] = {}
    for source_id, claim in claims.items():
        grouped.setdefault(_claim_exact_key(claim), []).append((source_id, claim))

    reduced: dict[str, ReducedClaimGroup] = {}
    for idx, members in enumerate(grouped.values(), 1):
        source_ids = tuple(source_id for source_id, _ in members)
        representative = max((claim for _, claim in members), key=lambda c: _CONF_RANK[c.confidence])
        merged = representative.model_copy(deep=True)
        merged.evidence = _merge_evidence(e for _, c in members for e in c.evidence)
        merged.confidence = _highest_confidence(c.confidence for _, c in members)
        merged.evidence_level = _conservative_evidence_level(c.evidence_level for _, c in members)
        merged.limitations = list(dict.fromkeys(l for _, c in members for l in c.limitations))
        group_id = f"R:C{idx:03d}"
        reduced[group_id] = ReducedClaimGroup(group_id, source_ids, merged)
    return reduced


def _reduce_artifacts(artifacts: dict[str, ArtifactFinding]) -> dict[str, ReducedArtifactGroup]:
    grouped: dict[tuple[str, str], list[tuple[str, ArtifactFinding]]] = {}
    for source_id, artifact in artifacts.items():
        grouped.setdefault(_artifact_exact_key(artifact), []).append((source_id, artifact))

    reduced: dict[str, ReducedArtifactGroup] = {}
    for idx, members in enumerate(grouped.values(), 1):
        source_ids = tuple(source_id for source_id, _ in members)
        first = members[0][1]
        roles = {a.role for _, a in members}
        specs = {a.detection_specificity for _, a in members}
        conflict = len(roles) > 1 or len(specs) > 1
        representative = first.model_copy(deep=True)
        representative.evidence = _merge_evidence(e for _, a in members for e in a.evidence)
        representative.confidence = _highest_confidence(a.confidence for _, a in members)
        group_id = f"R:A{idx:03d}"
        reduced[group_id] = ReducedArtifactGroup(group_id, source_ids, representative, conflict)
    return reduced


def _render_resolver_input(
    reduced_claims: dict[str, ReducedClaimGroup],
    reduced_artifacts: dict[str, ReducedArtifactGroup],
    artifacts: dict[str, ArtifactFinding],
    unavailable_packets: list[str],
) -> str:
    lines = [
        "<reduced_claims>",
        "Each id is already an exact-duplicate group. Decide only relations between these groups.",
    ]
    for group in reduced_claims.values():
        c = group.claim
        lines.extend([
            f"- id: {group.group_id}",
            f"  sources: {', '.join(group.source_claim_ids)}",
            f"  behavior: {c.behavior}",
            f"  confidence: {c.confidence.value}",
            f"  evidence_level: {c.evidence_level.value}",
            f"  condition: {c.condition}",
            f"  flow: {c.source} -> {c.action} -> {c.sink}",
            "  evidence:",
            *[f"    - {e.location}: {e.observation}" for e in c.evidence],
        ])
        if c.limitations:
            lines.append(f"  limitations: {'; '.join(c.limitations)}")
    lines.append("</reduced_claims>")

    conflicts = [g for g in reduced_artifacts.values() if g.metadata_conflict]
    lines.extend([
        "<artifact_metadata_conflicts>",
        "Only choose which REAL source artifact supplies role/specificity metadata. Do not rewrite values.",
    ])
    if not conflicts:
        lines.append("none")
    for group in conflicts:
        lines.extend([
            f"- group_id: {group.group_id}",
            f"  kind: {group.representative.kind}",
            f"  value: {group.representative.value}",
            "  variants:",
        ])
        for source_id in group.source_artifact_ids:
            a = artifacts[source_id]
            lines.append(
                f"    - {source_id}: role={a.role}; specificity={a.detection_specificity.value}; "
                f"confidence={a.confidence.value}; meaning={a.meaning}"
            )
    lines.append("</artifact_metadata_conflicts>")
    lines.extend([
        "<unavailable_packets>",
        ", ".join(unavailable_packets) if unavailable_packets else "none",
        "</unavailable_packets>",
    ])
    return "\n".join(lines)


def project_reconciliation_input(
    completed: dict[str, RunEnvelope], failures: dict[str, str]
) -> ReconciliationProjection:
    """Deterministically reduce validated facts before the decision-only resolver."""
    claims, artifacts, unavailable, packet_ids = _build_original_maps(completed, failures)
    reduced_claims = _reduce_claims(claims)
    reduced_artifacts = _reduce_artifacts(artifacts)
    text = _render_resolver_input(reduced_claims, reduced_artifacts, artifacts, unavailable)
    return ReconciliationProjection(
        text=text,
        claims=claims,
        artifacts=artifacts,
        reduced_claims=reduced_claims,
        reduced_artifacts=reduced_artifacts,
        unavailable_packets=unavailable,
        packet_ids=packet_ids,
        gaps=_collect_global_gaps(completed),
    )



def projection_audit_dict(projection: ReconciliationProjection) -> dict[str, object]:
    """Small deterministic audit artifact; contains ids/mappings, not rewritten malware facts."""
    return {
        "source_claim_count": len(projection.claims),
        "reduced_claim_count": len(projection.reduced_claims),
        "source_artifact_count": len(projection.artifacts),
        "reduced_artifact_count": len(projection.reduced_artifacts),
        "artifact_conflict_count": sum(
            1 for group in projection.reduced_artifacts.values() if group.metadata_conflict
        ),
        "unavailable_packets": projection.unavailable_packets,
        "claim_groups": {
            group_id: list(group.source_claim_ids)
            for group_id, group in projection.reduced_claims.items()
        },
        "artifact_groups": {
            group_id: {
                "source_artifact_ids": list(group.source_artifact_ids),
                "metadata_conflict": group.metadata_conflict,
            }
            for group_id, group in projection.reduced_artifacts.items()
        },
    }

@dataclass(frozen=True)
class _NormalizedClaimDecision:
    claim_id: str
    decision: str
    target_claim_id: str | None = None
    related_claim_ids: tuple[str, ...] = ()
    reason: str = ""


def _normalize_claim_decisions(
    decisions: GlobalReconciliationDecisionResult,
    projection: ReconciliationProjection,
) -> dict[str, _NormalizedClaimDecision]:
    known = set(projection.reduced_claims)
    candidates: dict[str, list[_NormalizedClaimDecision]] = {}

    for claim_id in decisions.keep_claim_ids:
        if claim_id in known:
            candidates.setdefault(claim_id, []).append(
                _NormalizedClaimDecision(claim_id, "keep")
            )
    for item in decisions.merge_into:
        if item.claim_id in known:
            candidates.setdefault(item.claim_id, []).append(
                _NormalizedClaimDecision(
                    item.claim_id, "merge_into", target_claim_id=item.target_claim_id, reason=item.reason
                )
            )
    for item in decisions.superseded_by:
        if item.claim_id in known:
            candidates.setdefault(item.claim_id, []).append(
                _NormalizedClaimDecision(
                    item.claim_id, "superseded_by", target_claim_id=item.target_claim_id, reason=item.reason
                )
            )
    for item in decisions.unresolved:
        if item.claim_id in known:
            candidates.setdefault(item.claim_id, []).append(
                _NormalizedClaimDecision(
                    item.claim_id,
                    "unresolved",
                    related_claim_ids=tuple(r for r in item.related_claim_ids if r in known),
                    reason=item.reason,
                )
            )

    normalized: dict[str, _NormalizedClaimDecision] = {}
    for claim_id in projection.reduced_claims:
        items = candidates.get(claim_id, [])
        if len(items) != 1:
            normalized[claim_id] = _NormalizedClaimDecision(
                claim_id,
                "unresolved",
                reason=("resolver omitted claim" if not items else "resolver placed claim in multiple buckets"),
            )
            continue
        d = items[0]
        if d.target_claim_id is not None and (
            d.target_claim_id not in known or d.target_claim_id == claim_id
        ):
            normalized[claim_id] = _NormalizedClaimDecision(
                claim_id, "unresolved", reason="resolver referenced invalid target claim"
            )
            continue
        normalized[claim_id] = d

    # Keep relation graph intentionally one-hop: merge/supersede targets must be kept.
    for claim_id, d in list(normalized.items()):
        if d.decision in {"merge_into", "superseded_by"}:
            target = normalized.get(d.target_claim_id or "")
            if target is None or target.decision != "keep":
                related = (d.target_claim_id,) if d.target_claim_id in known else ()
                normalized[claim_id] = _NormalizedClaimDecision(
                    claim_id,
                    "unresolved",
                    related_claim_ids=related,
                    reason="merge/supersede target was not a kept canonical claim",
                )
    return normalized


def _artifact_choice_map(
    decisions: GlobalReconciliationDecisionResult,
    projection: ReconciliationProjection,
) -> dict[str, str]:
    choices: dict[str, str] = {}
    by_group = {d.artifact_group_id: d for d in decisions.artifact_conflict_decisions}
    for group_id, group in projection.reduced_artifacts.items():
        if not group.metadata_conflict:
            continue
        d = by_group.get(group_id)
        if d and d.selected_source_artifact_id in group.source_artifact_ids:
            choices[group_id] = d.selected_source_artifact_id
    return choices


def _assemble_canonical_claims(
    normalized: dict[str, _NormalizedClaimDecision],
    projection: ReconciliationProjection,
) -> tuple[list[CanonicalClaim], list[UnresolvedConflict], set[str]]:
    canonical: list[CanonicalClaim] = []
    conflicts: list[UnresolvedConflict] = []
    accounted_originals: set[str] = set()

    merge_into: dict[str, list[str]] = {}
    superseded_by: dict[str, list[str]] = {}
    for claim_id, decision in normalized.items():
        if decision.decision == "merge_into":
            merge_into.setdefault(decision.target_claim_id or "", []).append(claim_id)
        elif decision.decision == "superseded_by":
            superseded_by.setdefault(decision.target_claim_id or "", []).append(claim_id)

    for claim_id, decision in normalized.items():
        group = projection.reduced_claims[claim_id]
        if decision.decision == "unresolved":
            related = [rid for rid in decision.related_claim_ids if rid in projection.reduced_claims]
            originals = list(group.source_claim_ids)
            for rid in related:
                originals.extend(projection.reduced_claims[rid].source_claim_ids)
            originals = list(dict.fromkeys(originals))
            accounted_originals.update(originals)
            conflicts.append(UnresolvedConflict(
                claim_ids=originals,
                description=f"Unresolved reduced claim {claim_id}",
                reason=decision.reason,
                impact="major",
            ))
            continue
        if decision.decision != "keep":
            continue

        merged_group_ids = [claim_id, *merge_into.get(claim_id, [])]
        superseded_group_ids = superseded_by.get(claim_id, [])
        source_originals = [
            source_id
            for gid in merged_group_ids
            for source_id in projection.reduced_claims[gid].source_claim_ids
        ]
        superseded_originals = [
            source_id
            for gid in superseded_group_ids
            for source_id in projection.reduced_claims[gid].source_claim_ids
        ]
        accounted_originals.update(source_originals)
        accounted_originals.update(superseded_originals)

        representative = group.claim
        evidence = _merge_evidence(
            e for gid in merged_group_ids for e in projection.reduced_claims[gid].claim.evidence
        )
        limitations = list(dict.fromkeys(
            l for gid in merged_group_ids for l in projection.reduced_claims[gid].claim.limitations
        ))
        confidence = _highest_confidence(
            projection.reduced_claims[gid].claim.confidence for gid in merged_group_ids
        )
        evidence_level = _conservative_evidence_level(
            projection.reduced_claims[gid].claim.evidence_level for gid in merged_group_ids
        )
        reasons = [normalized[gid].reason for gid in merged_group_ids if normalized[gid].reason]
        reasons.extend(normalized[gid].reason for gid in superseded_group_ids if normalized[gid].reason)
        resolution = (
            "superseded_weaker_claim" if superseded_originals
            else "merged" if len(source_originals) > 1
            else "unchanged"
        )
        canonical.append(CanonicalClaim(
            canonical_id=f"G:C{len(canonical)+1:03d}",
            behavior=representative.behavior,
            condition=representative.condition,
            source=representative.source,
            action=representative.action,
            sink=representative.sink,
            evidence=evidence,
            confidence=confidence,
            evidence_level=evidence_level,
            limitations=limitations,
            source_claim_ids=list(dict.fromkeys(source_originals)),
            superseded_claim_ids=list(dict.fromkeys(superseded_originals)),
            resolution=resolution,
            resolution_reason="; ".join(dict.fromkeys(reasons)) or "kept by resolver",
        ))

    # Defensive coverage fallback: no original claim may silently disappear.
    missing = set(projection.claims) - accounted_originals
    for source_id in sorted(missing):
        accounted_originals.add(source_id)
        conflicts.append(UnresolvedConflict(
            claim_ids=[source_id],
            description="Input claim was not consumed by resolver decisions",
            reason="deterministic coverage gate preserved it as unresolved",
            impact="major",
        ))
    return canonical, conflicts, accounted_originals


def _assemble_canonical_artifacts(
    decisions: GlobalReconciliationDecisionResult,
    projection: ReconciliationProjection,
) -> tuple[list[CanonicalArtifact], list[GlobalGap], set[str]]:
    choices = _artifact_choice_map(decisions, projection)
    canonical: list[CanonicalArtifact] = []
    gaps: list[GlobalGap] = []
    accounted: set[str] = set()

    for group_id, group in projection.reduced_artifacts.items():
        source_ids = list(group.source_artifact_ids)
        accounted.update(source_ids)
        selected_id = choices.get(group_id)
        if selected_id:
            selected = projection.artifacts[selected_id]
            role = selected.role
            specificity = selected.detection_specificity
            meaning = selected.meaning
            confidence = selected.confidence
        elif group.metadata_conflict:
            # Resolver omission is not allowed to promote a generic artifact into an IOC.
            selected = group.representative
            role = "other"
            specificity = _lowest_specificity(
                projection.artifacts[sid].detection_specificity for sid in source_ids
            )
            meaning = "Unresolved artifact metadata conflict; value preserved conservatively"
            confidence = min(
                (projection.artifacts[sid].confidence for sid in source_ids),
                key=lambda item: _CONF_RANK[item],
            )
            gaps.append(GlobalGap(
                question=f"Resolve metadata for artifact {selected.value}",
                reason=f"Conflicting role/specificity across sources: {', '.join(source_ids)}",
                source_packets=sorted({sid.split(':', 1)[0] for sid in source_ids}),
                impact="minor",
            ))
        else:
            selected = group.representative
            role = selected.role
            specificity = selected.detection_specificity
            meaning = selected.meaning
            confidence = selected.confidence

        evidence = _merge_evidence(
            e for sid in source_ids for e in projection.artifacts[sid].evidence
        )
        canonical.append(CanonicalArtifact(
            canonical_id=f"G:A{len(canonical)+1:03d}",
            kind=selected.kind,
            value=selected.value,
            role=role,
            detection_specificity=specificity,
            meaning=meaning,
            evidence=evidence,
            confidence=confidence,
            source_artifact_ids=source_ids,
        ))
    return canonical, gaps, accounted


def assemble_reconciliation_result(
    decisions: GlobalReconciliationDecisionResult,
    projection: ReconciliationProjection,
) -> GlobalReconciliationResult:
    """Programmatically build canonical facts from source objects plus relation decisions."""
    normalized = _normalize_claim_decisions(decisions, projection)
    canonical_claims, conflicts, accounted_claims = _assemble_canonical_claims(normalized, projection)
    canonical_artifacts, artifact_gaps, accounted_artifacts = _assemble_canonical_artifacts(
        decisions, projection
    )
    gaps = [*projection.gaps, *artifact_gaps]
    unavailable = list(projection.unavailable_packets)
    unaccounted_claims = sorted(set(projection.claims) - accounted_claims)
    unaccounted_artifacts = sorted(set(projection.artifacts) - accounted_artifacts)

    if unavailable:
        status = ReconciliationStatus.COMPLETE_WITH_MISSING_INPUTS
    elif conflicts or unaccounted_claims or unaccounted_artifacts:
        status = ReconciliationStatus.COMPLETE_WITH_CONFLICTS
    else:
        status = ReconciliationStatus.COMPLETE

    summary = (
        f"Deterministically reduced {len(projection.claims)} claims to "
        f"{len(projection.reduced_claims)} groups and {len(projection.artifacts)} artifacts to "
        f"{len(projection.reduced_artifacts)} exact-value groups; assembled "
        f"{len(canonical_claims)} canonical claims and {len(canonical_artifacts)} canonical artifacts; "
        f"unresolved_conflicts={len(conflicts)}; unavailable_packets={len(unavailable)}."
    )
    return GlobalReconciliationResult(
        status=status,
        summary=summary,
        canonical_claims=canonical_claims,
        canonical_artifacts=canonical_artifacts,
        unresolved_conflicts=conflicts,
        gaps=gaps,
        unavailable_packets=unavailable,
        unaccounted_input_claim_ids=unaccounted_claims,
        unaccounted_input_artifact_ids=unaccounted_artifacts,
    )


def validate_and_finalize_reconciliation(
    result: GlobalReconciliationResult,
    projection: ReconciliationProjection,
) -> GlobalReconciliationResult:
    """Final provenance gate over program-assembled canonical facts."""
    known_claims = set(projection.claims)
    known_artifacts = set(projection.artifacts)

    for canonical in result.canonical_claims:
        refs = set(canonical.source_claim_ids) | set(canonical.superseded_claim_ids)
        unknown = refs - known_claims
        if unknown:
            raise ReconciliationProvenanceError(
                f"canonical claim {canonical.canonical_id} references unknown claims: {sorted(unknown)}"
            )
        allowed_evidence = {
            _evidence_key(e)
            for source_id in canonical.source_claim_ids
            for e in projection.claims[source_id].evidence
        }
        invented = [_evidence_key(e) for e in canonical.evidence if _evidence_key(e) not in allowed_evidence]
        if invented:
            raise ReconciliationProvenanceError(
                f"canonical claim {canonical.canonical_id} contains evidence not present in source claims: {invented}"
            )

    for canonical in result.canonical_artifacts:
        refs = set(canonical.source_artifact_ids)
        unknown = refs - known_artifacts
        if unknown:
            raise ReconciliationProvenanceError(
                f"canonical artifact {canonical.canonical_id} references unknown artifacts: {sorted(unknown)}"
            )
        source_values = {projection.artifacts[source_id].value for source_id in refs}
        if canonical.value not in source_values:
            raise ReconciliationProvenanceError(
                f"canonical artifact {canonical.canonical_id} changed value {canonical.value!r}"
            )
        allowed_evidence = {
            _evidence_key(e)
            for source_id in refs
            for e in projection.artifacts[source_id].evidence
        }
        invented = [_evidence_key(e) for e in canonical.evidence if _evidence_key(e) not in allowed_evidence]
        if invented:
            raise ReconciliationProvenanceError(
                f"canonical artifact {canonical.canonical_id} contains invented evidence: {invented}"
            )

    # Program assembly should account for every original object. Keep explicit fields as a hard audit gate.
    accounted_claims = {
        source_id
        for c in result.canonical_claims
        for source_id in [*c.source_claim_ids, *c.superseded_claim_ids]
    }
    for conflict in result.unresolved_conflicts:
        accounted_claims.update(conflict.claim_ids)
    accounted_artifacts = {
        source_id for a in result.canonical_artifacts for source_id in a.source_artifact_ids
    }
    result.unaccounted_input_claim_ids = sorted(known_claims - accounted_claims)
    result.unaccounted_input_artifact_ids = sorted(known_artifacts - accounted_artifacts)
    if result.unavailable_packets:
        result.status = ReconciliationStatus.COMPLETE_WITH_MISSING_INPUTS
    elif result.unresolved_conflicts or result.unaccounted_input_claim_ids or result.unaccounted_input_artifact_ids:
        result.status = ReconciliationStatus.COMPLETE_WITH_CONFLICTS
    else:
        result.status = ReconciliationStatus.COMPLETE
    return result


@dataclass(frozen=True)
class GapReconciliationProjection:
    text: str
    gaps: dict[str, GlobalGap]


def project_gap_reconciliation_input(
    result: GlobalReconciliationResult,
) -> GapReconciliationProjection:
    """Build a compact, read-only view of canonical facts plus historical gaps."""
    gaps: dict[str, GlobalGap] = {}
    for idx, gap in enumerate(result.gaps, 1):
        gap_id = gap.gap_id or f"R:G{idx:03d}"
        copied = gap.model_copy(deep=True)
        copied.gap_id = gap_id
        gaps[gap_id] = copied

    lines = [
        "<canonical_claims>",
        "These are already validated canonical facts. Use them only to decide whether a historical gap is resolved.",
    ]
    for claim in result.canonical_claims:
        lines.extend([
            f"- id: {claim.canonical_id}",
            f"  behavior: {claim.behavior}",
            f"  confidence: {claim.confidence.value}",
            f"  condition: {claim.condition}",
            f"  flow: {claim.source} -> {claim.action} -> {claim.sink}",
        ])
        if claim.limitations:
            lines.append(f"  limitations: {'; '.join(claim.limitations)}")
    lines.append("</canonical_claims>")

    lines.extend([
        "<canonical_artifacts>",
        "Artifacts can resolve gaps about concrete paths/values, but do not infer behavior from them alone.",
    ])
    for artifact in result.canonical_artifacts:
        lines.append(
            f"- {artifact.canonical_id}: {artifact.kind} {artifact.value}; "
            f"role={artifact.role}; specificity={artifact.detection_specificity.value}; "
            f"meaning={artifact.meaning}"
        )
    lines.append("</canonical_artifacts>")

    lines.extend([
        "<historical_gaps>",
        "Classify every gap id as resolved or still_open.",
    ])
    for gap_id, gap in gaps.items():
        lines.extend([
            f"- id: {gap_id}",
            f"  type: {gap.gap_type}",
            f"  impact: {gap.impact}",
            f"  source_packets: {', '.join(gap.source_packets) if gap.source_packets else 'none'}",
            f"  question: {gap.question}",
            f"  reason: {gap.reason}",
            f"  resolved_by_claim_ids: {", ".join(gap.resolved_by_claim_ids) if gap.resolved_by_claim_ids else 'none'}",
        ])
    lines.append("</historical_gaps>")
    return GapReconciliationProjection(text="\n".join(lines), gaps=gaps)


def gap_projection_audit_dict(projection: GapReconciliationProjection) -> dict[str, object]:
    return {
        "source_gap_count": len(projection.gaps),
        "gap_ids": list(projection.gaps),
    }


def apply_gap_reconciliation(
    result: GlobalReconciliationResult,
    decisions: GapReconciliationDecisionResult,
    projection: GapReconciliationProjection,
) -> tuple[GlobalReconciliationResult, list[str], list[str]]:
    """Apply ID-only decisions conservatively: omissions/duplicates remain open."""
    known = set(projection.gaps)
    resolved = set()
    open_ids = set()
    for decision in decisions.decisions:
        if decision.gap_id not in known:
            continue
        if decision.status == "resolved":
            resolved.add(decision.gap_id)
        else:
            open_ids.add(decision.gap_id)
        if decision.resolved_by_claim_ids:
            projection.gaps[decision.gap_id].resolved_by_claim_ids = list(decision.resolved_by_claim_ids)

    # compatibility with old decision-only format
    if not decisions.decisions:
        resolved = {gap_id for gap_id in decisions.resolved_gap_ids if gap_id in known}
        open_ids = {gap_id for gap_id in decisions.still_open_gap_ids if gap_id in known}

    # Conflicting classification or resolver omission must never delete a real unknown.
    ambiguous = resolved & open_ids
    resolved -= ambiguous
    open_ids |= ambiguous
    omitted = known - resolved - open_ids
    open_ids |= omitted

    updated = result.model_copy(deep=True)
    updated.gaps = []
    for gap_id in projection.gaps:
        if gap_id in open_ids:
            updated.gaps.append(projection.gaps[gap_id])
    updated.summary = (
        f"{result.summary} Gap reconciliation resolved {len(resolved)} of {len(known)} "
        f"historical gaps; still_open={len(open_ids)}."
    )
    return updated, sorted(resolved), sorted(open_ids)
