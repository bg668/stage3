"""v1.6: material availability is a follow-up suggestion, never a malware fact."""
from __future__ import annotations

import json

import pytest

from bounded_investigator.evidence_scope import EvidenceScope, MaterialScopeItem, PacketCoverage
from bounded_investigator.final_assessment import (
    FinalAssessmentProvenanceError,
    build_material_suggestions,
    project_final_assessment_input,
    render_report_markdown,
    report_projection,
    separate_operational_uncertainties,
    validate_assessment,
)
from bounded_investigator.material_inventory import build_automatic_scope, build_input_inventory
from bounded_investigator.schemas import (
    DeliveryStatus, EvidenceRef, FinalEvidenceAssessment, FinalUncertainty,
    InvestigationResult, InvestigationStatus, MaterialNeed, RunEnvelope, UsageSummary,
)
from test_final_assessment import canonical


def entry(scope_id, status, *, submit="unknown", kind="function_code", reason="检查远程调用入口", packets=None):
    return MaterialScopeItem(
        scope_id=scope_id, label=f"{kind}: sample_target", kind=kind, target="sample_target",
        status=status, impact="major", requested_by_packets=packets or ["J01"],
        request_evidence=["F-0100 L5"], request_reason=reason, submission_status=submit,
        source="indexed input", details="Observed input scope verified by index",
    )


def test_no_preseeded_material_suggestions():
    assert build_material_suggestions(EvidenceScope()) == []


def test_not_in_input_generates_only_a_conditional_hint():
    scope = EvidenceScope(materials=[entry("requested_1", "not_in_input")])
    suggestions = build_material_suggestions(scope)
    assert len(suggestions) == 1
    hint = suggestions[0]
    assert hint.scope_id == "requested_1"
    assert hint.target == "sample_target"
    assert hint.action == "provide"
    assert "如后续能提供" in hint.recommendation
    assert "检查远程调用入口" in hint.recommendation
    assert all(word not in hint.recommendation for word in ("样本缺失", "样本不存在", "未提供源码"))
    assert hint.requested_by_packets == ["J01"]
    assert hint.request_evidence == ["F-0100 L5"]


def test_unknown_partial_and_available_are_not_treated_as_absent():
    scope = EvidenceScope(materials=[
        entry("known", "available", submit="submitted_to_worker"),
        entry("unread", "available", submit="not_submitted"),
        entry("partial", "partial", submit="submitted_to_worker"),
        entry("unknown", "unknown"),
    ])
    suggestions = {x.scope_id: x for x in build_material_suggestions(scope)}
    assert "known" not in suggestions
    assert suggestions["unread"].action == "submit_for_analysis"
    assert "本次输入中已有" in suggestions["unread"].recommendation
    assert "尚未覆盖的部分" in suggestions["partial"].recommendation
    assert suggestions["unknown"].action == "confirm_or_provide"
    assert "确认或补充" in suggestions["unknown"].recommendation


def test_expert_unknown_without_worker_request_is_not_a_material_hint():
    item = entry("unknown", "unknown", packets=[])
    item.requested_by_packets = []
    item.request_reason = ""
    assert build_material_suggestions(EvidenceScope(materials=[item])) == []


def test_material_and_delivery_outputs_are_redirected_but_behavior_stays():
    assessment = FinalEvidenceAssessment(summary="facts", remaining_uncertainties=[
        FinalUncertainty(question="not found", impact="major", reason="missing", uncertainty_type="material_not_in_input"),
        FinalUncertainty(question="resource missing", impact="major", reason="material", uncertainty_type="material_unavailable"),
        FinalUncertainty(question="packet failed", impact="minor", reason="JSON invalid", uncertainty_type="analysis_incomplete"),
        FinalUncertainty(question="success at runtime unknown", impact="major", reason="static only", uncertainty_type="runtime_unverified", related_claim_ids=["G:C001"]),
    ])
    cleaned, redirected = separate_operational_uncertainties(assessment)
    assert len(redirected) == 3
    assert len(cleaned.remaining_uncertainties) == 1
    assert cleaned.remaining_uncertainties[0].uncertainty_type == "runtime_unverified"
    assert validate_assessment(cleaned, project_final_assessment_input(canonical())) is cleaned
    with pytest.raises(FinalAssessmentProvenanceError):
        validate_assessment(assessment, project_final_assessment_input(canonical()))


def test_invalid_behavioral_source_is_still_rejected():
    bad = FinalEvidenceAssessment(summary="facts", remaining_uncertainties=[
        FinalUncertainty(question="unknown", impact="major", reason="test", uncertainty_type="unresolved_behavior", related_claim_ids=["G:C999"])
    ])
    cleaned, redirected = separate_operational_uncertainties(bad)
    assert not redirected
    with pytest.raises(FinalAssessmentProvenanceError):
        validate_assessment(cleaned, project_final_assessment_input(canonical()))


def test_report_material_hint_is_independent_from_unknown_and_delivery_warning():
    scope = EvidenceScope(materials=[entry("requested_1", "not_in_input")])
    assessment = FinalEvidenceAssessment(summary="behavior validated")
    warnings = [PacketCoverage(packet_id="J08", delivery_status="raw_only").model_dump()]
    suggestions = build_material_suggestions(scope)
    report = report_projection(canonical(), assessment,
                               input_coverage_warnings=warnings, material_suggestions=suggestions)
    assert report["remaining_uncertainties"] == []
    assert report["input_coverage_warnings"][0]["packet_id"] == "J08"
    assert [s["scope_id"] for s in report["material_suggestions"]] == ["requested_1"]
    rendered = render_report_markdown(canonical(), assessment, input_coverage_warnings=warnings,
                                      material_suggestions=suggestions)
    assert "后续可补充的分析材料" in rendered
    assert "检查远程调用入口" in rendered
    assert "调查结果交付覆盖警告" in rendered
    assert "已确认样本缺失" not in rendered


def test_real_worker_material_need_remains_actionable_without_gold(tmp_path):
    packet = tmp_path / "J01.md"
    packet.write_text("## F-0101 sub_401010，原文件 L0001–L0002 / 共 2 行\n```text\nL0001: ret;\nL0002: ret;\n```", encoding="utf-8")
    actual = RunEnvelope(packet_name="J01", elapsed_seconds=1, delivery_status=DeliveryStatus.SUCCESS,
                         revision_performed=False, usage=UsageSummary(), result=InvestigationResult(
                             status=InvestigationStatus.COMPLETE_WITH_GAPS, summary="Observed unresolved call",
                             material_needs=[MaterialNeed(
                                 kind="function_code", target="sub_401234",
                                 reason="查明远程线程创建条件", evidence=[EvidenceRef(location="F-0101 L2", observation="call sub_401234")]
                             )],
                         ))
    scope, requests = build_automatic_scope(build_input_inventory([packet]), {"J01": actual})
    assert len(requests) == 1
    assert scope.materials[0].status == "not_in_input"
    suggestions = build_material_suggestions(scope)
    assert len(suggestions) == 1
    assert "查明远程线程创建条件" in suggestions[0].recommendation
    assert "sub_401234" in suggestions[0].recommendation
    assert "sub_401234" not in project_final_assessment_input(canonical(), evidence_scope=scope).text


def test_canonical_source_packet_ids_are_accepted_without_saved_packet_envelopes():
    facts = canonical()
    projection = project_final_assessment_input(facts)
    assert "J12" in projection.source_packet_ids
    assert "J00" in projection.source_packet_ids
    good = FinalEvidenceAssessment(summary="Static scope only", remaining_uncertainties=[
        FinalUncertainty(question="Runtime result unknown", impact="major", reason="Only static evidence",
                         uncertainty_type="runtime_unverified", related_claim_ids=["G:C001"], related_packet_ids=["J12"])
    ])
    assert validate_assessment(good, projection) is good
    bad = good.model_copy(deep=True)
    bad.remaining_uncertainties[0].related_packet_ids = ["J99"]
    with pytest.raises(FinalAssessmentProvenanceError):
        validate_assessment(bad, projection)


def test_redirected_material_unknown_does_not_leak_into_summary_or_invent_a_suggestion():
    original = FinalEvidenceAssessment(
        summary="样本缺失资源，因此注入和C2均未知", remaining_uncertainties=[
            FinalUncertainty(question="No data", reason="not found", impact="major",
                             uncertainty_type="material_not_in_input", related_scope_ids=["fictional_scope"])
        ],
    )
    clean, audit = separate_operational_uncertainties(original)
    assert clean.remaining_uncertainties == []
    assert "缺失资源" not in clean.summary
    assert audit[0]["original_summary"] == original.summary
    assert validate_assessment(clean, project_final_assessment_input(canonical())) is clean
    assert build_material_suggestions(EvidenceScope()) == []


def test_report_files_serialize_suggestions_without_promoting_material_to_behavior(tmp_path):
    from bounded_investigator.final_assessment import save_report_outputs
    facts = canonical()
    scope = EvidenceScope(materials=[entry("requested_1", "partial")])
    suggestions = build_material_suggestions(scope)
    doc, md = save_report_outputs(tmp_path, facts, FinalEvidenceAssessment(summary="bounded"),
                                  material_suggestions=suggestions)
    loaded = json.loads(doc.read_text(encoding="utf-8"))
    assert loaded["remaining_uncertainties"] == []
    assert loaded["material_suggestions"][0]["availability"] == "partial"
    assert "如补充" in md.read_text(encoding="utf-8")
    assert "sample_target" in md.read_text(encoding="utf-8")
