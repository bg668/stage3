import pytest
from pydantic import ValidationError

from bounded_investigator.schemas import (
    ArtifactFinding,
    CoverageReview,
    EvidenceRef,
    InvestigationResult,
    InvestigationStatus,
    ReviewIssue,
)


def test_complete_cannot_have_handoff():
    with pytest.raises(ValidationError):
        InvestigationResult(
            status=InvestigationStatus.COMPLETE,
            summary="x",
            handoffs=[
                {
                    "target": "F-1",
                    "reason": "missing",
                    "expected_value": "clarify",
                    "priority": "high",
                }
            ],
        )


def test_artifact_role_is_required():
    artifact = ArtifactFinding(
        kind="file",
        value="%TEMP%\\x.abc",
        role="created_output",
        detection_specificity="high",
        meaning="credential output",
        evidence=[EvidenceRef(location="F-1", observation="WriteFile target")],
        confidence="high",
    )
    assert artifact.role == "created_output"
    assert artifact.detection_specificity.value == "high"


def test_review_requires_revision_for_major_internal_issue():
    issue = ReviewIssue(
        issue_id="R1",
        scope="packet_internal",
        severity="major",
        kind="omitted_claim",
        description="missed behavior",
        evidence=[EvidenceRef(location="F-1", observation="branch exists")],
        required_fix="add it",
    )
    review = CoverageReview(
        requires_revision=True,
        issues=[issue],
        review_summary="needs fix",
    )
    assert review.requires_revision is True


def test_minor_issue_cannot_force_revision():
    issue = ReviewIssue(
        issue_id="R1",
        scope="packet_internal",
        severity="minor",
        kind="other",
        description="cleanup detail",
        required_fix="optional wording",
    )
    with pytest.raises(ValidationError):
        CoverageReview(
            requires_revision=True,
            issues=[issue],
            review_summary="minor only",
        )
