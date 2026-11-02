import asyncio
import importlib.util
import json
from pathlib import Path

import pytest

from bounded_investigator.config import load_config
from bounded_investigator.schemas import (
    GlobalReconciliationEnvelope, GlobalReconciliationResult, InvestigationResult, RunEnvelope, UsageSummary,
)


@pytest.fixture
def script():
    spec = importlib.util.spec_from_file_location('verify_global', Path('scripts/verify_global.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def source(tmp_path):
    root = tmp_path/'saved'
    (root/'results').mkdir(parents=True)
    (root/'input').mkdir()
    config = load_config('config.example.yaml')
    (root/'effective_config.json').write_text(config.model_dump_json(), encoding='utf-8')
    for name, reviewed in [('J0000', True), ('J0001', False)]:
        envelope = RunEnvelope(packet_name=name, elapsed_seconds=0, revision_performed=False,
            review_completed=reviewed, result=InvestigationResult(status='complete', summary=name), usage=UsageSummary())
        (root/'results'/f'{name}.json').write_text(envelope.model_dump_json(), encoding='utf-8')
        (root/'input'/f'{name}.md').write_text('# Saved packet\n', encoding='utf-8')
    (root/'results/exploration_scope.json').write_text(json.dumps({
        'candidate_count': 100, 'unvisited_refs': ['F-0003'],
        'history': [{'task_id': 'J0000', 'status': 'reviewed'}, {'task_id': 'J0001', 'status': 'draft'}],
    }), encoding='utf-8')
    return root


def test_check_only_never_builds_workflow(script, source, tmp_path, monkeypatch):
    def forbidden(*a, **kw): raise AssertionError('must not construct models')
    monkeypatch.setattr(script, 'BoundedInvestigationWorkflow', forbidden)
    output = tmp_path/'probe'
    result = asyncio.run(script.verify(source, output, check_only=True))
    assert result['status']=='check_only' and result['admitted_tasks']==['J0000']
    assert result['excluded_tasks']=={'J0001': 'not a reviewed result'}
    assert (output/'input/J0000.json').exists()
    assert json.loads((output/'exploration_scope.json').read_text())['candidate_count']==100


def test_only_global_is_called_with_budget_and_scope(script, source, tmp_path, monkeypatch):
    calls=[]
    class Workflow:
        def __init__(self, config, **kwargs):
            assert config.providers[next(iter(config.providers))].sdk_max_retries==0
            assert kwargs['sample_budget'].limit==12000
        async def run_global_reconciliation(self, **kw):
            calls.append(kw)
            return GlobalReconciliationEnvelope(delivery_status='success', elapsed_seconds=0, provenance_valid=True,
                result=GlobalReconciliationResult(status='complete', summary='available facts'))
    monkeypatch.setattr(script, 'BoundedInvestigationWorkflow', Workflow)
    result=asyncio.run(script.verify(source, tmp_path/'probe', total_tokens=12000))
    assert result['status']=='success' and len(calls)==1
    assert set(calls[0]['completed_results'])=={'J0000'}
    report=json.loads((tmp_path/'probe/_report_ready.json').read_text(encoding='utf-8'))
    assert report['exploration_scope']['unvisited_refs']==['F-0003']


def test_error_keeps_cause_and_redacts_credentials(script, source, tmp_path, monkeypatch):
    class APIError(Exception):
        body={'error': {'message': 'Server disconnected', 'api_key': 'secret'}, 'Authorization': 'Bearer secret'}
        status_code=200
    class Workflow:
        def __init__(self,*a,**kw):pass
        async def run_global_reconciliation(self,**kw):
            try: raise APIError('Server disconnected')
            except APIError as cause: raise RuntimeError('outer failure') from cause
    monkeypatch.setattr(script,'BoundedInvestigationWorkflow',Workflow)
    result=asyncio.run(script.verify(source,tmp_path/'probe'))
    assert result['status']=='failed'
    error=json.loads((tmp_path/'probe/error.json').read_text(encoding='utf-8'))
    assert [e['type'] for e in error['exception_chain']]==['RuntimeError','APIError']
    assert error['exception_chain'][1]['status_code']==200
    assert 'secret' not in json.dumps(error)
    assert not (tmp_path/'probe/_final_report.md').exists()


def test_existing_output_is_not_touched(script, source, tmp_path):
    target=tmp_path/'existing'; target.mkdir(); marker=target/'marker'; marker.write_text('keep')
    with pytest.raises(FileExistsError): asyncio.run(script.verify(source,target))
    assert marker.read_text()=='keep'


def test_no_reviewed_results_fails_before_creating_output(script, source, tmp_path):
    p=source/'results/J0000.json'
    d=json.loads(p.read_text()); d['review_completed']=False; p.write_text(json.dumps(d))
    with pytest.raises(ValueError, match='reviewed'):
        asyncio.run(script.verify(source,tmp_path/'probe'))
    assert not (tmp_path/'probe').exists()
