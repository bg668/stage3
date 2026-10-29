"""Source locations must survive role loading and dependency handoff."""

from importlib.resources import files
import json

from bounded_investigator.harness import load_harness
from bounded_investigator.orchestration.dag import render_dependency_handoffs
from bounded_investigator.schemas import PartialInvestigationResult
from test_dag import _envelope


def test_condition_skill_is_loaded_once_by_analyzer_and_reviewer_only(tmp_path):
    harness = load_harness()
    harness.save(tmp_path)
    snapshot = json.loads((tmp_path / 'harness_snapshot.json').read_text(encoding='utf-8'))
    path = 'skills/control-flow-conditions/SKILL.md'
    assert path in snapshot['files']
    body = snapshot['files'][path].split('---', 2)[2].strip()
    assert all(harness.instructions(role).count(body) == 1 for role in ['analyzer', 'reviewer'])
    assert body not in harness.instructions('reviser')
    assert snapshot['files']['skills/packet-fact-boundary/SKILL.md']


def test_complete_artifact_handoff_keeps_original_source_locations():
    envelope = _envelope('J00')
    artifact = envelope.result.artifacts[0]
    artifact.evidence[0] = artifact.evidence[0].model_copy(deep=True)
    artifact.evidence[0].location = 'F-0012 L4-L6'
    # A different claim range prevents an unrelated claim from satisfying the assertion.
    envelope.result.claims[0].evidence[0].location = 'F-0020 L1-L3'
    text = render_dependency_handoffs(['J00'], {'J00': envelope}, {})
    assert 'evidence=F-0012 L4-L6' in text.split('artifacts:')[1]


def test_partial_artifact_handoff_keeps_original_source_locations():
    envelope = _envelope('J00')
    result = envelope.result
    envelope.result = None
    envelope.partial_result = PartialInvestigationResult(summary='partial', claims=[], artifacts=result.artifacts)
    text = render_dependency_handoffs(['J00'], {'J00': envelope}, {})
    assert 'evidence=F-1 L1-L5' in text.split('artifacts:')[1]


def test_trusted_reference_contract_does_not_offer_summary_sections_as_locations():
    rules = files('bounded_investigator').joinpath('prompts/investigation_result.rules.md').read_text(encoding='utf-8')
    assert '原始源码行段或节' not in rules
    assert 'F-XXXX Lx[-Ly]' in rules
    assert 'dependency J00 summary' in rules
