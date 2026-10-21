import json
from pathlib import Path

import pytest

from bounded_investigator.dag import DagManifest, load_manifest, render_dependency_handoffs
from bounded_investigator.schemas import (
    ArtifactFinding,
    BehaviorClaim,
    CoverageReview,
    EvidenceRef,
    InvestigationResult,
    InvestigationStatus,
    RunEnvelope,
    UsageSummary,
)


def _envelope(name: str) -> RunEnvelope:
    ev = EvidenceRef(location="F-1 L1-L5", observation="writes fixed file")
    result = InvestigationResult(
        status=InvestigationStatus.COMPLETE,
        summary="upstream fact",
        claims=[BehaviorClaim(
            behavior="credential output",
            condition="mode enabled",
            source="credential data",
            action="serialize",
            sink="%TEMP%\\X.abc",
            evidence=[ev],
            confidence="high",
        )],
        artifacts=[ArtifactFinding(
            kind="file",
            value="%TEMP%\\X.abc",
            role="created_output",
            detection_specificity="high",
            meaning="fixed output",
            evidence=[ev],
            confidence="high",
        )],
    )
    return RunEnvelope(
        packet_name=name,
        elapsed_seconds=1.0,
        revision_performed=False,
        usage=UsageSummary(),
        review=CoverageReview(requires_revision=False, issues=[], review_summary="ok"),
        result=result,
    )


def test_manifest_cycle_rejected():
    with pytest.raises(ValueError):
        DagManifest.model_validate({
            "jobs": [
                {"job_id": "A", "depends_on": ["B"]},
                {"job_id": "B", "depends_on": ["A"]},
            ]
        })


def test_manifest_load_and_handoff(tmp_path: Path):
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({
        "packet_count": 2,
        "jobs": [
            {"job_id": "J00", "depends_on": []},
            {"job_id": "J01", "depends_on": ["J00"]},
        ],
    }), encoding="utf-8")
    manifest = load_manifest(path)
    assert manifest.by_id()["J01"].depends_on == ["J00"]

    text = render_dependency_handoffs(["J00"], {"J00": _envelope("J00")}, {})
    assert "[dependency J00]" in text
    assert "credential output" in text
    assert "[created_output/high]" in text
    assert '"claims"' not in text


def test_failed_dependency_is_visible_not_fatal():
    text = render_dependency_handoffs(["J00"], {}, {"J00": "validation failed"})
    assert "status: unavailable" in text
    assert "validation failed" in text

from bounded_investigator.schemas import (
    BehaviorClaim, Confidence, DeliveryStatus, EvidenceRef, PartialInvestigationResult,
    RunEnvelope, UsageSummary,
)


def test_partial_dependency_only_passes_validated_facts():
    partial = PartialInvestigationResult(
        summary="partial",
        claims=[BehaviorClaim(
            behavior="delete file",
            condition="none observed",
            source="path",
            action="DeleteFileA",
            sink="file removed",
            evidence=[EvidenceRef(location="F-1 L1", observation="DeleteFileA(path)")],
            confidence=Confidence.HIGH,
        )],
        artifacts=[],
    )
    env = RunEnvelope(
        packet_name="JX",
        elapsed_seconds=1.0,
        revision_performed=False,
        delivery_status=DeliveryStatus.PARTIAL_VALIDATED,
        usage=UsageSummary(),
        review_completed=False,
        partial_result=partial,
    )
    text = render_dependency_handoffs(["JX"], {"JX": env}, {})
    assert "status: partial_validated" in text
    assert "delete file" in text
    assert "only independently Pydantic-validated" in text


def test_raw_only_dependency_is_unavailable():
    env = RunEnvelope(
        packet_name="JX",
        elapsed_seconds=1.0,
        revision_performed=False,
        delivery_status=DeliveryStatus.RAW_ONLY,
        usage=UsageSummary(),
        review_completed=False,
    )
    text = render_dependency_handoffs(["JX"], {"JX": env}, {})
    assert "status: unavailable" in text
    assert "raw_only" in text
