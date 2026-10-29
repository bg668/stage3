"""Shared canonical facts for report, inventory and coverage tests."""
from bounded_investigator.schemas import (
    CanonicalArtifact, CanonicalClaim, Confidence, DetectionSpecificity,
    EvidenceRef, GlobalGap, GlobalReconciliationResult, ReconciliationStatus,
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


def report_canonical():
    """Report tests explicitly use a classified fact, unlike legacy analysis fixtures."""
    from bounded_investigator.schemas import EvidenceLevel
    result = canonical()
    result.canonical_claims[0].evidence_level = EvidenceLevel.CONFIRMED
    return result
