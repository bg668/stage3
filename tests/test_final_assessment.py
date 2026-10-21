from __future__ import annotations

import json

import pytest

from bounded_investigator.final_assessment import (
    FinalAssessmentProvenanceError,
    project_final_assessment_input,
    report_projection,
    render_report_markdown,
    save_report_outputs,
    validate_assessment,
)
from bounded_investigator.reconciliation import project_reconciliation_input
from bounded_investigator.schemas import (
    ArtifactFinding,
    BehaviorClaim,
    CanonicalArtifact,
    CanonicalClaim,
    Confidence,
    DetectionSpecificity,
    DeliveryStatus,
    EvidenceRef,
    FinalEvidenceAssessment,
    FinalUncertainty,
    GapItem,
    GlobalGap,
    GlobalReconciliationResult,
    InvestigationResult,
    InvestigationStatus,
    ReconciliationStatus,
    RunEnvelope,
    UsageSummary,
)


def canonical():
    ev = EvidenceRef(location="F-0483 L328-L329", observation="PK11SDR_Decrypt")
    return GlobalReconciliationResult(
        status=ReconciliationStatus.COMPLETE,
        summary="Validated facts",
        canonical_claims=[CanonicalClaim(
            canonical_id="G:C001",
            behavior="Firefox credential extraction",
            condition="signons file present",
            source="signons file",
            action="NSS decryption",
            sink="credential output",
            evidence=[ev],
            confidence=Confidence.HIGH,
            source_claim_ids=["J12:C001"],
            resolution="unchanged",
            resolution_reason="direct evidence",
        )],
        canonical_artifacts=[CanonicalArtifact(
            canonical_id="G:A001", kind="file", value="FIREFOX.abc",
            role="created_output", detection_specificity=DetectionSpecificity.HIGH,
            meaning="output file", evidence=[ev], confidence=Confidence.HIGH,
            source_artifact_ids=["J00:A001"],
        )],
        gaps=[
            GlobalGap(gap_id="R:G001", gap_type="investigation", impact="major",
                      question="How does NSS decrypt?", reason="only first half read", source_packets=["J11"]),
            GlobalGap(gap_id="R:G002", gap_type="report", impact="major",
                      question="inner PE unknown", reason="bytes not provided", source_packets=["J13"]),
        ],
    )


def test_projection_separates_canonical_facts_and_historical_gaps():
    projection = project_final_assessment_input(canonical())
    assert projection.claim_ids == frozenset({"G:C001"})
    assert projection.artifact_ids == frozenset({"G:A001"})
    assert projection.gap_ids == frozenset({"R:G001", "R:G002"})
    assert "NSS decryption" in projection.text
    assert "non-authoritative" in projection.text
    assert "R:G001 [investigation/major]" in projection.text
    assert "<canonical_claims>" in projection.text
    assert "raw_output" not in projection.text


def test_valid_uncertainty_requires_real_provenance():
    projection = project_final_assessment_input(canonical())
    result = FinalEvidenceAssessment(
        summary="Most outer behaviors confirmed",
        remaining_uncertainties=[FinalUncertainty(
            question="inner PE functionality unknown", impact="major",
            reason="inner bytes are not available", related_gap_ids=["R:G002"],
            related_claim_ids=["G:C001"],
        )],
    )
    assert validate_assessment(result, projection) is result


@pytest.mark.parametrize("refs", [
    {"related_claim_ids": ["G:C999"]},
    {"related_artifact_ids": ["G:A999"]},
    {"related_gap_ids": ["R:G999"]},
    {},
])
def test_invalid_or_missing_provenance_is_rejected(refs):
    result = FinalEvidenceAssessment(
        summary="Unknown remaining",
        remaining_uncertainties=[FinalUncertainty(
            question="unknown external payload", impact="major",
            reason="insufficient static data", **refs,
        )],
    )
    with pytest.raises(FinalAssessmentProvenanceError):
        validate_assessment(result, project_final_assessment_input(canonical()))


def test_final_report_uses_only_canonical_and_final_unknowns(tmp_path):
    facts = canonical()
    assessment = FinalEvidenceAssessment(
        summary="Only the inner PE is unavailable",
        remaining_uncertainties=[FinalUncertainty(
            question="inner PE functionality unknown", impact="major",
            reason="missing payload bytes", related_gap_ids=["R:G002"],
        )],
    )
    ready = report_projection(facts, assessment)
    assert len(ready["remaining_uncertainties"]) == 1
    assert len(ready["canonical_claims"]) == 1
    assert len(ready["canonical_artifacts"]) == 1
    assert "gaps" not in ready
    text = render_report_markdown(facts, assessment)
    assert "inner PE functionality unknown" in text
    assert "How does NSS decrypt?" not in text
    jp, mp = save_report_outputs(tmp_path, facts, assessment)
    assert json.loads(jp.read_text(encoding="utf-8"))["schema"] == "report_ready_v1"
    assert "FIREFOX.abc" in mp.read_text(encoding="utf-8")


def test_upstream_projection_preserves_worker_gap_types():
    evidence = [EvidenceRef(location="F-0483 L1", observation="read profile")]
    worker = RunEnvelope(
        packet_name="J11", elapsed_seconds=1, revision_performed=False,
        delivery_status=DeliveryStatus.SUCCESS, usage=UsageSummary(),
        result=InvestigationResult(
            status=InvestigationStatus.COMPLETE_WITH_GAPS,
            summary="Partial Firefox analysis",
            claims=[BehaviorClaim(
                behavior="Firefox", condition="if profile", source="profile",
                action="ReadFile", sink="buffer", evidence=evidence, confidence=Confidence.HIGH,
            )],
            artifacts=[ArtifactFinding(
                kind="file", value="signons.txt", role="read_source",
                detection_specificity="none", meaning="browser source",
                evidence=evidence, confidence=Confidence.HIGH,
            )],
            gaps=[
                GapItem(gap_type="investigation", question="read remaining function",
                        reason="truncated", scope="outside_packet", impact="major"),
                GapItem(gap_type="evidence", question="need resource bytes",
                        reason="not provided", scope="outside_packet", impact="major"),
            ],
        ),
    )
    global_projection = project_reconciliation_input({"J11": worker}, {})
    assert {gap.gap_type for gap in global_projection.gaps} == {"investigation", "evidence"}
    assert all(gap.gap_type != "report" for gap in global_projection.gaps)


def test_input_no_gaps_is_valid_and_empty_unknowns_are_not_forced():
    facts = canonical()
    facts.gaps = []
    projection = project_final_assessment_input(facts)
    answer = FinalEvidenceAssessment(summary="All externally available paths have been reviewed")
    assert validate_assessment(answer, projection).remaining_uncertainties == []
