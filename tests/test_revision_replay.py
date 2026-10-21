"""Offline regression from the real 2026-10-21 run, not a new model evaluation."""

import json
import socket
from pathlib import Path

import pytest
from pydantic_ai import Agent

from bounded_investigator.revision import merge_scoped_revision, validate_revision_scope
from bounded_investigator.schemas import CoverageReview, InvestigationResult
from bounded_investigator.source_snippets import build_code_index_from_text


FIXTURES = Path(__file__).parent / "fixtures" / "revision_20261021"
ACTUAL_CANDIDATES = ["J00", "J02", "J03", "J06", "J07", "J08", "J09", "J10", "J11", "J12", "J13", "J14", "J15"]


@pytest.fixture(autouse=True)
def forbid_model_and_network_calls(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Replay must not call a model or the network")

    monkeypatch.setattr(Agent, "run", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)


def load_case(name):
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def replay(case):
    assert case["candidate_result"] is not None, "Do not invent historical Reviser output"
    draft = InvestigationResult.model_validate(case["analyzer_result"])
    review = CoverageReview.model_validate(case["typed_review"])
    candidate = InvestigationResult.model_validate(case["candidate_result"])
    index = build_code_index_from_text(case["packet_text"], f"{case['packet_name']}.md")
    return merge_scoped_revision(draft, candidate, review, index=index)


def changed_paths(left, right, prefix=""):
    if isinstance(left, dict) and isinstance(right, dict):
        return [p for key in left for p in changed_paths(left[key], right[key], f"{prefix}/{key}")]
    if isinstance(left, list) and isinstance(right, list) and len(left) == len(right):
        return [p for i, (a, b) in enumerate(zip(left, right)) for p in changed_paths(a, b, f"{prefix}/{i}")]
    return [] if left == right else [prefix]


@pytest.mark.parametrize("name", ACTUAL_CANDIDATES)
def test_actual_candidates_merge_to_curated_expected_results(name):
    case = load_case(name)
    outcome = replay(case)
    differences = changed_paths(case["expected_result"], outcome.result.model_dump(mode="json"))
    assert not differences, f"{name}: unexpected fields {differences}"
    assert outcome.audit["ignored_paths"] == case["expected_ignored_paths"]


@pytest.mark.parametrize(("name", "section", "deleted_indices"), [
    ("J04", "material_needs", [1]), ("J05", "artifacts", [0, 1, 3]),
])
def test_real_rejected_reviews_compile_batch_references_with_deletions(name, section, deleted_indices):
    case = load_case(name)
    assert case["candidate_result"] is None
    assert case["expected_result"] is None
    assert case["original_scope_audit"]["passed"] is False
    assert "cannot replace and delete" in case["original_scope_audit"]["error"]
    plan = validate_revision_scope(
        InvestigationResult.model_validate(case["analyzer_result"]),
        CoverageReview.model_validate(case["typed_review"]),
    )
    assert plan.deletions[section] == set(deleted_indices)
    assert plan.replacements
    assert not any(p[0] == section and p[1] in deleted_indices for p in plan.replacements if len(p) > 1)


def test_j00_behavior_and_summary_changes_are_no_longer_lost():
    case = load_case("J00")
    result = replay(case).result
    assert result.summary == case["candidate_result"]["summary"]
    assert result.claims[0].behavior == case["candidate_result"]["claims"][0]["behavior"]
    assert result.claims[2].behavior == case["candidate_result"]["claims"][2]["behavior"]
    assert "three additional attempts" in result.claims[2].behavior
    assert "three additional times" in result.claims[2].action


def test_j13_retracted_remote_entry_claim_is_not_restored_by_merge():
    case = load_case("J13")
    result = replay(case).result
    assert result.summary == case["candidate_result"]["summary"]
    assert result.claims[1].behavior == case["candidate_result"]["claims"][1]["behavior"]
    assert "调用远程入口" not in result.summary
    assert "尝试启动远程映像" not in result.claims[1].behavior
    assert "不能确认该回调启动远程映像" in result.claims[1].sink


def test_all_37_historical_out_of_scope_changes_are_accounted_for():
    old_ignored = recovered = classification_recovered = still_ignored = 0
    for name in ACTUAL_CANDIDATES:
        case = load_case(name)
        outcome = replay(case)
        old_ignored += len(case["original_scope_audit"]["ignored_paths"])
        recovered += len(case["recovered_paths"])
        classification_recovered += len(case.get("classification_recovered_paths", []))
        still_ignored += len(outcome.audit["ignored_paths"])
    assert (old_ignored, recovered, classification_recovered, still_ignored) == (37, 5, 2, 30)


def test_actual_j13_prompt_check_retains_kind_correction_without_other_changes():
    case = load_case("J13_prompt_check")
    outcome = replay(case)
    final = outcome.result.model_dump(mode="json")
    assert final == case["expected_result"]
    assert changed_paths(case["original_final_result"], final) == ["/artifacts/0/kind"]
    assert final["artifacts"][0]["kind"] == "string"
    assert final["artifacts"][0]["value"] == "_PERSIST"
    assert len(case["original_scope_audit"]["ignored_paths"]) == 4
    assert outcome.audit["ignored_paths"] == case["expected_ignored_paths"] == [
        "/claims/1/evidence/0/observation", "/claims/1/evidence/1/observation",
        "/claims/3/evidence/0/observation",
    ]


def test_j01_minor_findings_do_not_change_revision_trigger():
    case = load_case("J01")
    review = CoverageReview.model_validate(case["typed_review"])
    assert not review.requires_revision
    with pytest.raises(ValueError, match="does not request revision"):
        validate_revision_scope(InvestigationResult.model_validate(case["analyzer_result"]), review)


def test_fixtures_separate_real_outputs_from_curated_protocol_inputs():
    for path in sorted(FIXTURES.glob("J??.json")):
        case = json.loads(path.read_text(encoding="utf-8"))
        assert "curated test input" in case["provenance"]["typed_review_origin"]
        assert all("revision_targets" in i and "fixes" not in i for i in case["original_review"]["issues"])
        for record in case["provenance"]["original_records"].values():
            assert len(record["sha256"]) == 64
        assert (case["candidate_result"] is not None) == ("reviser" in case["provenance"]["original_records"])
