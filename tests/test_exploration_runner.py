import asyncio
import hashlib
import json
from pathlib import Path

import pytest

from bounded_investigator.schemas import InvestigationResult, RunEnvelope, UsageSummary
from test_upstream_pipeline import fixture_inputs
from test_exploration_planner import config


def sources(tmp_path):
    pp, tp = fixture_inputs(tmp_path, edges=[['F-0001', 'F-0002'], ['F-0001', 'F-0003']])
    pre = json.loads(pp.read_text())
    pre['functions'][0]['is_entry'] = True
    pp.write_text(json.dumps(pre), encoding='utf-8')
    tri = json.loads(tp.read_text())
    tri['preprocess_ref']['sha256'] = hashlib.sha256(pp.read_bytes()).hexdigest()
    tp.write_text(json.dumps(tri), encoding='utf-8')
    return pp, tp


def envelope(name, result, reviewed=True):
    return RunEnvelope(packet_name=name, elapsed_seconds=0, revision_performed=False, review_completed=reviewed,
                       result=result, usage=UsageSummary())


def test_entry_then_requested_function_and_normal_stop(monkeypatch, tmp_path):
    from bounded_investigator.orchestration import exploration
    from bounded_investigator.orchestration.exploration_models import PlanDecision
    pp, tp = sources(tmp_path)
    inputs, contexts, plans = [], [], []
    class Workflow:
        def __init__(self, *args, **kwargs): pass
        async def run(self, **kw):
            inputs.append(kw['packet_text'])
            contexts.append(kw['dependency_context'])
            if len(inputs) == 1:
                result = InvestigationResult(status='complete_with_gaps', summary='entry facts', material_needs=[{
                    'kind': 'function_code', 'target': 'F-0002', 'reason': 'bind source',
                    'evidence': [{'location': 'F-0001 L1', 'observation': 'calls source'}]}])
            else:
                result = InvestigationResult(status='complete', summary='followup')
            return envelope(kw['packet_name'], result)
    async def choose(**kw):
        plans.append(kw['candidates'])
        if len(plans) == 1:
            target = next(c for c in kw['candidates'] if c.source_task_ids and c.primary_refs == ['F-0002'])
            return PlanDecision(action='analyze', candidate_id=target.candidate_id, reason='gap', expected_value='source')
        return PlanDecision(action='stop', reason='pilot evidence enough')
    monkeypatch.setattr(exploration, 'BoundedInvestigationWorkflow', Workflow)
    monkeypatch.setattr(exploration, 'choose_next', choose)
    cfg = config()
    cfg.global_reconciliation.enabled = False
    out = tmp_path / 'run'
    result = asyncio.run(exploration.run_exploration(preprocess=pp, triage=tp, config=cfg, output_dir=out))
    assert len(inputs) == 2 and '## F-0001' in inputs[0] and '## F-0002' not in inputs[0]
    assert '## F-0002' in inputs[1]
    assert result['stop_reason'] == 'no_executable_candidates'
    assert result['reviewed_refs'] == ['F-0001', 'F-0002']
    assert len(list((out / 'source/code').glob('*.txt'))) == 3
    assert (out / 'experiment_manifest.json').is_file() and (out / 'results/exploration_events.jsonl').is_file()
    assert result['candidate_count'] == 2 and not result['full_candidate_coverage_implies_business_completion']


def test_candidate_repeat_needs_relevant_new_fact_not_unrelated_task(tmp_path):
    from bounded_investigator.orchestration.exploration import candidate_allowed
    from bounded_investigator.orchestration.exploration_models import Candidate
    candidate = Candidate(candidate_id='C-x', primary_refs=['F-0001'], question='reworded question', issue_key='same-call-site',
                          relevant_evidence_ids=['fact-1'])
    history = [{'primary_refs': ['F-0001'], 'issue_key': 'same-call-site', 'material_fingerprint': 'source-v1',
                'post_evidence_ids': ['fact-1']}]
    assert not candidate_allowed(candidate, history, material_fingerprint='source-v1')
    assert not candidate_allowed(candidate, history + [{'primary_refs': ['F-0002'], 'issue_key': 'other',
        'material_fingerprint': 'source-v2', 'post_evidence_ids': ['unrelated-fact']}], material_fingerprint='source-v1')
    candidate.relevant_evidence_ids.append('new-relevant-fact')
    assert candidate_allowed(candidate, history, material_fingerprint='source-v1')
    assert not candidate_allowed(candidate, history * 2, material_fingerprint='source-v1')


def test_failure_or_draft_does_not_make_reviewed_facts(monkeypatch, tmp_path):
    from bounded_investigator.orchestration import exploration
    pp, tp = sources(tmp_path)
    class Workflow:
        def __init__(self, *args, **kwargs): pass
        async def run(self, **kw):
            return envelope(kw['packet_name'], InvestigationResult(status='complete', summary='unreviewed draft'), False)
    async def fail(**kw):
        assert kw['reviewed_facts'] == [] and kw['drafts'][0]['resolved'] is False
        raise exploration.PlanningFailed('invalid choice')
    monkeypatch.setattr(exploration, 'BoundedInvestigationWorkflow', Workflow)
    monkeypatch.setattr(exploration, 'choose_next', fail)
    cfg = config()
    cfg.global_reconciliation.enabled = False
    result = asyncio.run(exploration.run_exploration(preprocess=pp, triage=tp, config=cfg, output_dir=tmp_path / 'out'))
    assert result['stop_reason'] == 'planning_failed' and result['reviewed_refs'] == []
    assert result['draft_refs'] == ['F-0001'] and result['unvisited_refs'] == ['F-0002']


def test_budget_crossing_draft_never_starts_planner_or_global(monkeypatch, tmp_path):
    from pydantic_ai.usage import RequestUsage
    from bounded_investigator.orchestration import exploration
    pp, tp = sources(tmp_path)
    calls = []
    class Workflow:
        def __init__(self, *args, **kw): self.budget = kw['sample_budget']
        async def run(self, **kw):
            calls.append('analyzer')
            self.budget.record(phase='analysis', packet_name=kw['packet_name'], stage='analyzer', started=0,
                usage=RequestUsage(input_tokens=170000, output_tokens=1000), status='completed')
            return envelope(kw['packet_name'], InvestigationResult(status='complete', summary='draft with findings'), False)
        async def run_global_reconciliation(self, **kw):
            raise AssertionError('unreviewed draft cannot become global facts')
    async def forbidden(**kw):
        raise AssertionError('exhausted analysis budget cannot call planner')
    monkeypatch.setattr(exploration, 'BoundedInvestigationWorkflow', Workflow)
    monkeypatch.setattr(exploration, 'choose_next', forbidden)
    result = asyncio.run(exploration.run_exploration(preprocess=pp, triage=tp, config=config(), output_dir=tmp_path / 'out'))
    assert calls == ['analyzer'] and result['stop_reason'] == 'analysis_budget_exhausted'
    assert result['deferred_refs'] == ['F-0002']
    manifest = json.loads((tmp_path / 'out/experiment_manifest.json').read_text())
    assert manifest['global_reconciliation']['reason'] == 'no_reviewed_results'
    assert manifest['budget']['analysis_excess_tokens'] == 11000


def test_round_cap_and_nonselected_support_are_separate(monkeypatch, tmp_path):
    from bounded_investigator.orchestration import exploration
    from bounded_investigator.orchestration.exploration_models import PlanDecision
    pp, tp = sources(tmp_path)
    seen = []
    class Workflow:
        def __init__(self, *args, **kw): pass
        async def run(self, **kw):
            seen.append(kw['packet_text'])
            result = InvestigationResult(status='complete_with_gaps', summary='need helper', material_needs=[{
                'kind': 'function_code', 'target': 'F-0003', 'reason': 'inspect unretained helper',
                'evidence': [{'location': 'F-0001 L1', 'observation': 'calls helper'}]}])
            return envelope(kw['packet_name'], result)
    async def choose(**kw):
        candidate = next(c for c in kw['candidates'] if c.support_refs == ['F-0003'])
        assert candidate.primary_refs == ['F-0001'] and candidate.source_task_ids == ['J0000']
        return PlanDecision(action='analyze', candidate_id=candidate.candidate_id, reason='bound helper', expected_value='meaning')
    monkeypatch.setattr(exploration, 'BoundedInvestigationWorkflow', Workflow)
    monkeypatch.setattr(exploration, 'choose_next', choose)
    cfg = config()
    cfg.exploration.max_rounds = 2
    cfg.global_reconciliation.enabled = False
    result = asyncio.run(exploration.run_exploration(preprocess=pp, triage=tp, config=cfg, output_dir=tmp_path / 'out'))
    assert result['stop_reason'] == 'round_limit' and result['candidate_count'] == 2
    assert result['nonselected_support_refs'] == ['F-0003'] and result['primary_refs'] == ['F-0001']
    assert result['unvisited_refs'] == ['F-0002'] and len(result['revisits']) == 1
    assert '## F-0003' in seen[1]


def test_planner_stop_preserves_unvisited_and_frozen_prompt(monkeypatch, tmp_path):
    from bounded_investigator.orchestration import exploration
    from bounded_investigator.orchestration.exploration_models import PlanDecision
    pp, tp = sources(tmp_path)
    frozen = exploration.planner_instructions()
    class Workflow:
        def __init__(self, *args, **kw): pass
        async def run(self, **kw):
            monkeypatch.setattr(exploration, 'planner_instructions', lambda: 'changed while running')
            return envelope(kw['packet_name'], InvestigationResult(status='complete', summary='entry'))
    async def choose(**kw):
        assert kw['instructions'] == frozen
        return PlanDecision(action='stop', reason='pilot limit justified')
    monkeypatch.setattr(exploration, 'BoundedInvestigationWorkflow', Workflow)
    monkeypatch.setattr(exploration, 'choose_next', choose)
    cfg = config()
    cfg.global_reconciliation.enabled = False
    result = asyncio.run(exploration.run_exploration(preprocess=pp, triage=tp, config=cfg, output_dir=tmp_path / 'out'))
    assert result['stop_reason'] == 'planner_stop' and result['model_stop_reason'] == 'pilot limit justified'
    assert result['unvisited_refs'] == ['F-0002']


def test_unknown_or_path_targets_are_rejected_not_scanned(tmp_path):
    from bounded_investigator.orchestration.exploration import _targets
    from bounded_investigator.upstream.catalog import load_catalog
    pp, tp = sources(tmp_path)
    catalog = load_catalog(pp, tp)
    for raw in ['../../F-0001.txt', 'C:\\F-0001.txt', 'F-9999', 'unresolved symbol']:
        with pytest.raises(ValueError):
            _targets(raw, catalog)
