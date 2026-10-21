import asyncio
import importlib.util
import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

from bounded_investigator.prompts import render_result_context, reviser_prompt
from bounded_investigator.schemas import (
    CoverageReview, DetectionSpecificity, EvidenceLevel, InvestigationResult, InvestigationStatus,
)
from bounded_investigator.source_snippets import build_code_index_from_text


MATERIAL = """## F-0001 sub_main
```c
L0001: x = ReadFile();
L0002: WriteFile(x);
L0003: return 0;
```
"""


@pytest.fixture
def draft():
    claim = dict(
        behavior="collect", condition="enabled", source="input", action="read",
        sink="buffer", confidence="high", evidence_level="confirmed", limitations=[],
        evidence=[dict(location="F-0001 L1", observation="read input")],
    )
    artifact = dict(
        kind="string", value="DAEMON", role="parser_marker", detection_specificity="low",
        confidence="high", meaning="visible marker",
        evidence=[dict(location="F-0001 L1", observation="marker context")],
    )
    return InvestigationResult.model_validate(dict(
        status="complete_with_gaps", summary="original {{summary}}",
        claims=[claim, dict(deepcopy(claim), behavior="second behavior")],
        artifacts=[artifact, dict(deepcopy(artifact), value="second marker")],
        gaps=[dict(gap_type="evidence", question="missing helper?", reason="not supplied",
                   scope="outside_packet", impact="major")],
        handoffs=[], material_needs=[],
    ))


def review_for(*targets, severity="major", scope="packet_internal"):
    return CoverageReview.model_validate(dict(
        requires_revision=severity in {"major", "critical"} and scope == "packet_internal",
        review_summary="fix only typed objects",
        issues=[dict(
            issue_id="R1", scope=scope, severity=severity, kind="wrong_claim",
            description="wrong field", required_fix="repair the selected field",
            evidence=[dict(location="F-0001 L1", observation="local source")],
            fixes=[dict(type=kind, targets=[path]) for kind, path in targets],
        )],
    ))


def apply_revision(draft, candidate, review, **kwargs):
    assert importlib.util.find_spec("bounded_investigator.revision") is not None, "Scoped merge is missing"
    from bounded_investigator.revision import merge_scoped_revision

    return merge_scoped_revision(draft, candidate, review, **kwargs)


def test_review_schema_preserves_typed_modification_scope():
    review = review_for(("claim_revision", "/claims/0"))
    issue = review.model_dump(mode="json")["issues"][0]
    assert issue["fixes"] == [dict(type="claim_revision", targets=["/claims/0"])]


def test_legacy_review_remains_readable_without_granting_implicit_permission(draft):
    review = review_for()
    assert review.requires_revision
    with pytest.raises(ValueError, match="R1.*fixes"):
        apply_revision(draft, draft, review)


def test_only_type_authorized_fields_are_accepted_and_all_other_values_are_copied(draft):
    candidate = draft.model_copy(deep=True)
    candidate.claims[0].source = "correct source"
    candidate.claims[0].behavior = "unrequested rewrite"
    candidate.claims[1].limitations = ["unrequested limitation"]
    candidate.artifacts[0].detection_specificity = DetectionSpecificity.MEDIUM
    candidate.summary = "unrequested summary"
    original = draft.model_copy(deep=True)
    candidate_before = candidate.model_copy(deep=True)
    outcome = apply_revision(draft, candidate, review_for(("artifact_classification", "/artifacts/0")))
    expected = draft.model_dump(mode="json")
    expected["artifacts"][0]["detection_specificity"] = "medium"
    assert outcome.result.model_dump(mode="json") == expected
    assert outcome.audit["passed"] is True
    assert "/claims/0/source" in outcome.audit["ignored_paths"]
    assert draft == original and candidate == candidate_before
    outcome.result.claims[1].limitations.append("later mutation")
    assert draft == original


def test_location_only_repair_cannot_rewrite_observations_or_levels(draft):
    candidate = draft.model_copy(deep=True)
    candidate.claims[0].evidence[0].location = "F-0001 L2"
    candidate.claims[0].evidence[0].observation = "invented observation"
    candidate.claims[0].evidence_level = EvidenceLevel.SUSPECTED
    outcome = apply_revision(draft, candidate, review_for(("reference_format", "/claims/0/evidence/0")))
    claim = outcome.result.claims[0]
    assert claim.evidence[0].location == "F-0001 L2"
    assert claim.evidence[0].observation == draft.claims[0].evidence[0].observation
    assert claim.evidence_level == draft.claims[0].evidence_level


def test_wildcard_is_expanded_against_original_items_only(draft):
    candidate = draft.model_copy(deep=True)
    for c in candidate.claims:
        c.evidence[0].location = "F-0001 L2"
        c.evidence[0].observation = "unrequested rewrite"
    outcome = apply_revision(draft, candidate, review_for(("reference_format", "/claims/*")))
    assert all(c.evidence[0].location == "F-0001 L2" for c in outcome.result.claims)
    assert [c.evidence[0].observation for c in outcome.result.claims] == [c.evidence[0].observation for c in draft.claims]


@pytest.mark.parametrize("path", [
    "/", "/claims", "/claims/99/source", "/claims/0/unknown", "/claims/01/source",
    "/claims/*", "/claims/0/*", "/unknown/0", "/summary/0", "/claims/0/evidence/-1/location",
])
def test_invalid_or_overbroad_paths_do_not_grant_permission(draft, path):
    with pytest.raises(ValueError):
        apply_revision(draft, draft, review_for(("claim_revision", path)))


def test_outside_packet_issue_cannot_expand_internal_revision_scope(draft):
    review = review_for(("reference_format", "/claims/0"))
    outside = review_for(("claim_revision", "/claims/0"), scope="outside_packet").issues[0]
    review.issues.append(outside)
    candidate = draft.model_copy(deep=True)
    candidate.claims[0].evidence[0].location = "F-0001 L2"
    candidate.claims[0].source = "outside change"
    candidate.summary = "outside change"
    outcome = apply_revision(draft, candidate, review)
    assert outcome.result.summary == draft.summary
    assert outcome.result.claims[0].source == draft.claims[0].source
    assert outcome.result.claims[0].evidence[0].location == "F-0001 L2"


@pytest.mark.parametrize("section", ["claims", "artifacts", "gaps"])
def test_unauthorized_deletion_is_rejected_without_guessing_item_alignment(draft, section):
    candidate = draft.model_copy(deep=True)
    getattr(candidate, section).pop(0)
    with pytest.raises(ValueError, match="length"):
        apply_revision(draft, candidate, review_for(("claim_revision", "/claims/0")))


def test_reordering_original_items_is_rejected(draft):
    candidate = draft.model_copy(deep=True)
    candidate.claims.reverse()
    with pytest.raises(ValueError, match="order"):
        apply_revision(draft, candidate, review_for(("claim_revision", "/claims/0")))


def test_explicit_append_keeps_existing_items_unchanged(draft):
    candidate = draft.model_copy(deep=True)
    new_claim = draft.claims[0].model_copy(deep=True)
    new_claim.behavior = "new branch"
    candidate.claims.append(new_claim)
    candidate.claims[0].behavior = "unrequested rewrite"
    outcome = apply_revision(draft, candidate, review_for(("append_item", "/claims/2")))
    assert outcome.result.claims[:2] == draft.claims
    assert outcome.result.claims[2] == new_claim


def test_delete_and_replace_use_original_indices_even_after_compaction(draft):
    candidate = draft.model_copy(deep=True)
    candidate.claims.pop(0)
    candidate.claims[0].source = "correct second source"
    outcome = apply_revision(draft, candidate, review_for(
        ("delete_item", "/claims/0"), ("claim_revision", "/claims/1"),
    ))
    assert len(outcome.result.claims) == 1
    assert outcome.result.claims[0].behavior == "second behavior"
    assert outcome.result.claims[0].source == "correct second source"
    assert len(draft.claims) == 2


def test_delete_and_append_can_coexist_without_reusing_original_indices(draft):
    candidate = draft.model_copy(deep=True)
    candidate.artifacts.pop(0)
    candidate.artifacts.append(draft.artifacts[0].model_copy(update={"value": "new marker"}))
    outcome = apply_revision(draft, candidate, review_for(
        ("delete_item", "/artifacts/0"), ("append_item", "/artifacts/2"),
    ))
    assert [a.value for a in outcome.result.artifacts] == ["second marker", "new marker"]


@pytest.mark.parametrize("targets", [
    [("append_item", "/claims/0")], [("append_item", "/claims/3")],
    [("delete_item", "/claims/0/source")], [("delete_item", "/claims/*")],
    [("delete_item", "/claims/0"), ("claim_revision", "/claims/0")],
])
def test_invalid_append_delete_or_conflicting_targets_are_rejected(draft, targets):
    with pytest.raises(ValueError):
        apply_revision(draft, draft, review_for(*targets))


def test_merged_status_is_validated_with_retained_gaps(draft):
    candidate = draft.model_copy(deep=True)
    candidate.status = InvestigationStatus.COMPLETE
    candidate.artifacts.pop(0)
    with pytest.raises(ValueError, match="complete"):
        apply_revision(draft, candidate, review_for(("delete_item", "/artifacts/0")))


def test_explicitly_deleting_last_gap_allows_complete_status(draft):
    candidate = draft.model_copy(deep=True)
    candidate.status = InvestigationStatus.COMPLETE
    candidate.gaps.clear()
    outcome = apply_revision(draft, candidate, review_for(("delete_item", "/gaps/0")))
    assert outcome.result.status.value == "complete"
    assert not outcome.result.gaps


def test_downgrade_includes_linked_limitations_but_still_rejects_missing_limits(draft):
    candidate = draft.model_copy(deep=True)
    candidate.claims[0].evidence_level = EvidenceLevel.SUSPECTED
    with pytest.raises(ValueError, match="limitations"):
        apply_revision(draft, candidate, review_for(("claim_revision", "/claims/0")))
    candidate.claims[0].limitations = ["helper missing"]
    outcome = apply_revision(draft, candidate, review_for(("claim_revision", "/claims/0")))
    assert outcome.result.claims[0].limitations == ["helper missing"]


def test_new_unclassified_claim_is_rejected_but_legacy_level_is_preserved(draft):
    draft.claims[0].evidence_level = EvidenceLevel.UNCLASSIFIED
    candidate = draft.model_copy(deep=True)
    candidate.claims[0].evidence[0].location = "F-0001 L2"
    outcome = apply_revision(draft, candidate, review_for(("reference_format", "/claims/0/evidence/0")))
    assert outcome.result.claims[0].evidence_level.value == "unclassified"
    candidate.claims.append(draft.claims[0].model_copy(deep=True))
    with pytest.raises(ValueError, match="unclassified"):
        apply_revision(draft, candidate, review_for(("append_item", "/claims/2")))


def test_existing_classified_claim_cannot_become_unclassified(draft):
    candidate = draft.model_copy(deep=True)
    candidate.claims[0].evidence_level = EvidenceLevel.UNCLASSIFIED
    with pytest.raises(ValueError, match="unclassified"):
        apply_revision(draft, candidate, review_for(("claim_revision", "/claims/0")))


def test_new_invalid_evidence_is_rejected_and_preexisting_errors_are_not_silently_changed(draft):
    index = build_code_index_from_text(MATERIAL, "J00.md")
    candidate = draft.model_copy(deep=True)
    candidate.claims[0].evidence[0].location = "unparseable new reference"
    with pytest.raises(ValueError, match="evidence"):
        apply_revision(draft, candidate, review_for(("reference_format", "/claims/0/evidence/0")), index=index)
    draft.claims[1].evidence[0].location = "legacy unparseable reference"
    candidate = draft.model_copy(deep=True)
    candidate.claims[0].source = "correct source"
    outcome = apply_revision(draft, candidate, review_for(("claim_revision", "/claims/0")), index=index)
    assert outcome.result.claims[1].evidence[0].location == "legacy unparseable reference"


def test_appended_artifact_cannot_group_multiple_objects(draft):
    candidate = draft.model_copy(deep=True)
    artifact = draft.artifacts[0].model_copy(deep=True)
    artifact.kind = "file"
    artifact.value = "first.dat;second.dat"
    candidate.artifacts.append(artifact)
    with pytest.raises(ValueError, match="artifact"):
        apply_revision(draft, candidate, review_for(("append_item", "/artifacts/2")))


def test_authorized_correction_may_make_two_items_equal_without_being_a_reorder(draft):
    candidate = draft.model_copy(deep=True)
    candidate.claims[0].behavior = draft.claims[1].behavior
    outcome = apply_revision(draft, candidate, review_for(("claim_revision", "/claims/0")))
    assert outcome.result.claims[0] == outcome.result.claims[1]


def test_appending_a_copy_of_preexisting_invalid_evidence_is_still_a_new_error(draft):
    index = build_code_index_from_text(MATERIAL, "J00.md")
    draft.claims[0].evidence[0].location = "legacy malformed reference"
    candidate = draft.model_copy(deep=True)
    candidate.claims.append(draft.claims[0].model_copy(update={"behavior": "new branch"}))
    with pytest.raises(ValueError, match="evidence"):
        apply_revision(draft, candidate, review_for(("append_item", "/claims/2")), index=index)


def test_handoff_exposes_original_paths_and_allowed_operations(draft):
    text = render_result_context(draft)
    for path in ("/claims/0", "/claims/1", "/artifacts/0", "/gaps/0"):
        assert path in text
    review = review_for(("claim_revision", "/claims/0"), ("append_item", "/claims/2"))
    prompt = reviser_prompt("J00", "material {{raw}}", draft, review)
    assert "replace /claims/0/source" in prompt
    assert "append /claims/2" in prompt
    assert "material {{raw}}" in prompt


@pytest.mark.parametrize(("stage", "expected_count"), [("analyzer", 3), ("reviser", 2)])
def test_reviser_candidate_is_not_normalized_before_original_indices_are_applied(draft, tmp_path, stage, expected_count):
    from bounded_investigator import workflow
    from pydantic_ai.usage import RunUsage, UsageLimits

    candidate = draft.model_copy(deep=True)
    candidate.artifacts[0].kind = "file"
    candidate.artifacts[0].value = "a.dat;b.dat"

    async def fake_run(*args, **kwargs):
        return SimpleNamespace(output=candidate)

    runner = workflow.BoundedInvestigationWorkflow.__new__(workflow.BoundedInvestigationWorkflow)
    agent = SimpleNamespace(
        stage=stage, schema=InvestigationResult,
        agent=SimpleNamespace(run=fake_run),
        model_config=SimpleNamespace(stream_output=False, output_mode="tool"),
    )
    outcome = asyncio.run(runner._run_agent(
        stage_agent=agent, prompt="material", packet_name="J00", usage=RunUsage(),
        limits=UsageLimits(), repair_runtime=workflow._RepairRuntime(), artifact_dir=tmp_path,
    ))
    assert len(outcome.output.artifacts) == expected_count
    assert len(candidate.artifacts) == 2


@pytest.mark.parametrize("mode", ["accepted", "bad_scope", "bad_candidate", "merged_inconsistent"])
def test_workflow_uses_guarded_result_and_records_failures_without_extra_calls(draft, monkeypatch, tmp_path, mode):
    from bounded_investigator import workflow
    from bounded_investigator.config import load_config
    from pathlib import Path

    config = load_config(Path(__file__).parents[1] / "config.example.yaml")
    config.format_repair.enabled = False
    config.global_reconciliation.enabled = False
    monkeypatch.setattr(workflow, "build_stage_agent", lambda **kwargs: SimpleNamespace(
        stage=kwargs["stage"], schema=kwargs["schema"],
    ))
    monkeypatch.setattr(workflow, "get_logger", lambda *_: __import__("logging").getLogger("test.scoped_revision"))
    runner = workflow.BoundedInvestigationWorkflow(config)
    monkeypatch.setattr(runner, "_stage_models", lambda: {})
    monkeypatch.setattr(runner, "_stage_log", lambda *args: None)
    monkeypatch.setattr(runner, "_finish_stage_usage", lambda *args: workflow.UsageSummary())
    targets = [] if mode == "bad_scope" else [("claim_revision", "/claims/0")]
    if mode == "merged_inconsistent":
        targets = [("claim_revision", "/claims/0")]
    review = review_for(*targets)
    candidate = draft.model_copy(deep=True)
    candidate.claims[0].source = "correct source"
    candidate.artifacts[0].detection_specificity = DetectionSpecificity.MEDIUM
    if mode == "bad_candidate":
        candidate.artifacts.pop(0)
    if mode == "merged_inconsistent":
        candidate.claims[0].evidence_level = EvidenceLevel.SUSPECTED
        candidate.claims[0].limitations = []
    calls = []

    async def run_agent(**kwargs):
        stage = kwargs["stage_agent"].stage
        calls.append(stage)
        return workflow._StageOutcome(
            status=workflow.DeliveryStatus.SUCCESS,
            output={"analyzer": draft, "reviewer": review, "reviser": candidate}[stage],
        )

    monkeypatch.setattr(runner, "_run_agent", run_agent)
    envelope = asyncio.run(runner.run(packet_name="J00", packet_text=MATERIAL, artifact_dir=tmp_path))
    assert calls == (["analyzer", "reviewer"] if mode == "bad_scope" else ["analyzer", "reviewer", "reviser"])
    if mode == "accepted":
        assert envelope.revision_performed and not envelope.revision_failed
        assert envelope.result.claims[0].source == "correct source"
        assert envelope.result.artifacts == draft.artifacts
        assert (tmp_path / "reviser/scope_applied_result.json").is_file()
    else:
        assert not envelope.revision_performed and envelope.revision_failed
        assert envelope.result == draft
        assert "revision_scope" in envelope.validation_errors
    audit = json.loads((tmp_path / "reviser/revision_scope.json").read_text(encoding="utf-8"))
    assert audit["passed"] == (mode == "accepted")
