from copy import deepcopy
from types import SimpleNamespace

import pytest

from bounded_investigator.prompts import reviewer_instructions, reviser_instructions, reviser_prompt
from bounded_investigator.revision import merge_scoped_revision, validate_revision_scope
from bounded_investigator.schemas import Confidence, CoverageReview, EvidenceLevel, InvestigationResult, ReviewIssue


@pytest.fixture
def draft():
    claim = dict(behavior="read", condition="enabled", source="input", action="ReadFile",
                 sink="buffer", confidence="high", evidence_level="confirmed", limitations=[],
                 evidence=[dict(location="F-0001 L1", observation="read input")])
    artifact = dict(kind="file", value="sample.dat", role="created_output", detection_specificity="low",
                    meaning="output", confidence="high", evidence=deepcopy(claim["evidence"]))
    return InvestigationResult.model_validate(dict(
        status="complete_with_gaps", summary="original summary", claims=[claim, deepcopy(claim)],
        artifacts=[artifact], gaps=[dict(gap_type="evidence", question="helper?", reason="not supplied",
        scope="outside_packet", impact="major")], handoffs=[], material_needs=[],
    ))


def review_for(*fixes, **issue_overrides):
    issue = dict(issue_id="R1", severity="major", scope="packet_internal", kind="wrong_claim",
                 description="wrong result", required_fix="correct selected objects", fixes=list(fixes))
    issue.update(issue_overrides)
    return CoverageReview.model_validate(dict(requires_revision=True, review_summary="needs fix", issues=[issue]))


def fix(kind, *targets):
    return dict(type=kind, targets=list(targets))


def test_review_schema_exposes_typed_fixes_without_raw_permission_paths():
    properties = ReviewIssue.model_json_schema()["properties"]
    assert "fixes" in properties
    assert "revision_targets" not in properties
    review = review_for(fix("reference_format", "/claims/0"), fix("claim_revision", "/claims/1"))
    assert review.model_dump(mode="json")["issues"][0]["fixes"] == [
        fix("reference_format", "/claims/0"), fix("claim_revision", "/claims/1"),
    ]


@pytest.mark.parametrize("output_mode", ["tool", "native", "prompted"])
def test_actual_agent_output_schema_uses_typed_review_protocol(output_mode):
    from pydantic_ai import Agent
    from pydantic_ai.models.test import TestModel

    from bounded_investigator.llm import wrap_output

    # Construct the same output wrapper as production without running any model.
    agent = Agent(TestModel(), output_type=wrap_output(SimpleNamespace(output_mode=output_mode), CoverageReview))
    schema = agent.output_json_schema()
    properties = schema["$defs"]["ReviewIssue"]["properties"]
    assert "fixes" in properties
    assert "revision_targets" not in properties
    assert schema["$defs"]["RevisionFix"]["properties"]["type"]["enum"] == [
        "reference_format", "evidence_revision", "claim_revision", "artifact_classification",
        "gap_revision", "append_item", "delete_item",
    ]


def test_claim_revision_includes_all_semantic_fields_and_summary_but_not_evidence(draft):
    plan = validate_revision_scope(draft, review_for(fix("claim_revision", "/claims/0")))
    assert plan.replacements == {("summary",)} | {
        ("claims", 0, field) for field in ("behavior", "condition", "source", "action", "sink", "confidence", "evidence_level", "limitations")
    }


def test_reference_format_is_narrow_and_preserves_observations(draft):
    candidate = draft.model_copy(deep=True)
    candidate.claims[0].evidence[0].location = "F-0001 L2"
    candidate.claims[0].evidence[0].observation = "unrequested rewrite"
    candidate.claims[0].behavior = "unrequested behavior"
    candidate.summary = "unrequested summary"
    outcome = merge_scoped_revision(draft, candidate, review_for(fix("reference_format", "/claims/0")))
    assert outcome.result.claims[0].evidence[0].location == "F-0001 L2"
    assert outcome.result.claims[0].evidence[0].observation == draft.claims[0].evidence[0].observation
    assert outcome.result.claims[0].behavior == draft.claims[0].behavior
    assert outcome.result.summary == draft.summary


def test_each_fix_type_stays_bound_to_its_own_targets(draft):
    plan = validate_revision_scope(draft, review_for(
        fix("reference_format", "/claims/0"), fix("artifact_classification", "/artifacts/0"),
    ))
    assert ("claims", 0, "evidence", 0, "location") in plan.replacements
    assert ("claims", 0, "behavior") not in plan.replacements
    assert ("artifacts", 0, "role") in plan.replacements
    assert ("artifacts", 0, "evidence", 0, "location") not in plan.replacements


def test_artifact_classification_scope_includes_kind_without_other_permissions(draft):
    plan = validate_revision_scope(draft, review_for(fix("artifact_classification", "/artifacts/0")))
    assert plan.replacements == {
        ("artifacts", 0, field) for field in ("kind", "role", "detection_specificity", "meaning")
    }


def test_kind_correction_preserves_value_confidence_evidence_and_other_fields(draft):
    draft.artifacts[0].kind = "mutex"
    draft.artifacts[0].value = "_PERSIST"
    candidate = draft.model_copy(deep=True)
    candidate.artifacts[0].kind = "string"
    candidate.artifacts[0].role = "config_marker"
    candidate.artifacts[0].meaning = "suffix for a derived mutex name"
    candidate.artifacts[0].value = "unrequested replacement"
    candidate.artifacts[0].confidence = Confidence.LOW
    candidate.artifacts[0].evidence[0].location = "F-0001 L2"
    candidate.artifacts[0].evidence[0].observation = "unrequested observation"
    candidate.summary = "unrequested summary"
    outcome = merge_scoped_revision(draft, candidate, review_for(fix("artifact_classification", "/artifacts/0")))
    expected = draft.model_copy(deep=True)
    expected.artifacts[0].kind = "string"
    expected.artifacts[0].role = "config_marker"
    expected.artifacts[0].meaning = "suffix for a derived mutex name"
    assert outcome.result == expected
    assert set(outcome.audit["ignored_paths"]) == {
        "/summary", "/artifacts/0/value", "/artifacts/0/confidence",
        "/artifacts/0/evidence/0/location", "/artifacts/0/evidence/0/observation",
    }


def test_both_roles_and_compiled_scope_allow_artifact_kind_correction(draft):
    for instructions in (reviewer_instructions(), reviser_instructions()):
        policy = next(line for line in instructions.splitlines() if line.startswith("- artifact_classification："))
        assert "允许修改 kind, role, detection_specificity, meaning" in policy
        assert "。value/confidence 和证据保持原值。" in policy
    prompt = reviser_prompt("J13", "material", draft, review_for(fix("artifact_classification", "/artifacts/0")))
    assert "replace /artifacts/0/kind" in prompt
    assert "replace /artifacts/0/value" not in prompt


def test_types_union_permissions_for_one_target(draft):
    plan = validate_revision_scope(draft, review_for(
        fix("reference_format", "/claims/0"), fix("claim_revision", "/claims/0"),
    ))
    assert ("claims", 0, "behavior") in plan.replacements
    assert ("claims", 0, "evidence", 0, "location") in plan.replacements
    assert ("claims", 0, "evidence", 0, "observation") not in plan.replacements


def test_evidence_revision_supports_splitting_without_rewriting_claim(draft):
    candidate = draft.model_copy(deep=True)
    candidate.claims[0].evidence.append(deepcopy(candidate.claims[0].evidence[0]))
    candidate.claims[0].behavior = "unrequested rewrite"
    result = merge_scoped_revision(draft, candidate, review_for(fix("evidence_revision", "/claims/0"))).result
    assert len(result.claims[0].evidence) == 2
    assert result.claims[0].behavior == draft.claims[0].behavior


def test_gap_revision_includes_status_and_summary(draft):
    plan = validate_revision_scope(draft, review_for(fix("gap_revision", "/gaps/0")))
    assert {("gaps", 0), ("summary",), ("status",)} == plan.replacements


def test_delete_wins_only_over_bulk_reference_selection(draft):
    plan = validate_revision_scope(draft, review_for(
        fix("reference_format", "/claims/*", "/artifacts/*"), fix("delete_item", "/artifacts/0"),
    ))
    assert plan.deletions["artifacts"] == {0}
    assert not any(path[0] == "artifacts" for path in plan.replacements)
    assert ("claims", 0, "evidence", 0, "location") in plan.replacements


@pytest.mark.parametrize("other_type", ["reference_format", "evidence_revision", "artifact_classification"])
def test_explicitly_editing_and_deleting_same_object_is_still_a_conflict(draft, other_type):
    with pytest.raises(ValueError, match="replace and delete"):
        validate_revision_scope(draft, review_for(fix(other_type, "/artifacts/0"), fix("delete_item", "/artifacts/0")))


@pytest.mark.parametrize(("kind", "target"), [
    ("claim_revision", "/artifacts/0"), ("artifact_classification", "/claims/0"),
    ("gap_revision", "/claims/0"), ("reference_format", "/gaps/0"),
    ("claim_revision", "/claims/0/source"), ("claim_revision", "/claims/*"),
    ("evidence_revision", "/claims/*"), ("reference_format", "/claims/99"),
    ("reference_format", "/claims/01"), ("delete_item", "/claims/*"),
    ("delete_item", "/claims/0/source"), ("append_item", "/claims/0"),
    ("append_item", "/claims/3"), ("claim_revision", "/"),
])
def test_invalid_type_target_combinations_fail_closed(draft, kind, target):
    with pytest.raises(ValueError):
        validate_revision_scope(draft, review_for(fix(kind, target)))


@pytest.mark.parametrize("entry", [fix("unknown", "/claims/0"), fix("claim_revision")])
def test_unknown_type_and_empty_targets_are_rejected(entry):
    with pytest.raises(ValueError):
        review_for(entry)


def test_legacy_raw_targets_are_readable_but_never_authorize_new_revision(draft):
    review = review_for(revision_targets=[dict(operation="replace", path="/claims/0")])
    assert review.issues[0].revision_targets
    with pytest.raises(ValueError, match="fixes"):
        validate_revision_scope(draft, review)


def test_raw_targets_cannot_expand_a_typed_fix(draft):
    review = review_for(fix("reference_format", "/claims/0"),
                       revision_targets=[dict(operation="replace", path="/claims/0")])
    plan = validate_revision_scope(draft, review)
    assert plan.replacements == {("claims", 0, "evidence", 0, "location")}


def test_both_roles_receive_policy_and_reviser_receives_compiled_scope(draft):
    for instructions in (reviewer_instructions(), reviser_instructions()):
        for name in ("reference_format", "evidence_revision", "claim_revision", "artifact_classification", "gap_revision", "append_item", "delete_item"):
            assert name in instructions
    prompt = reviser_prompt("J00", "material {{raw}}", draft, review_for(fix("claim_revision", "/claims/0")))
    assert "claim_revision" in prompt
    assert "replace /claims/0/behavior" in prompt
    assert "replace /summary" in prompt
    assert "material {{raw}}" in prompt


def test_semantic_downgrade_accepts_linked_confidence_and_limitations(draft):
    candidate = draft.model_copy(deep=True)
    candidate.claims[0].evidence_level = EvidenceLevel.SUSPECTED
    candidate.claims[0].confidence = Confidence.MEDIUM
    candidate.claims[0].limitations = ["source-to-sink not closed"]
    outcome = merge_scoped_revision(draft, candidate, review_for(fix("claim_revision", "/claims/0")))
    assert outcome.result.claims[0].confidence.value == "medium"
    assert outcome.result.claims[0].limitations == ["source-to-sink not closed"]


def test_whole_evidence_revision_subsumes_bulk_locations_when_list_shrinks(draft):
    draft.claims[0].evidence.append(deepcopy(draft.claims[0].evidence[0]))
    candidate = draft.model_copy(deep=True)
    candidate.claims[0].evidence.pop()
    outcome = merge_scoped_revision(draft, candidate, review_for(
        fix("reference_format", "/claims/*"), fix("evidence_revision", "/claims/0"),
    ))
    assert outcome.result.claims[0].evidence == candidate.claims[0].evidence


def test_explicit_empty_evidence_owner_and_delete_still_conflict(draft):
    draft.artifacts[0].evidence.clear()
    with pytest.raises(ValueError, match="replace and delete"):
        validate_revision_scope(draft, review_for(
            fix("reference_format", "/artifacts/0"), fix("delete_item", "/artifacts/0"),
        ))
