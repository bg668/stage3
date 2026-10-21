from __future__ import annotations

"""Read-only final evidence assessment and deterministic report projection.

Canonical facts are authoritative within their stated evidence limits. Historical
questions are audit hints only. Automatically indexed inputs, independently
validated material requests and packet-delivery quality have separate meanings.
"""

import json
from dataclasses import dataclass
from pathlib import Path

from .evidence_scope import (
    EvidenceScope, MaterialScopeItem, PacketCoverage, coverage_warnings,
)
from .schemas import (
    FinalEvidenceAssessment, FinalUncertainty, GlobalReconciliationResult,
    MaterialSuggestion,
)


class FinalAssessmentProvenanceError(ValueError):
    """Assessment referenced unsupported or incorrectly classified evidence."""


@dataclass(frozen=True)
class FinalAssessmentProjection:
    text: str
    claim_ids: frozenset[str]
    artifact_ids: frozenset[str]
    gap_ids: frozenset[str]
    source_gap_count: int
    source_claim_count: int
    source_artifact_count: int
    scope_items: dict[str, MaterialScopeItem]
    packet_items: dict[str, PacketCoverage]
    source_packet_ids: frozenset[str]
    included_historical_gap_count: int


def _historical_gap_priority(entry: tuple[str, object]) -> tuple[int, int, str]:
    gid, item = entry
    # Context ranking, NOT an automatic close/open judgement.
    impact = {"critical": 0, "major": 1, "minor": 2}
    category = {"report": 0, "evidence": 1, "capability": 2, "investigation": 3}
    return (category.get(item.gap_type, 3), impact.get(item.impact, 2), gid)


def _select_history_gaps(gaps: list[tuple[str, object]], limit: int = 36) -> list[tuple[str, object]]:
    """Limit process noise without removing critical report/evidence gaps."""
    ranked = sorted(gaps, key=_historical_gap_priority)
    mandatory = [(gid, gap) for gid, gap in ranked
                 if (gap.gap_type in {"report", "evidence"} and gap.impact in {"critical", "major"})
                 or (gap.gap_type == "capability" and gap.impact == "critical")]
    chosen = mandatory[:]
    seen = {gid for gid, _ in chosen}
    for gid, gap in ranked:
        if len(chosen) >= max(limit, len(mandatory)):
            break
        if gid not in seen:
            chosen.append((gid, gap))
            seen.add(gid)
    return chosen


def project_final_assessment_input(
    result: GlobalReconciliationResult,
    *,
    evidence_scope: EvidenceScope | None = None,
    packet_coverage: list[PacketCoverage] | None = None,
) -> FinalAssessmentProjection:
    """Bounded canonical facts, observed input inventory and actual material needs.

    A raw-only packet is a delivery failure, not proof of absent original
    material. 'not_in_input' means not supplied to THIS run only; an explicit
    attestation is required for a stronger 'unavailable' assertion.
    """
    evidence_scope = evidence_scope or EvidenceScope()
    packet_coverage = list(packet_coverage or [])
    claim_ids = frozenset(c.canonical_id for c in result.canonical_claims)
    artifact_ids = frozenset(a.canonical_id for a in result.canonical_artifacts)
    gap_records: list[tuple[str, object]] = []
    seen_gaps: set[str] = set()
    for index, gap in enumerate(result.gaps, 1):
        gid = gap.gap_id or f"R:LEGACY_G{index:03d}"
        if gid in seen_gaps:
            continue
        seen_gaps.add(gid)
        gap_records.append((gid, gap))

    packet_items = {item.packet_id: item for item in packet_coverage}
    # Canonical provenance can attest *which packet* contributed a fact, even
    # when the replay directory no longer contains a full Jxx delivery envelope.
    # It cannot prove the packet's delivery status.
    source_packet_ids = set(packet_items)
    for claim in result.canonical_claims:
        source_packet_ids.update(src.split(":", 1)[0] for src in claim.source_claim_ids if ":" in src)
    for artifact in result.canonical_artifacts:
        source_packet_ids.update(src.split(":", 1)[0] for src in artifact.source_artifact_ids if ":" in src)
    for gap in result.gaps:
        source_packet_ids.update(gap.source_packets)
    source_packet_ids.update(result.unavailable_packets)
    for pid in result.unavailable_packets:
        if pid not in packet_items:
            packet_items[pid] = PacketCoverage(
                packet_id=pid, delivery_status="unknown",
                note="Canonical marks result unavailable; source code availability is unknown",
            )

    lines = [
        "<assessment_scope>",
        "Canonical claims/artifacts are confirmed only to the strength of their code evidence.",
        "Historical gaps are non-authoritative investigation history, not final unknowns.",
        "Packet delivery failure means structured result unavailable; NEVER means function/source bytes absent.",
        "Material requests and their availability are handled independently by deterministic suggestion generation.",
        "Do NOT turn a material request, an unobserved file, or an unknown material status into a report-level uncertainty.",
        "If a missing piece of material could help future work, the program will provide a conditional suggestion separately.",
        "A Worker delivery failure is a system coverage warning, NOT proof original material is missing.",
        f"reconciliation_status: {result.status.value}",
        f"unaccounted_claims: {', '.join(result.unaccounted_input_claim_ids) or 'none'}",
        f"unaccounted_artifacts: {', '.join(result.unaccounted_input_artifact_ids) or 'none'}",
        "</assessment_scope>",
        "<material_followup_policy>",
        f"The program tracks {len(evidence_scope.materials)} material requests separately; their status is not a final behavioral unknown.",
        "Do not restate or reconstruct material absence from historical notes; no sample-specific materials are mandatory.",
        "Only evaluate behavioral uncertainty supported by canonical facts and evidence limitations.",
        "</material_followup_policy>",
    ]
    lines.append("<observed_input_inventory>")
    if evidence_scope.inventory_summary:
        for key, val in evidence_scope.inventory_summary.items():
            lines.append(f"- {key}: {val}")
    else:
        lines.append("not supplied; do not infer completeness of input code or resource files")
    lines.append("</observed_input_inventory>")

    lines.append("<packet_delivery_coverage>")
    if not packet_items:
        lines.append("Packet status detail unavailable; do not infer file/source absence.")
    for pid, item in sorted(packet_items.items()):
        lines.append(
            f"- {pid}: {item.delivery_status}; validated_claims={item.validated_claims}; "
            f"validated_artifacts={item.validated_artifacts}; note={item.note or 'none'}"
        )
    lines.append("</packet_delivery_coverage>")

    lines.append("<canonical_claims>")
    for claim in result.canonical_claims:
        ev = "; ".join(f"{e.location}: {e.observation}" for e in claim.evidence)
        lines.append(
            f"- {claim.canonical_id} [{claim.confidence.value}] {claim.behavior}; "
            f"when={claim.condition}; {claim.source} -> {claim.action} -> {claim.sink}; "
            f"evidence={ev}; limits={'; '.join(claim.limitations) or 'none'}"
        )
    lines.append("</canonical_claims>")

    lines.append("<canonical_artifacts>")
    for artifact in result.canonical_artifacts:
        lines.append(
            f"- {artifact.canonical_id}: {artifact.kind} {artifact.value}; "
            f"role={artifact.role}; specificity={artifact.detection_specificity.value}; "
            f"meaning={artifact.meaning}"
        )
    lines.append("</canonical_artifacts>")

    lines.append("<unresolved_claim_conflicts>")
    if not result.unresolved_conflicts:
        lines.append("none")
    for conflict in result.unresolved_conflicts:
        lines.append(
            f"- {','.join(conflict.claim_ids)} [{conflict.impact}] "
            f"{conflict.description}; reason={conflict.reason}"
        )
    lines.append("</unresolved_claim_conflicts>")

    selected_gaps = _select_history_gaps(gap_records)
    lines.append("<selected_historical_gaps>")
    lines.append(
        f"Selected {len(selected_gaps)} of {len(gap_records)} process notes by type/impact; "
        "omitted notes remain in the audit file, not evidence of resolution."
    )
    for gid, gap in selected_gaps:
        lines.append(
            f"- {gid} [{gap.gap_type}/{gap.impact}] {gap.question}; "
            f"reason={gap.reason}; source_packets={','.join(gap.source_packets)}"
        )
    lines.append("</selected_historical_gaps>")

    return FinalAssessmentProjection(
        text="\n".join(lines),
        claim_ids=claim_ids,
        artifact_ids=artifact_ids,
        gap_ids=frozenset(seen_gaps),
        source_gap_count=len(gap_records),
        source_claim_count=len(claim_ids),
        source_artifact_count=len(artifact_ids),
        scope_items={x.scope_id: x for x in evidence_scope.materials},
        packet_items=packet_items,
        source_packet_ids=frozenset(source_packet_ids),
        included_historical_gap_count=len(selected_gaps),
    )


_MATERIAL_ONLY_TYPES = frozenset({
    "material_unavailable", "material_not_in_input", "material_not_analyzed", "analysis_incomplete",
})


def separate_operational_uncertainties(
    assessment: FinalEvidenceAssessment,
) -> tuple[FinalEvidenceAssessment, list[dict[str, object]]]:
    """Keep evidence-limited behavioral unknowns; audit the other categories.

    This prevents a format-valid but unsupported `material_not_in_input` response
    from turning the entire assessment into RAW_ONLY. Material suggestions come
    *only* from the independently checked input inventory, never from the LLM.
    """
    remaining: list[FinalUncertainty] = []
    redirected: list[dict[str, object]] = []
    for item in assessment.remaining_uncertainties:
        if item.uncertainty_type in _MATERIAL_ONLY_TYPES:
            redirected.append({
                "reason": "handled_by_inventory_or_delivery_coverage",
                "item": item.model_dump(mode="json"),
            })
        else:
            remaining.append(item)
    # The old model summary may also have material-absence assertions. When the
    # output had to be rerouted, never publish that summary as an audited fact.
    summary = assessment.summary
    if redirected:
        for record in redirected:
            record["original_summary"] = assessment.summary
        summary = "根据已归并事实，仅保留有证据来源的行为级未知项；补充材料建议在独立清单中列出。"
    return assessment.model_copy(update={"summary": summary, "remaining_uncertainties": remaining}), redirected


_BENEFIT_BY_KIND = {
    "function_code": "核对具体分支、调用条件和数据流向",
    "resource_bytes": "检查资源正文、解码结果及相关行为条件",
    "data_bytes": "解析所引用的数据结构或载荷内容",
    "runtime_trace": "验证条件分支是否被触发，以及操作是否实际成功",
    "other": "进一步核实未闭合的证据关系",
}


def build_material_suggestions(scope: EvidenceScope) -> list[MaterialSuggestion]:
    """Produce conditional, non-assertive hints from real Worker material needs.

    No LLM call, no fabricated material requirement, no claim of sample absence.
    Available materials already submitted to a Worker never become suggestions.
    """
    output: list[MaterialSuggestion] = []
    for item in sorted(scope.materials, key=lambda x: (
        {"critical": 0, "major": 1, "minor": 2}[x.impact], x.scope_id,
    )):
        if item.status == "available" and item.submission_status != "not_submitted":
            continue
        if item.status == "unknown" and not item.requested_by_packets and not item.request_reason:
            # Unverified expert inventory entry, not an actual requested follow-up.
            continue
        if item.status == "available":
            action = "submit_for_analysis"
            intro = f"如将本次输入中已有的「{item.label}」提交给后续调查"
        elif item.status == "unknown":
            action = "confirm_or_provide"
            intro = f"如能确认或补充「{item.label}」的实际内容"
        elif item.status == "partial":
            action = "provide"
            intro = f"如补充「{item.label}」尚未覆盖的部分"
        else:
            # Even an attested unavailable item is a suggestion, not a report fact.
            action = "provide"
            intro = f"如后续能提供「{item.label}」"
        benefit = item.request_reason or _BENEFIT_BY_KIND.get(item.kind or "", _BENEFIT_BY_KIND["other"])
        output.append(MaterialSuggestion(
            scope_id=item.scope_id,
            target=item.target or item.label,
            kind=item.kind or "other",
            availability=item.status,
            action=action,
            recommendation=f"{intro}，可进一步分析：{benefit}。",
            expected_benefit=benefit,
            impact=item.impact,
            requested_by_packets=list(item.requested_by_packets),
            request_evidence=list(item.request_evidence),
        ))
    return output


def validate_assessment(
    assessment: FinalEvidenceAssessment,
    projection: FinalAssessmentProjection,
) -> FinalEvidenceAssessment:
    """Hard checks for provenance and cause type, never accept arbitrary facts."""
    errors: list[str] = []
    scope_ids = frozenset(projection.scope_items)
    packet_ids = projection.source_packet_ids
    for i, item in enumerate(assessment.remaining_uncertainties, 1):
        refs = (set(item.related_claim_ids) | set(item.related_artifact_ids)
                | set(item.related_gap_ids) | set(item.related_scope_ids)
                | set(item.related_packet_ids))
        if not refs:
            errors.append(f"uncertainty[{i}]: lacks any source claim/artifact/gap/scope/packet ID")
        for attr, actual, allowed in (
            ("related_claim_ids", item.related_claim_ids, projection.claim_ids),
            ("related_artifact_ids", item.related_artifact_ids, projection.artifact_ids),
            ("related_gap_ids", item.related_gap_ids, projection.gap_ids),
            ("related_scope_ids", item.related_scope_ids, scope_ids),
            ("related_packet_ids", item.related_packet_ids, packet_ids),
        ):
            unknown = sorted(set(actual) - allowed)
            if unknown:
                errors.append(f"uncertainty[{i}].{attr}: unknown IDs {unknown}")
        if item.uncertainty_type in _MATERIAL_ONLY_TYPES:
            errors.append(
                f"uncertainty[{i}]: {item.uncertainty_type} must be kept out of final behavioral unknowns; "
                "use inventory-derived material suggestions or packet delivery warnings"
            )
        # A material-only / packet-delivery item belongs outside report unknowns.
        # Other uncertainties still require correct provenance references.
    if errors:
        raise FinalAssessmentProvenanceError("; ".join(errors))
    return assessment


def ensure_material_scope_coverage(
    assessment: FinalEvidenceAssessment,
    projection: FinalAssessmentProjection,
) -> tuple[FinalEvidenceAssessment, list[str]]:
    """Deprecated compatibility shim: v1.6 never converts absent material into unknowns."""
    return assessment, []



def report_projection(
    canonical: GlobalReconciliationResult,
    assessment: FinalEvidenceAssessment,
    *,
    material_scope: list[dict[str, object]] | None = None,
    input_coverage_warnings: list[dict[str, object]] | None = None,
    fallback_material_scope_ids: list[str] | None = None,
    material_suggestions: list[MaterialSuggestion] | None = None,
) -> dict[str, object]:
    """Only validated canonical facts and assessed report unknowns, no history TODOs."""
    return {
        "schema": "report_ready_v1",  # additive extension of the existing output contract
        "reconciliation_status": canonical.status.value,
        "canonical_summary": canonical.summary,
        "final_assessment_summary": assessment.summary,
        "canonical_claims": [c.model_dump(mode="json") for c in canonical.canonical_claims],
        "canonical_artifacts": [a.model_dump(mode="json") for a in canonical.canonical_artifacts],
        "remaining_uncertainties": [u.model_dump(mode="json") for u in assessment.remaining_uncertainties],
        "unresolved_conflicts": [c.model_dump(mode="json") for c in canonical.unresolved_conflicts],
        "material_scope": material_scope or [],  # audit only
        "material_suggestions": [x.model_dump(mode="json") for x in (material_suggestions or [])],
        "input_coverage_warnings": input_coverage_warnings or [],
        "fallback_material_scope_ids": fallback_material_scope_ids or [],
        "unavailable_packets": list(canonical.unavailable_packets),
        "unaccounted_input_claim_ids": list(canonical.unaccounted_input_claim_ids),
        "unaccounted_input_artifact_ids": list(canonical.unaccounted_input_artifact_ids),
    }


def render_report_markdown(
    canonical: GlobalReconciliationResult,
    assessment: FinalEvidenceAssessment,
    *,
    input_coverage_warnings: list[dict[str, object]] | None = None,
    material_suggestions: list[MaterialSuggestion] | None = None,
) -> str:
    lines = [
        "# 恶意代码静态调查结果（已归并事实）", "",
        "以下为已验证调查结果的整理，不表示运行时必然执行成功。", "",
        f"**事实整合状态：** {canonical.status.value}。 **最终证据评估：** {assessment.summary}",
        "", "## 已归并行为事实", "",
    ]
    if not canonical.canonical_claims:
        lines.append("无可用的已验证行为事实。")
    for c in canonical.canonical_claims:
        lines.extend([
            f"**{c.canonical_id} | {c.behavior}**（{c.confidence.value}）",
            f"条件：{c.condition}；来源：{c.source}；操作：{c.action}；去向：{c.sink}。",
            "证据：" + "；".join(e.location for e in c.evidence) + "。",
        ])
        if c.limitations:
            lines.append("限制：" + "；".join(c.limitations) + "。")
        lines.append("")
    lines.extend(["## 制品与检测线索", ""])
    if not canonical.canonical_artifacts:
        lines.append("未获得已验证制品。")
    for a in canonical.canonical_artifacts:
        lines.append(
            f"- {a.canonical_id} | `{a.value.replace('`', '')}` | {a.kind} | "
            f"角色={a.role}，检测特异性={a.detection_specificity.value}"
        )
    lines.extend(["", "## 仍需验证的行为与执行结果", ""])
    if not assessment.remaining_uncertainties:
        lines.append("当前已验证事实未形成新的行为级不确定性；不代表已验证运行时成功。")
    for i, u in enumerate(assessment.remaining_uncertainties, 1):
        ids = u.related_claim_ids + u.related_artifact_ids + u.related_gap_ids + u.related_scope_ids + u.related_packet_ids
        lines.append(
            f"{i}. **[{u.impact}/{u.uncertainty_type}] {u.question}**：{u.reason}（关联：{', '.join(ids)}）"
        )
    if material_suggestions:
        lines.extend(["", "## 后续可补充的分析材料（建议，不代表样本缺失）", ""])
        for suggestion in material_suggestions:
            lines.append(f"- {suggestion.recommendation}（来源：{suggestion.scope_id}）")
    if canonical.unresolved_conflicts:
        lines.extend(["", "## 尚未消解的事实冲突", ""])
        for conflict in canonical.unresolved_conflicts:
            lines.append(f"- {conflict.description}（{', '.join(conflict.claim_ids)}）")
    if input_coverage_warnings:
        lines.extend(["", "## 调查结果交付覆盖警告（不代表原始材料缺失）", ""])
        for entry in input_coverage_warnings:
            lines.append(
                f"- {entry.get('packet_id')}：{entry.get('delivery_status')}；"
                f"validated_claims={entry.get('validated_claims', 0)}；"
                f"validated_artifacts={entry.get('validated_artifacts', 0)}"
            )
    lines.append("")
    return "\n".join(lines)


def save_report_outputs(
    directory: Path,
    canonical: GlobalReconciliationResult,
    assessment: FinalEvidenceAssessment,
    *,
    material_scope: list[dict[str, object]] | None = None,
    input_coverage_warnings: list[dict[str, object]] | None = None,
    fallback_material_scope_ids: list[str] | None = None,
    material_suggestions: list[MaterialSuggestion] | None = None,
) -> tuple[Path, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    json_path = directory / "_report_ready.json"
    md_path = directory / "_final_report.md"
    json_path.write_text(
        json.dumps(report_projection(
            canonical, assessment,
            material_scope=material_scope,
            input_coverage_warnings=input_coverage_warnings,
            fallback_material_scope_ids=fallback_material_scope_ids,
            material_suggestions=material_suggestions,
        ), ensure_ascii=False, indent=2), encoding="utf-8",
    )
    md_path.write_text(render_report_markdown(
        canonical, assessment, input_coverage_warnings=input_coverage_warnings,
        material_suggestions=material_suggestions,
    ), encoding="utf-8")
    return json_path, md_path
