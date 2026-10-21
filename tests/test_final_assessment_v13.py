from __future__ import annotations

import pytest

from bounded_investigator.evidence_scope import EvidenceScope, MaterialScopeItem, PacketCoverage
from bounded_investigator.final_assessment import (
    FinalAssessmentProvenanceError, ensure_material_scope_coverage,
    build_material_suggestions, separate_operational_uncertainties,
    project_final_assessment_input, report_projection, render_report_markdown,
    validate_assessment,
)
from bounded_investigator.schemas import FinalEvidenceAssessment, FinalUncertainty
from test_final_assessment import canonical


def attested_scope():
    return EvidenceScope(materials=[
        MaterialScopeItem(
            scope_id="resource_body", label="RCDATA content", status="unavailable",
            source="operator-checked resource export", details="No resource bytes available",
            report_question="Actual decoded configuration values cannot be recovered?",
        ),
        MaterialScopeItem(
            scope_id="inner_pe", label="Inner PE bytes", status="unavailable",
            source="operator-checked PE data export", details="Inner PE not present",
            report_question="Inner payload behavior cannot be determined?",
        ),
        MaterialScopeItem(scope_id="runtime_logs", label="Runtime log events", status="unknown"),
    ])


def test_no_scope_no_verified_material_absence():
    projection = project_final_assessment_input(canonical())
    assert "material requests" in projection.text
    assert "inner_pe" not in projection.scope_items
    with pytest.raises(FinalAssessmentProvenanceError):
        validate_assessment(FinalEvidenceAssessment(
            summary="missing data",
            remaining_uncertainties=[FinalUncertainty(
                question="Inner PE missing", impact="major", reason="missing",
                uncertainty_type="material_unavailable", related_gap_ids=["R:G002"],
            )]), projection)


def test_scope_type_and_packet_delivery_are_different():
    projection = project_final_assessment_input(
        canonical(), evidence_scope=attested_scope(),
        packet_coverage=[PacketCoverage(packet_id="J02", delivery_status="raw_only")],
    )
    assert "raw_only" in projection.text
    assert "source code" in projection.text.lower() or "source bytes absent" in projection.text
    assert "resource_body" not in projection.text
    assert "resource_body" in projection.scope_items
    valid = FinalEvidenceAssessment(summary="verified boundary", remaining_uncertainties=[
        FinalUncertainty(
            question="config unknown", impact="major", reason="resource not available",
            uncertainty_type="material_unavailable", related_scope_ids=["resource_body"],
        ),
        FinalUncertainty(
            question="J02 results not validated", impact="minor", reason="output format",
            uncertainty_type="analysis_incomplete", related_packet_ids=["J02"],
        ),
    ])
    clean, redirected = separate_operational_uncertainties(valid)
    assert clean.remaining_uncertainties == []
    assert len(redirected) == 2
    assert validate_assessment(clean, projection) is clean
    with pytest.raises(FinalAssessmentProvenanceError):
        validate_assessment(valid, projection)
    with pytest.raises(FinalAssessmentProvenanceError):
        validate_assessment(FinalEvidenceAssessment(summary="incorrect", remaining_uncertainties=[
            FinalUncertainty(
                question="runtime logs unavailable", impact="major", reason="unknown only",
                uncertainty_type="material_unavailable", related_scope_ids=["runtime_logs"],
            )
        ]), projection)
    with pytest.raises(FinalAssessmentProvenanceError):
        validate_assessment(FinalEvidenceAssessment(summary="incorrect", remaining_uncertainties=[
            FinalUncertainty(
                question="J02 code missing", impact="major", reason="system result failed",
                uncertainty_type="material_unavailable", related_packet_ids=["J02"],
            )
        ]), projection)


def test_material_scope_becomes_conditional_hints_not_behavioral_unknowns():
    projection = project_final_assessment_input(canonical(), evidence_scope=attested_scope())
    model_reply = FinalEvidenceAssessment(summary="known facts", remaining_uncertainties=[])
    assessed, added = ensure_material_scope_coverage(model_reply, projection)
    assert added == []  # Never fabricate a missing-material uncertainty.
    assert assessed.remaining_uncertainties == []
    suggestions = build_material_suggestions(attested_scope())
    assert {x.scope_id for x in suggestions} == {"resource_body", "inner_pe"}
    assert all("如后续能提供" in x.recommendation for x in suggestions)
    assert all("样本中没有" not in x.recommendation for x in suggestions)
    assert "runtime_logs" not in {x.scope_id for x in suggestions}


def test_report_keeps_delivery_warning_separate_from_malware_unknown():
    projection = project_final_assessment_input(
        canonical(), evidence_scope=attested_scope(),
        packet_coverage=[PacketCoverage(packet_id="J02", delivery_status="raw_only")],
    )
    assessment, _ = ensure_material_scope_coverage(
        FinalEvidenceAssessment(summary="bounded facts"), projection
    )
    warning = [{"packet_id": "J02", "delivery_status": "raw_only"}]
    ready = report_projection(
        canonical(), assessment,
        material_scope=[m.model_dump() for m in attested_scope().materials],
        input_coverage_warnings=warning,
    )
    assert "gaps" not in ready
    assert ready["input_coverage_warnings"][0]["packet_id"] == "J02"
    assert len(ready["remaining_uncertainties"]) == 0
    suggestions = build_material_suggestions(attested_scope())
    ready = report_projection(
        canonical(), assessment,
        input_coverage_warnings=warning,
        material_suggestions=suggestions,
    )
    assert len(ready["material_suggestions"]) == 2
    md = render_report_markdown(
        canonical(), assessment, input_coverage_warnings=warning,
        material_suggestions=suggestions,
    )
    assert "后续可补充的分析材料" in md
    assert "调查结果交付覆盖警告" in md
    assert "raw_only" in md
    assert "How does NSS decrypt" not in md


def test_history_projection_reduces_low_priority_process_noise():
    facts = canonical()
    from bounded_investigator.schemas import GlobalGap
    facts.gaps.extend(GlobalGap(
        gap_id=f"R:G{i:03d}", gap_type="investigation", question=f"read helper {i}",
        reason="todo", source_packets=["J01"], impact="minor"
    ) for i in range(3,100))
    projection = project_final_assessment_input(facts)
    assert projection.source_gap_count == 99
    assert projection.included_historical_gap_count == 36
    assert "Selected 36 of 99" in projection.text
