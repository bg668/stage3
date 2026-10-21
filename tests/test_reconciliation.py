from bounded_investigator.reconciliation import (
    apply_gap_reconciliation,
    assemble_reconciliation_result,
    project_gap_reconciliation_input,
    project_reconciliation_input,
    validate_and_finalize_reconciliation,
)
from bounded_investigator.schemas import (
    ArtifactConflictDecision,
    ArtifactFinding,
    BehaviorClaim,
    ClaimRelationDecision,
    ClaimUnresolvedDecision,
    Confidence,
    DeliveryStatus,
    EvidenceRef,
    GapReconciliationDecisionResult,
    GlobalGap,
    GlobalReconciliationDecisionResult,
    InvestigationResult,
    InvestigationStatus,
    ReconciliationStatus,
    RunEnvelope,
    UsageSummary,
)


def _env(
    packet: str,
    behavior: str,
    action: str,
    artifact_value: str,
    *,
    condition: str = "none observed",
    source: str = "input",
    sink: str = "effect",
    role: str = "created_output",
    specificity: str = "high",
    confidence: Confidence = Confidence.HIGH,
) -> RunEnvelope:
    ev = EvidenceRef(location=f"{packet}-F1 L1-L3", observation=action)
    result = InvestigationResult(
        status=InvestigationStatus.COMPLETE,
        summary=f"{packet} summary",
        claims=[BehaviorClaim(
            behavior=behavior,
            condition=condition,
            source=source,
            action=action,
            sink=sink,
            evidence=[ev],
            confidence=confidence,
        )],
        artifacts=[ArtifactFinding(
            kind="file",
            value=artifact_value,
            role=role,
            detection_specificity=specificity,
            meaning="fixed output",
            evidence=[ev],
            confidence=confidence,
        )],
    )
    return RunEnvelope(
        packet_name=packet,
        elapsed_seconds=1.0,
        revision_performed=False,
        delivery_status=DeliveryStatus.SUCCESS,
        usage=UsageSummary(),
        result=result,
    )


def _keep_all(projection):
    return GlobalReconciliationDecisionResult(
        keep_claim_ids=list(projection.reduced_claims)
    )


def test_projection_assigns_stable_source_and_reduced_ids_and_marks_unavailable():
    projection = project_reconciliation_input(
        {"J01": _env("J01", "delete", "DeleteFileA", "x.tmp")},
        {"J02": "failed"},
    )
    assert "J01:C001" in projection.claims
    assert "J01:A001" in projection.artifacts
    assert "R:C001" in projection.reduced_claims
    assert "R:A001" in projection.reduced_artifacts
    assert projection.unavailable_packets == ["J02"]
    assert "<unavailable_packets>\nJ02" in projection.text


def test_exact_claim_and_artifact_duplicates_are_reduced_before_llm():
    projection = project_reconciliation_input(
        {
            "J01": _env("J01", "delete", "DeleteFileA", "x.tmp"),
            "J02": _env("J02", "delete", "DeleteFileA", "x.tmp"),
        },
        {},
    )
    assert len(projection.claims) == 2
    assert len(projection.reduced_claims) == 1
    assert len(projection.artifacts) == 2
    assert len(projection.reduced_artifacts) == 1
    claim_group = next(iter(projection.reduced_claims.values()))
    assert claim_group.source_claim_ids == ("J01:C001", "J02:C001")


def test_decision_only_supersede_assembles_stronger_claim_without_rewriting_facts():
    projection = project_reconciliation_input(
        {
            "J13": _env(
                "J13", "process injection", "suspected APC execution", "x.bin",
                confidence=Confidence.LOW,
            ),
            "J14": _env(
                "J14", "process injection", "CreateRemoteThread", "y.bin",
                confidence=Confidence.HIGH,
            ),
        },
        {},
    )
    ids = list(projection.reduced_claims)
    apc_id = next(i for i in ids if "APC" in projection.reduced_claims[i].claim.action)
    crt_id = next(i for i in ids if "CreateRemoteThread" in projection.reduced_claims[i].claim.action)
    decisions = GlobalReconciliationDecisionResult(
        keep_claim_ids=[crt_id],
        superseded_by=[ClaimRelationDecision(
            claim_id=apc_id,
            target_claim_id=crt_id,
            reason="direct CreateRemoteThread evidence supersedes inference",
        )],
    )
    final = validate_and_finalize_reconciliation(
        assemble_reconciliation_result(decisions, projection), projection
    )
    assert final.status == ReconciliationStatus.COMPLETE
    assert len(final.canonical_claims) == 1
    claim = final.canonical_claims[0]
    assert claim.action == "CreateRemoteThread"
    assert claim.source_claim_ids == ["J14:C001"]
    assert claim.superseded_claim_ids == ["J13:C001"]
    assert claim.resolution == "superseded_weaker_claim"


def test_resolver_omission_becomes_unresolved_not_silent_loss():
    projection = project_reconciliation_input(
        {
            "J01": _env("J01", "delete", "DeleteFileA", "x.tmp"),
            "J02": _env("J02", "inject", "CreateRemoteThread", "y.bin"),
        },
        {},
    )
    first = next(iter(projection.reduced_claims))
    decisions = GlobalReconciliationDecisionResult(keep_claim_ids=[first])
    final = validate_and_finalize_reconciliation(
        assemble_reconciliation_result(decisions, projection), projection
    )
    assert final.status == ReconciliationStatus.COMPLETE_WITH_CONFLICTS
    assert len(final.canonical_claims) == 1
    assert len(final.unresolved_conflicts) == 1
    assert final.unaccounted_input_claim_ids == []


def test_artifacts_are_program_assembled_and_exact_duplicates_merge():
    projection = project_reconciliation_input(
        {
            "J01": _env("J01", "a", "op1", "%TEMP%\\x.abc"),
            "J02": _env("J02", "b", "op2", "%TEMP%\\x.abc"),
        },
        {},
    )
    decisions = _keep_all(projection)
    final = validate_and_finalize_reconciliation(
        assemble_reconciliation_result(decisions, projection), projection
    )
    assert len(final.canonical_artifacts) == 1
    art = final.canonical_artifacts[0]
    assert art.value == "%TEMP%\\x.abc"
    assert art.source_artifact_ids == ["J01:A001", "J02:A001"]


def test_artifact_metadata_conflict_uses_selected_real_source():
    projection = project_reconciliation_input(
        {
            "J01": _env(
                "J01", "a", "op1", "same.dat", role="created_output", specificity="high"
            ),
            "J02": _env(
                "J02", "b", "op2", "same.dat", role="read_source", specificity="none"
            ),
        },
        {},
    )
    group_id, group = next(iter(projection.reduced_artifacts.items()))
    assert group.metadata_conflict is True
    decisions = _keep_all(projection)
    decisions.artifact_conflict_decisions = [ArtifactConflictDecision(
        artifact_group_id=group_id,
        selected_source_artifact_id="J01:A001",
        reason="creation evidence is direct",
    )]
    final = validate_and_finalize_reconciliation(
        assemble_reconciliation_result(decisions, projection), projection
    )
    art = final.canonical_artifacts[0]
    assert art.role == "created_output"
    assert art.detection_specificity.value == "high"
    assert art.value == "same.dat"


def test_missing_artifact_conflict_decision_degrades_conservatively():
    projection = project_reconciliation_input(
        {
            "J01": _env(
                "J01", "a", "op1", "same.dat", role="created_output", specificity="high"
            ),
            "J02": _env(
                "J02", "b", "op2", "same.dat", role="read_source", specificity="none"
            ),
        },
        {},
    )
    final = validate_and_finalize_reconciliation(
        assemble_reconciliation_result(_keep_all(projection), projection), projection
    )
    art = final.canonical_artifacts[0]
    assert art.role == "other"
    assert art.detection_specificity.value == "none"
    assert any("Resolve metadata" in gap.question for gap in final.gaps)


def test_gap_reconciliation_removes_resolved_and_keeps_open():
    projection = project_reconciliation_input(
        {"J01": _env("J01", "inject", "CreateRemoteThread", "x.bin")}, {}
    )
    final = validate_and_finalize_reconciliation(
        assemble_reconciliation_result(_keep_all(projection), projection), projection
    )
    final.gaps = [
        GlobalGap(gap_id="R:G001", question="Is injection CreateRemoteThread?", reason="upstream unknown", impact="major"),
        GlobalGap(gap_id="R:G002", question="What is inner PE C2?", reason="bytes unavailable", impact="major"),
    ]
    gap_projection = project_gap_reconciliation_input(final)
    decisions = GapReconciliationDecisionResult(
        resolved_gap_ids=["R:G001"],
        still_open_gap_ids=["R:G002"],
    )
    updated, resolved, open_ids = apply_gap_reconciliation(final, decisions, gap_projection)
    assert resolved == ["R:G001"]
    assert open_ids == ["R:G002"]
    assert [g.gap_id for g in updated.gaps] == ["R:G002"]


def test_gap_reconciliation_omitted_or_conflicting_ids_stay_open():
    projection = project_reconciliation_input(
        {"J01": _env("J01", "inject", "CreateRemoteThread", "x.bin")}, {}
    )
    final = validate_and_finalize_reconciliation(
        assemble_reconciliation_result(_keep_all(projection), projection), projection
    )
    final.gaps = [
        GlobalGap(gap_id="R:G001", question="q1", reason="r1", impact="major"),
        GlobalGap(gap_id="R:G002", question="q2", reason="r2", impact="minor"),
    ]
    gap_projection = project_gap_reconciliation_input(final)
    decisions = GapReconciliationDecisionResult(
        resolved_gap_ids=["R:G001"],
        still_open_gap_ids=["R:G001"],
    )
    updated, resolved, open_ids = apply_gap_reconciliation(final, decisions, gap_projection)
    assert resolved == []
    assert open_ids == ["R:G001", "R:G002"]
    assert len(updated.gaps) == 2
