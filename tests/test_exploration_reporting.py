"""Exploration scope survives projection, offline reporting and reconciliation."""
import asyncio
import json

import pytest

from bounded_investigator.config import load_config
from bounded_investigator.reporting.knowledge_report import project_knowledge, render_knowledge_markdown
from bounded_investigator.reporting.service import report_saved_results
from bounded_investigator.schemas import (
    DeliveryStatus, GlobalReconciliationEnvelope, GlobalReconciliationResult,
    InvestigationResult, ReconciliationStatus, RunEnvelope, UsageSummary,
)


@pytest.fixture(autouse=True)
def no_external_models():
    from pydantic_ai.models import override_allow_model_requests
    with override_allow_model_requests(False):
        yield


def canonical():
    return GlobalReconciliationResult(status=ReconciliationStatus.COMPLETE, summary='Available facts only')


def envelope():
    return GlobalReconciliationEnvelope(delivery_status=DeliveryStatus.SUCCESS,
        elapsed_seconds=0, provenance_valid=True, result=canonical())


def scope():
    return {'coverage_status': 'recorded', 'candidate_count': 100,
        'supplied_refs': ['F-0001', 'F-0002'], 'reviewed_refs': ['F-0001'],
        'primary_refs': ['F-0001', 'F-0002'],
        'draft_refs': ['F-0002'], 'support_refs': ['F-0101'],
        'selected_support_refs': [], 'nonselected_support_refs': ['F-0101'],
        'unvisited_refs': ['F-0003'], 'deferred_refs': ['F-0004'],
        'revisits': [{'ref': 'F-0001', 'reason': 'new related evidence'}],
        'stop_reason': 'planner_stop', 'model_stop_reason': 'planner_sufficient',
        'pending_questions': ['unresolved source binding']}


def test_optional_scope_preserves_static_projection_and_separates_completion():
    static, _ = project_knowledge(canonical())
    unchanged, _ = project_knowledge(canonical(), exploration_scope=None)
    assert unchanged == static and 'exploration_scope' not in static
    report, _ = project_knowledge(canonical(), exploration_scope=scope())
    assert report['exploration_scope'] == scope()
    assert report['completion_scope'] == 'available_facts_only'
    assert report['business_evaluation_status'] == 'pending_human_review'
    md = render_knowledge_markdown(report)
    assert '100' in md and 'F-0001' in md and 'F-0002' in md and 'F-0101' in md
    assert 'F-0003' in md and 'F-0004' in md and 'planner_sufficient' in md
    assert '未作为主体访问' in md and '初稿' in md and '不代表' in md
    assert '非保留支持函数（不计入候选分母）' in md
    assert 'unresolved source binding' in md


@pytest.mark.parametrize('scope_location', ['results', 'parent'])
def test_offline_report_preserves_saved_scope(tmp_path, scope_location):
    source = tmp_path / 'source'
    results = source / 'results'
    results.mkdir(parents=True)
    global_path = results / '_global_reconciliation.json'
    global_path.write_text(envelope().model_dump_json(), encoding='utf-8')
    target = results if scope_location == 'results' else source
    (target / 'exploration_scope.json').write_text(json.dumps(scope()), encoding='utf-8')
    output = tmp_path / 'report'
    summary = report_saved_results(global_result=global_path, output_dir=output)
    report = json.loads((output / '_report_ready.json').read_text(encoding='utf-8'))
    assert report['exploration_scope'] == scope()
    assert summary['completion_scope'] == 'available_facts_only'
    assert json.loads((output / 'exploration_scope.json').read_text(encoding='utf-8')) == scope()


def test_dynamic_manifest_without_scope_is_explicitly_unknown(tmp_path):
    source = tmp_path / 'source'
    results = source / 'results'
    results.mkdir(parents=True)
    (source / 'experiment_manifest.json').write_text(json.dumps({'mode': 'explore'}), encoding='utf-8')
    global_path = results / '_global_reconciliation.json'
    global_path.write_text(envelope().model_dump_json(), encoding='utf-8')
    output = tmp_path / 'report'
    report_saved_results(global_result=global_path, output_dir=output)
    report = json.loads((output / '_report_ready.json').read_text(encoding='utf-8'))
    assert report['exploration_scope']['coverage_status'] == 'unknown'
    assert '未知' in (output / '_final_report.md').read_text(encoding='utf-8')


def test_unreadable_saved_scope_does_not_become_complete_coverage(tmp_path):
    from bounded_investigator.reporting.service import load_saved_exploration_scope
    (tmp_path / 'exploration_scope.json').write_text('{interrupted write', encoding='utf-8')
    found = load_saved_exploration_scope(tmp_path)
    assert found['coverage_status'] == 'unknown'
    assert '无法读取' in found['reason']


@pytest.mark.parametrize('record_scope', [True, False])
def test_replay_preserves_scope_even_when_global_is_recomputed(monkeypatch, tmp_path, record_scope):
    from bounded_investigator import workflow
    from bounded_investigator.orchestration.replay import reconcile_saved_results
    source = tmp_path / 'source'
    source.mkdir()
    worker = RunEnvelope(packet_name='J0000', elapsed_seconds=0, revision_performed=False,
        usage=UsageSummary(), result=InvestigationResult(status='complete', summary='fixed worker'))
    (source / 'J0000.json').write_text(worker.model_dump_json(), encoding='utf-8')
    if record_scope:
        (source / 'exploration_scope.json').write_text(json.dumps(scope()), encoding='utf-8')
    else:
        (source / 'experiment_manifest.json').write_text(json.dumps({'mode': 'explore'}), encoding='utf-8')
    class FixedWorkflow:
        def __init__(self, config):
            pass
        async def run_global_reconciliation(self, **kwargs):
            return envelope()
    monkeypatch.setattr(workflow, 'BoundedInvestigationWorkflow', FixedWorkflow)
    config = load_config('config.example.yaml')
    config.global_reconciliation.enabled = True
    output = tmp_path / 'replay'
    asyncio.run(reconcile_saved_results(packet_results_dir=source, output_dir=output, config=config))
    report = json.loads((output / '_report_ready.json').read_text(encoding='utf-8'))
    if record_scope:
        assert report['exploration_scope'] == scope()
    else:
        assert report['exploration_scope']['coverage_status'] == 'unknown'
    assert json.loads((output / 'exploration_scope.json').read_text(encoding='utf-8')) == report['exploration_scope']
