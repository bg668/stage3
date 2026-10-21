from __future__ import annotations

"""Deterministic reporting from an already reconciled, provenance-checked fact set.

The final stage does NOT ask another model to predict unknown malware behavior.
Validated conclusions keep their epistemic level; unclosed work, superseded
claims, and delivery failures each have a durable disposition. No sample-specific
capability, absence or IOC is presumed here.
"""

import json
from pathlib import Path
from typing import Any

from .source_snippets import build_code_index, report_code_excerpts
from .evidence_validation import validate_canonical_references

from .schemas import (
    CanonicalArtifact, CanonicalClaim, DetectionSpecificity,
    EvidenceLevel, GlobalReconciliationResult, MaterialSuggestion,
)


_STRONG_IOC_ROLES = {"created_output", "created_mutex", "config_marker"}


def _artifact_destination(artifact: CanonicalArtifact) -> str:
    # Detection usefulness and data-flow role are separate. A generic read_source
    # or Windows path must never become an infection IOC from a high score alone.
    if artifact.role in _STRONG_IOC_ROLES and artifact.detection_specificity in {
        DetectionSpecificity.HIGH, DetectionSpecificity.MEDIUM,
    }:
        return "iocs"
    if artifact.role == "persistence_value" and artifact.detection_specificity == DetectionSpecificity.HIGH:
        return "iocs"
    if (artifact.kind == "network" and artifact.role == "other"
            and artifact.detection_specificity == DetectionSpecificity.HIGH):
        return "iocs"
    if artifact.detection_specificity in {DetectionSpecificity.HIGH, DetectionSpecificity.MEDIUM}:
        return "detection_features"
    return "context_artifacts"


def _make_disposition(kind: str, source_id: str, destination: str, target_id: str) -> dict[str, str]:
    return {"kind": kind, "source_id": source_id, "destination": destination, "target_id": target_id}


def project_knowledge(
    canonical: GlobalReconciliationResult,
    *,
    material_suggestions: list[MaterialSuggestion] | None = None,
    input_coverage_warnings: list[dict[str, object]] | None = None,
    source_claim_count: int | None = None,
    source_artifact_count: int | None = None,
    packet_source_dir: Path | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Normalize the final three levels and produce a per-source audit ledger.

    An old saved claim with no explicit evidence_level is sent to the appendix.
    No confidence threshold or API keyword can silently promote it to confirmed.
    """
    core: list[dict[str, Any]] = []
    suspected: list[dict[str, Any]] = []
    leads: list[dict[str, Any]] = []
    iocs: list[dict[str, Any]] = []
    detection: list[dict[str, Any]] = []
    context: list[dict[str, Any]] = []
    dispositions: list[dict[str, str]] = []
    claim_destinations: dict[str, str] = {}
    artifact_destinations: dict[str, str] = {}
    gap_destinations: dict[str, str] = {}

    for claim in canonical.canonical_claims:
        serialized = claim.model_dump(mode="json")
        if claim.evidence_level == EvidenceLevel.CONFIRMED:
            destination = "core_behaviors"
            core.append(serialized)
        elif claim.evidence_level == EvidenceLevel.SUSPECTED:
            destination = "suspected_behaviors"
            suspected.append(serialized)
        else:
            destination = "investigation_leads"
            leads.append({
                "lead_id": f"CLAIM:{claim.canonical_id}",
                "kind": "bounded_claim" if claim.evidence_level == EvidenceLevel.LEAD else "legacy_unclassified_claim",
                "description": claim.behavior,
                "why_retained": (
                    "调查阶段明确标记为待调查线索" if claim.evidence_level == EvidenceLevel.LEAD
                    else "历史调查结果未声明证据等级；保留，不自动升格为已确认"
                ),
                "evidence": [x.model_dump(mode="json") for x in claim.evidence],
                "claim": serialized,
                "source_packets": sorted({x.split(":", 1)[0] for x in claim.source_claim_ids}),
                "historical": True,
            })
        for source_id in claim.source_claim_ids:
            claim_destinations[source_id] = destination
            dispositions.append(_make_disposition("claim", source_id, destination, claim.canonical_id))
        for source_id in claim.superseded_claim_ids:
            claim_destinations[source_id] = "superseded_audit"
            dispositions.append(_make_disposition("claim", source_id, "superseded_audit", claim.canonical_id))

    for artifact in canonical.canonical_artifacts:
        destination = _artifact_destination(artifact)
        payload = artifact.model_dump(mode="json")
        {"iocs": iocs, "detection_features": detection, "context_artifacts": context}[destination].append(payload)
        for source_id in artifact.source_artifact_ids:
            artifact_destinations[source_id] = destination
            dispositions.append(_make_disposition("artifact", source_id, destination, artifact.canonical_id))

    # All historical gaps survive as investigation history, never misrepresented
    # as a freshly assessed list of current unknowns. Individual gap status may
    # be stale because later DAG nodes can already have answered it.
    for idx, gap in enumerate(canonical.gaps, 1):
        gap_id = gap.gap_id or f"H:G{idx:03d}"
        leads.append({
            "lead_id": gap_id,
            "kind": "historical_gap",
            "description": gap.question,
            "why_retained": gap.reason,
            "gap_type": gap.gap_type,
            "impact": gap.impact,
            "source_packets": gap.source_packets,
            "resolved_by_claim_ids": gap.resolved_by_claim_ids,
            "historical": True,
        })
        gap_destinations[gap_id] = "investigation_leads"
        dispositions.append(_make_disposition("gap", gap_id, "investigation_leads", gap_id))

    for idx, conflict in enumerate(canonical.unresolved_conflicts, 1):
        cid = f"CONFLICT:{idx:03d}"
        leads.append({
            "lead_id": cid,
            "kind": "unresolved_conflict",
            "description": conflict.description,
            "why_retained": conflict.reason,
            "source_claim_ids": conflict.claim_ids,
            "impact": conflict.impact,
            "historical": False,
        })
        for source_id in conflict.claim_ids:
            if source_id not in claim_destinations:
                claim_destinations[source_id] = "conflict_audit"
                dispositions.append(_make_disposition("claim", source_id, "conflict_audit", cid))

    for source_id in canonical.unaccounted_input_claim_ids:
        if source_id not in claim_destinations:
            claim_destinations[source_id] = "unaccounted_audit"
            dispositions.append(_make_disposition("claim", source_id, "unaccounted_audit", source_id))
            leads.append({
                "lead_id": f"UNACCOUNTED:{source_id}",
                "kind": "unaccounted_claim",
                "description": f"调查记录 {source_id} 未被全局事实整合消费",
                "why_retained": "结果覆盖异常，需到原始调查记录复核；不能作为已确认行为",
                "source_claim_ids": [source_id], "historical": False,
            })
    for source_id in canonical.unaccounted_input_artifact_ids:
        if source_id not in artifact_destinations:
            artifact_destinations[source_id] = "unaccounted_audit"
            dispositions.append(_make_disposition("artifact", source_id, "unaccounted_audit", source_id))
            leads.append({
                "lead_id": f"UNACCOUNTED:{source_id}",
                "kind": "unaccounted_artifact",
                "description": f"制品记录 {source_id} 未被全局事实整合消费",
                "why_retained": "结果覆盖异常，需到原始调查记录复核",
                "historical": False,
            })

    # Duplicate provenance assignments would make an input look preserved while
    # silently overwriting its destination in the dict; never call that complete.
    destination_collisions = []
    assigned: dict[tuple[str, str], tuple[str, str]] = {}
    for record in dispositions:
        key = (record["kind"], record["source_id"])
        value = (record["destination"], record["target_id"])
        if key in assigned and assigned[key] != value:
            destination_collisions.append({"kind": key[0], "source_id": key[1],
                                           "first": assigned[key], "second": value})
        assigned[key] = value

    warnings = list(input_coverage_warnings or [])
    if canonical.unavailable_packets:
        known = {str(item.get("packet_id")) for item in warnings}
        for packet in canonical.unavailable_packets:
            if packet not in known:
                warnings.append({
                    "packet_id": packet, "delivery_status": "unavailable",
                    "note": "无可消费的结构化结果；原始源码是否提供须独立核查",
                })

    evidence_reference_audit: dict[str, Any]
    if packet_source_dir is not None and packet_source_dir.is_dir():
        packet_paths = sorted(p for p in packet_source_dir.glob("J*.md") if p.is_file())
        if packet_paths:
            evidence_reference_audit = validate_canonical_references(
                canonical, index=build_code_index(packet_paths)
            )
        else:
            evidence_reference_audit = {
                "schema": "evidence_reference_validation_v1",
                "scope": "full_material",
                "status": "not_run",
                "passed": True,
                "reason": "no J*.md source packets found",
            }
    else:
        evidence_reference_audit = {
            "schema": "evidence_reference_validation_v1",
            "scope": "full_material",
            "status": "not_run",
            "passed": True,
            "reason": "packet source directory not supplied",
        }

    issues: list[str] = []
    if destination_collisions:
        issues.append(f"同一来源被分配到不一致的去向：{len(destination_collisions)}")
    if canonical.unaccounted_input_claim_ids:
        issues.append("存在未被全局事实层对账的调查结论")
    if canonical.unaccounted_input_artifact_ids:
        issues.append("存在未被全局事实层对账的制品")
    if source_claim_count is not None and len(claim_destinations) != source_claim_count:
        issues.append(f"调查结论来源数量不匹配: recorded={len(claim_destinations)} expected={source_claim_count}")
    if source_artifact_count is not None and len(artifact_destinations) != source_artifact_count:
        issues.append(f"制品来源数量不匹配: recorded={len(artifact_destinations)} expected={source_artifact_count}")
    if evidence_reference_audit.get("status") != "not_run":
        invalid_segments = int(evidence_reference_audit.get("invalid_segments", 0))
        if invalid_segments:
            issues.append(f"存在无效源码证据引用：{invalid_segments}")
        confirmed_without_valid = evidence_reference_audit.get("confirmed_claims_without_valid_evidence", [])
        if confirmed_without_valid:
            issues.append(
                "已确认行为缺少有效源码证据：" + ", ".join(str(x) for x in confirmed_without_valid)
            )

    audit: dict[str, Any] = {
        "schema": "knowledge_audit_v1",
        "passed": not issues,
        "issues": issues,
        "destination_collisions": destination_collisions,
        "source_claim_count": len(claim_destinations),
        "source_artifact_count": len(artifact_destinations),
        "source_gap_count": len(gap_destinations),
        "claim_dispositions": claim_destinations,
        "artifact_dispositions": artifact_destinations,
        "gap_dispositions": gap_destinations,
        "dispositions": dispositions,
        "superseded_claim_ids": sorted({sid for c in canonical.canonical_claims for sid in c.superseded_claim_ids}),
        "unaccounted_claim_ids": list(canonical.unaccounted_input_claim_ids),
        "unaccounted_artifact_ids": list(canonical.unaccounted_input_artifact_ids),
        "delivery_coverage_warnings": warnings,
        "evidence_reference_audit": evidence_reference_audit,
    }
    report: dict[str, Any] = {
        "schema": "report_ready_v2",
        "status": "complete" if audit["passed"] else "incomplete_provenance",
        "audit_issues": audit["issues"],
        "reconciliation_status": canonical.status.value,
        "core_behaviors": core,
        "suspected_behaviors": suspected,
        "investigation_leads": leads,
        "iocs": iocs,
        "detection_features": detection,
        "context_artifacts": context,
        "unresolved_conflicts": [x.model_dump(mode="json") for x in canonical.unresolved_conflicts],
        "material_suggestions": [x.model_dump(mode="json") for x in (material_suggestions or [])],
        "input_coverage_warnings": warnings,
        "superseded_claim_ids": audit["superseded_claim_ids"],
        "audit_file": "_knowledge_audit.json",
        "evidence_reference_audit_file": "_evidence_reference_audit.json",
        # Machine consumers can still reach all original canonical records.
        "canonical_claims": [x.model_dump(mode="json") for x in canonical.canonical_claims],
        "canonical_artifacts": [x.model_dump(mode="json") for x in canonical.canonical_artifacts],
    }
    report["code_excerpts"] = report_code_excerpts(report, packet_source_dir)
    report["source_excerpts_available"] = bool(report["code_excerpts"])
    return report, audit


def _short(value: str, limit: int = 250) -> str:
    value = " ".join(value.split())
    return value if len(value) <= limit else value[:limit-1] + "…"


def _valuable_limitations(limitations: list[str]) -> list[str]:
    """Only report limitations materially specific to the code under discussion.

    Source conclusions remain untouched in report-ready JSON. This filter only
    affects human-facing display; it never changes claims, evidence or audit.
    """
    boilerplate = (
        "静态分析不能证明", "静态分析无法证明", "无法确认运行时一定",
        "运行时一定成功", "不能断言每次运行", "不代表运行时",
        "不能直接断言实际", "不证明运行时", "调用存在不证明",
        "尚不能仅凭这些调用关系", "api 操作成功", "does not prove runtime",
        "runtime success cannot", "may fail at runtime",
    )
    return [x for x in limitations if not any(phrase in x.lower() for phrase in boilerplate)]


def render_knowledge_markdown(report: dict[str, Any]) -> str:
    lines: list[str] = [
        "# 恶意代码调查报告", "",
        "## 一、核心已确认行为", "",
    ]
    if report.get("audit_issues"):
        lines.extend(["**完整性警告：** " + "；".join(report["audit_issues"]), ""])
    core = report["core_behaviors"]
    if not core:
        lines.append("本轮没有被明确标记为已确认的结构化行为结论；历史未分级记录保留在附录。")
    for item in core:
        lines.append(f"**{item['canonical_id']} | {item['behavior']}**")
        lines.append(f"条件：{_short(item['condition'])}；操作：{_short(item['action'])}；结果：{_short(item['sink'])}。")
        evidence = ", ".join(x["location"] for x in item["evidence"])
        lines.append(f"证据：{evidence}。")
        snippets = report.get("code_excerpts", {}).get(item["canonical_id"], [])
        if isinstance(snippets, dict):
            # Compatibility with v1.7.2 report JSON.
            snippets = [snippets]
        for snippet in snippets:
            lines.extend([
                f"关键源码（{snippet['function_id']}，来自 {', '.join(snippet['source_packets'])}，"
                f"原始证据 {snippet['evidence_location']}）：",
                "```text",
                snippet["code"],
                "```",
            ])
        relevant = _valuable_limitations(item.get("limitations", []))
        if relevant:
            lines.append("重要限制：" + "；".join(_short(x) for x in relevant) + "。")
        lines.append("")
    lines.extend(["## 二、疑似行为", ""])
    if not report["suspected_behaviors"]:
        lines.append("本轮没有明确分级为疑似的行为结论。")
    for item in report["suspected_behaviors"]:
        name = item["behavior"]
        label = name if name.startswith("疑似") else f"疑似{name}"
        lines.extend([
            f"**{item['canonical_id']} | {label}**",
            f"线索：{_short(item['source'])} → {_short(item['action'])} → {_short(item['sink'])}。",
            "证据：" + ", ".join(x["location"] for x in item["evidence"]) + "。",
        ])
        snippets = report.get("code_excerpts", {}).get(item["canonical_id"], [])
        if isinstance(snippets, dict):
            snippets = [snippets]
        for snippet in snippets:
            lines.extend([
                f"关键源码（{snippet['function_id']}，来自 {', '.join(snippet['source_packets'])}，"
                f"原始证据 {snippet['evidence_location']}）：",
                "```text",
                snippet["code"],
                "```",
            ])
        useful = _valuable_limitations(item.get("limitations", []))
        if useful:
            lines.append("关键未闭合点：" + "；".join(_short(x) for x in useful) + "。")
        lines.append("")
    lines.extend(["## 三、IOC 与检测特征", "", "### 高价值 IOC", ""])
    if not report["iocs"]:
        lines.append("未形成独立的高价值 IOC。")
    for item in report["iocs"]:
        lines.append(f"- {item['canonical_id']} `{item['value'].replace('`', '')}`（{item['kind']}，{item['role']}，{item['detection_specificity']}）")
    lines.extend(["", "### 关联检测特征", ""])
    if not report["detection_features"]:
        lines.append("无额外关联检测特征。")
    for item in report["detection_features"]:
        lines.append(f"- {item['canonical_id']} `{item['value'].replace('`', '')}`（{item['role']}，{item['detection_specificity']}）")
    lines.extend(["", "## 四、分析范围与交付情况", ""])
    lines.append("以下仅说明本次调查交付状态；历史待调查问题不等于当前尚未解决的行为。")
    if report["input_coverage_warnings"]:
        for item in report["input_coverage_warnings"]:
            lines.append(f"- {item.get('packet_id', 'unknown')}: {item.get('delivery_status', 'unknown')}。")
    else:
        lines.append("未记录结构化交付异常。")
    if report["material_suggestions"]:
        lines.extend(["", "## 五、可补充材料建议（条件式建议，不代表样本缺失）", ""])
        for item in report["material_suggestions"]:
            lines.append(f"- {item['recommendation']}（来源 {item['scope_id']}）")
    lines.extend([
        "", "## 附录 A：待调查与历史线索", "",
        "本附录保留调查过程中形成的有效线索及历史问题。它们可能已由后续证据回答，不能直接解释为当前仍然未知。", "",
    ])
    for item in report["investigation_leads"]:
        kind = item["kind"]
        lines.append(f"- **{item['lead_id']} [{kind}]** {_short(item['description'], 350)}")
        if kind in {"legacy_unclassified_claim", "bounded_claim"}:
            if kind == "legacy_unclassified_claim":
                lines.append("  - 历史未分级：保留原始判断和证据，不自动升格为确认。")
            claim = item["claim"]
            lines.append("  - 依据：" + ", ".join(x["location"] for x in claim["evidence"]))
    if not report["investigation_leads"]:
        lines.append("无。")
    lines.extend(["", "## 附录 B：其他材料与审计去向", ""])
    for item in report["context_artifacts"]:
        lines.append(f"- {item['canonical_id']} `{item['value'].replace('`', '')}`（{item['role']}）")
    if report["superseded_claim_ids"]:
        lines.append("已被更强结论覆盖的历史来源：" + ", ".join(report["superseded_claim_ids"]))
    lines.extend([
        "", "完整 Claim、Artifact、证据与来源去向见 `_report_ready.json` 和 `_knowledge_audit.json`。", "",
    ])
    return "\n".join(lines)


def save_knowledge_outputs(
    directory: Path,
    canonical: GlobalReconciliationResult,
    *,
    material_suggestions: list[MaterialSuggestion] | None = None,
    input_coverage_warnings: list[dict[str, object]] | None = None,
    source_claim_count: int | None = None,
    source_artifact_count: int | None = None,
    packet_source_dir: Path | None = None,
) -> tuple[Path, Path, Path, dict[str, Any]]:
    directory.mkdir(parents=True, exist_ok=True)
    report, audit = project_knowledge(
        canonical,
        material_suggestions=material_suggestions,
        input_coverage_warnings=input_coverage_warnings,
        source_claim_count=source_claim_count,
        source_artifact_count=source_artifact_count,
        packet_source_dir=packet_source_dir,
    )
    json_path = directory / "_report_ready.json"
    markdown_path = directory / "_final_report.md"
    audit_path = directory / "_knowledge_audit.json"
    evidence_audit_path = directory / "_evidence_reference_audit.json"
    evidence_audit_path.write_text(
        json.dumps(audit.get("evidence_reference_audit", {}), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    audit_path.write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    markdown_path.write_text(render_knowledge_markdown(report), encoding="utf-8")
    return json_path, markdown_path, audit_path, audit
