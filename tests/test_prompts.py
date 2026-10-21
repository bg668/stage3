from importlib.resources import files
from types import SimpleNamespace

import pytest

from bounded_investigator.prompts import (
    analyzer_instructions,
    analyzer_prompt,
    format_repair_instructions,
    format_repair_prompt,
    gap_reconciliation_instructions,
    gap_reconciliation_prompt,
    render_result_context,
    reviewer_instructions,
    reviewer_prompt,
    reviser_instructions,
    reviser_prompt,
)
from bounded_investigator.schemas import (
    CoverageReview,
    EvidenceLevel,
    InvestigationResult,
    InvestigationStatus,
)


def test_prompts_are_external_and_rendered():
    instructions = analyzer_instructions()
    assert "有界调查 Worker" in instructions
    prompt = analyzer_prompt(
        "J00",
        "int main() { return 0; }",
        "[dependency J99]\nsummary: upstream\n[/dependency]",
    )
    assert '<packet name="J00">' in prompt
    assert "int main()" in prompt
    assert "[dependency J99]" in prompt
    assert "{{packet_name}}" not in prompt


def test_compact_result_context_is_not_json_dump():
    result = InvestigationResult(
        status=InvestigationStatus.COMPLETE,
        summary="done",
    )
    text = render_result_context(result)
    assert text.startswith("status: complete")
    assert '"status"' not in text
    assert "claims:" in text


def test_format_repair_prompt_is_external_and_bounded():
    assert "Format Repairer" in format_repair_instructions()
    prompt = format_repair_prompt(
        target_stage="analyzer",
        attempt=1,
        target_schema='{"type":"object"}',
        validation_error="missing field",
        original_raw_output='{"x":1}',
        current_candidate='{"x":1}',
    )
    assert '<repair target_stage="analyzer" attempt="1">' in prompt
    assert "missing field" in prompt
    assert "{{target_schema}}" not in prompt


def test_global_reconciliation_prompt_renders():
    from bounded_investigator.prompts import global_reconciliation_prompt
    rendered = global_reconciliation_prompt("[packet J00]\nclaims: none\n[/packet]")
    assert "reduced_validated_facts" in rendered
    assert "packet J00" in rendered


def test_gap_reconciliation_prompt_renders():
    assert "Global Gap Reconciler" in gap_reconciliation_instructions()
    rendered = gap_reconciliation_prompt("<historical_gaps>R:G001</historical_gaps>")
    assert "R:G001" in rendered
    assert "{{gap_context}}" not in rendered


@pytest.mark.parametrize(
    ("instructions_fn", "role", "output_schema"),
    [
        (analyzer_instructions, "有界调查 Worker", "InvestigationResult"),
        (reviewer_instructions, "Coverage Reviewer", "CoverageReview"),
        (reviser_instructions, "有界调查 Reviser", "InvestigationResult"),
    ],
)
def test_roles_share_result_rules_and_keep_their_output_contract(instructions_fn, role, output_schema):
    resource = files("bounded_investigator").joinpath("prompts", "investigation_result.rules.md")
    assert resource.is_file(), "The result quality rules must be a shared prompt resource"
    rules = resource.read_text(encoding="utf-8").strip()
    instructions = instructions_fn()
    assert instructions.count(rules) == 1
    assert role in instructions
    assert output_schema in instructions
    for required in (
        "dependency_handoffs", "condition", "source", "action", "sink",
        "location", "observation", "confidence", "evidence_level",
        "confirmed", "suspected", "lead", "unclassified", "limitations",
        "role", "detection_specificity", "gap_type", "material_needs",
        "targets", "start_line", "end_line", "status", "complete_with_gaps", "blocked",
    ):
        assert required in rules


def test_workflow_supplies_shared_instructions_without_changing_agent_schemas(monkeypatch):
    from bounded_investigator import workflow

    captured = {}

    def capture_agent(**kwargs):
        captured[kwargs["stage"]] = kwargs
        return object()

    monkeypatch.setattr(workflow, "build_stage_agent", capture_agent)
    config = SimpleNamespace(
        format_repair=SimpleNamespace(enabled=False),
        global_reconciliation=SimpleNamespace(enabled=False),
    )
    workflow.BoundedInvestigationWorkflow(config)
    rules_resource = files("bounded_investigator").joinpath("prompts", "investigation_result.rules.md")
    assert rules_resource.is_file()
    rules = rules_resource.read_text(encoding="utf-8").strip()
    assert set(captured) == {"analyzer", "reviewer", "reviser"}
    for stage, schema in (
        ("analyzer", InvestigationResult),
        ("reviewer", CoverageReview),
        ("reviser", InvestigationResult),
    ):
        assert captured[stage]["schema"] is schema
        assert rules in captured[stage]["instructions"]


@pytest.fixture
def complete_result():
    # Distinct values expose fields lost in the compact handoff, including legacy
    # target + structured targets coexisting on the same request.
    return InvestigationResult.model_validate({
        "status": "complete_with_gaps",
        "summary": "摘要 {{summary_marker}}",
        "claims": [
            {
                "behavior": f"behavior-{level.value}",
                "condition": f"condition-{level.value}",
                "source": f"source-{level.value}\n" + "原始来源{{source_marker}}" * 100,
                "action": f"action-{level.value}",
                "sink": f"sink-{level.value}",
                "confidence": confidence,
                "evidence_level": level.value,
                "limitations": [] if i == 0 else [f"limit-{i}-a", f"limit-{i}-b\n第二行"],
                "evidence": [
                    {"location": f"F-C{i} L11-L19", "observation": f"claim-{i}-observation-a"},
                    {"location": f"F-C{i} L21-L29", "observation": f"claim-{i}-{{{{observation}}}}-b"},
                ],
            }
            for i, (level, confidence) in enumerate(zip(EvidenceLevel, ("high", "medium", "low", "high")))
        ],
        "artifacts": [{
            "kind": "file", "value": "C:\\输出\\{{filename}}.bin",
            "role": "created_output", "detection_specificity": "medium",
            "meaning": "artifact-meaning", "confidence": "low",
            "evidence": [
                {"location": "F-A001 L31-L39", "observation": "artifact-observation-a"},
                {"location": "F-A002 L41-L49", "observation": "artifact-observation-b"},
            ],
        }],
        "gaps": [
            {"gap_type": gap_type, "question": f"gap-question-{i}", "reason": f"gap-reason-{i}",
             "scope": "outside_packet", "impact": "major"}
            for i, gap_type in enumerate(("investigation", "evidence", "capability", "report"))
        ],
        "handoffs": [{
            "target": "handoff-target", "reason": "handoff-reason",
            "expected_value": "handoff-expected", "priority": "high",
        }],
        "material_needs": [
            {
                "kind": "function_code", "target": "legacy-function-target",
                "targets": [
                    {"identifier": "F-0101", "start_line": 101, "end_line": 119},
                    {"identifier": "sub_402000", "start_line": 221, "end_line": 249},
                    {"identifier": "F-0103"},
                ],
                "reason": "function-need-reason", "impact": "critical",
                "evidence": [
                    {"location": "F-M001 L51-L59", "observation": "function-need-observation-a"},
                    {"location": "F-M002 L61-L69", "observation": "function-need-{{observation}}-b"},
                ],
            },
            {
                "kind": "resource_bytes", "target": "resource-target-{{raw}}",
                "reason": "resource-need-reason", "impact": "minor",
                "evidence": [{"location": "F-M003 L71-L79", "observation": "resource-need-observation"}],
            },
        ],
    })


def _nonempty_scalar_values(value):
    if isinstance(value, dict):
        for child in value.values():
            yield from _nonempty_scalar_values(child)
    elif isinstance(value, list):
        for child in value:
            yield from _nonempty_scalar_values(child)
    elif value is not None and value != "":
        yield str(value)


def test_result_context_preserves_every_field_value(complete_result):
    text = render_result_context(complete_result)
    for value in _nonempty_scalar_values(complete_result.model_dump(mode="json")):
        assert value in text
    for claim in complete_result.claims:
        assert f"confidence={claim.confidence.value}" in text
        assert f"evidence_level={claim.evidence_level.value}" in text
    for gap in complete_result.gaps:
        assert f"gap_type={gap.gap_type}" in text
    assert "target: legacy-function-target" in text
    assert "targets:" in text
    for start, end in ((101, 119), (221, 249)):
        assert f"start_line: {start}" in text
        assert f"end_line: {end}" in text


@pytest.mark.parametrize("stage", ["reviewer", "reviser"])
def test_review_and_revision_receive_the_complete_shared_context(complete_result, stage):
    review = CoverageReview(requires_revision=False, review_summary="review {{review_marker}}")
    kwargs = dict(
        packet_name="J00", packet_text="材料 {{packet_name}} {{not_a_template}}",
        draft=complete_result, dependency_context="依赖 {{dependency_marker}}",
        evidence_validation_context="核验 {{validation_marker}}",
    )
    prompt = reviewer_prompt(**kwargs) if stage == "reviewer" else reviser_prompt(**kwargs, review=review)
    assert f"<analyzer_result>\n{render_result_context(complete_result)}\n</analyzer_result>" in prompt
    for value in _nonempty_scalar_values(complete_result.model_dump(mode="json")):
        assert value in prompt
    assert kwargs["packet_text"] in prompt
    assert kwargs["dependency_context"] in prompt
    assert kwargs["evidence_validation_context"] in prompt
    if stage == "reviser":
        assert review.review_summary in prompt


@pytest.mark.parametrize("status", list(InvestigationStatus))
def test_empty_result_collections_remain_visible(status):
    result = InvestigationResult(status=status, summary="empty {{summary}}")
    text = render_result_context(result)
    assert f"status: {status.value}" in text
    assert result.summary in text
    for field in ("claims", "artifacts", "gaps", "handoffs", "material_needs"):
        assert f"{field}:\n  - none" in text


def test_legacy_unclassified_is_not_upgraded_during_handoff(complete_result):
    raw = complete_result.model_dump(mode="json")
    raw["claims"] = [raw["claims"][0]]
    del raw["claims"][0]["evidence_level"]
    legacy = InvestigationResult.model_validate(raw)
    text = render_result_context(legacy)
    assert "confidence=high" in text
    assert "evidence_level=unclassified" in text
    assert "evidence_level=confirmed" not in text
    assert legacy.claims[0].evidence_level is EvidenceLevel.UNCLASSIFIED


def test_rendering_and_handoffs_do_not_mutate_original_result(complete_result):
    original = complete_result.model_copy(deep=True)
    review = CoverageReview(requires_revision=False, review_summary="no changes")
    render_result_context(complete_result)
    reviewer_prompt("J00", "material", complete_result)
    reviser_prompt("J00", "material", complete_result, review)
    assert complete_result == original
