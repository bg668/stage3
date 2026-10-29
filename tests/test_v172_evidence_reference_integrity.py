import json
from pathlib import Path

from bounded_investigator.evidence.validation import (
    validate_canonical_references,
    validate_evidence_refs,
    validate_investigation_result,
)
from bounded_investigator.reporting.knowledge_report import project_knowledge, save_knowledge_outputs
from bounded_investigator.schemas import (
    BehaviorClaim,
    CanonicalClaim,
    Confidence,
    EvidenceLevel,
    EvidenceRef,
    GlobalReconciliationResult,
    InvestigationResult,
    InvestigationStatus,
    ReconciliationStatus,
)
from bounded_investigator.evidence.snippets import (
    build_code_index_from_text,
    parse_evidence_location,
)


def packet_text() -> str:
    return """## F-0500 sub_main
```c
L0047: a = CreateMutexA(0, 0, \"_x_X_UPDATE_X_x_\");
L0048: e = GetLastError();
L0049: if (e == 183) return 1;
L0050: return 0;
```

## F-0038 sub_aux
```c
L0009: return CryptUnprotectData(...);
L0010: return 1;
```
"""


def test_claim_schema_is_not_polluted_by_validation_state():
    assert set(EvidenceRef.model_fields) == {"location", "observation"}
    assert "evidence_validation" not in BehaviorClaim.model_fields
    assert "validation" not in BehaviorClaim.model_fields


def test_location_parser_handles_multiple_ranges():
    assert parse_evidence_location("F-0500 L47-L49; F-0038 L9-L10") == [
        ("F-0500", 47, 49),
        ("F-0038", 9, 10),
    ]


def test_current_packet_validation_defers_cross_packet_reference_without_calling_it_invalid():
    index = build_code_index_from_text(packet_text(), "J00.md")
    refs = [
        EvidenceRef(location="F-0500 L47-L49", observation="mutex branch"),
        EvidenceRef(location="dependency J01 F-0999 L1-L2", observation="upstream evidence"),
    ]
    checks = validate_evidence_refs(
        refs, owner_kind="claim", owner_id="C001", index=index,
        missing_status="not_in_current_packet",
    )
    assert [x.status for x in checks] == ["valid", "not_in_current_packet"]


def test_present_function_with_partial_range_is_deferred_in_current_packet_but_rejected_globally():
    index = build_code_index_from_text(packet_text(), "J00.md")
    current = validate_evidence_refs(
        [EvidenceRef(location="F-0500 L47-L69", observation="too wide")],
        owner_kind="claim", owner_id="C001", index=index,
        missing_status="not_in_current_packet",
    )
    assert current[0].status == "not_in_current_packet"
    full = validate_evidence_refs(
        [EvidenceRef(location="F-0500 L47-L69", observation="too wide")],
        owner_kind="claim", owner_id="C001", index=index,
        missing_status="invalid_function",
    )
    assert full[0].status == "invalid_range"


def test_investigation_result_validation_is_sidecar_only():
    index = build_code_index_from_text(packet_text(), "J00.md")
    result = InvestigationResult(
        status=InvestigationStatus.COMPLETE,
        summary="x",
        claims=[BehaviorClaim(
            behavior="mutex dispatch", condition="none observed", source="literal",
            action="CreateMutexA", sink="branch", confidence=Confidence.HIGH,
            evidence_level=EvidenceLevel.CONFIRMED,
            evidence=[EvidenceRef(location="F-0500 L47-L49", observation="visible")],
        )],
    )
    before = result.model_dump(mode="json")
    audit = validate_investigation_result(result, index=index)
    after = result.model_dump(mode="json")
    assert audit["passed"] is True
    assert audit["valid_segments"] == 1
    assert before == after
    assert "evidence_validation" not in after["claims"][0]


def canonical(location: str, *, level=EvidenceLevel.CONFIRMED) -> GlobalReconciliationResult:
    return GlobalReconciliationResult(
        status=ReconciliationStatus.COMPLETE,
        summary="x",
        canonical_claims=[CanonicalClaim(
            canonical_id="G:C001", behavior="mutex dispatch", condition="none observed",
            source="literal", action="CreateMutexA", sink="branch",
            evidence=[EvidenceRef(location=location, observation="visible")],
            confidence=Confidence.HIGH, evidence_level=level,
            source_claim_ids=["J00:C001"], resolution="unchanged", resolution_reason="x",
        )],
    )


def test_full_material_audit_rejects_invalid_confirmed_reference():
    index = build_code_index_from_text(packet_text(), "J00.md")
    audit = validate_canonical_references(canonical("F-0500 L47-L69"), index=index)
    assert audit["passed"] is False
    assert audit["invalid_segments"] == 1
    assert audit["confirmed_claims_without_valid_evidence"] == ["G:C001"]


def test_knowledge_audit_embeds_evidence_reference_audit_without_schema_changes(tmp_path):
    packets = tmp_path / "packets"
    packets.mkdir()
    (packets / "J00.md").write_text(packet_text(), encoding="utf-8")
    report, audit = project_knowledge(canonical("F-0500 L47-L49"), packet_source_dir=packets)
    assert audit["passed"] is True
    evidence_audit = audit["evidence_reference_audit"]
    assert evidence_audit["scope"] == "full_material"
    assert evidence_audit["valid_segments"] == 1
    assert evidence_audit["invalid_segments"] == 0
    assert report["evidence_reference_audit_file"] == "_evidence_reference_audit.json"

    out = tmp_path / "out"
    save_knowledge_outputs(out, canonical("F-0500 L47-L49"), packet_source_dir=packets)
    saved = json.loads((out / "_evidence_reference_audit.json").read_text(encoding="utf-8"))
    assert saved["passed"] is True
    assert saved["valid_segments"] == 1
