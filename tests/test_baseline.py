import json
from pathlib import Path
import pytest
from bounded_investigator.config import load_config


@pytest.fixture(autouse=True)
def forbid_external_models():
    from pydantic_ai.models import override_allow_model_requests
    with override_allow_model_requests(False):
        yield


def test_fingerprint_changes_for_input_and_config(tmp_path):
    from bounded_investigator.orchestration.baseline import experiment_fingerprint
    p = tmp_path / 'J00.md'
    p.write_text('first', encoding='utf-8')
    config = load_config('config.example.yaml')
    a = experiment_fingerprint({'manual': tmp_path}, config, 'harness')
    p.write_text('second', encoding='utf-8')
    b = experiment_fingerprint({'manual': tmp_path}, config, 'harness')
    config.stages['analyzer'].max_output_tokens += 1
    c = experiment_fingerprint({'manual': tmp_path}, config, 'harness')
    assert len({a, b, c}) == 3


def test_baseline_refuses_existing_output_before_work(tmp_path):
    import asyncio
    from bounded_investigator.orchestration.baseline import run_baseline
    with pytest.raises(FileExistsError):
        asyncio.run(run_baseline(preprocess=tmp_path/'missing', triage=tmp_path/'missing',
            manual_dir=tmp_path/'missing', config=load_config('config.example.yaml'), output_dir=tmp_path))


def test_exhausted_budget_defers_jobs_without_constructing_workflow(monkeypatch, tmp_path):
    import asyncio
    import logging
    from bounded_investigator.orchestration.batch import run_batch_dag
    from bounded_investigator import workflow
    config = load_config('config.example.yaml')
    config.batch.concurrency = 1
    config.global_reconciliation.enabled = False
    packet = tmp_path / 'J00.md'
    packet.write_text('## F-0001 main，原文件 L0001–L0001 / 共 1 行\n```c\nL0001: return 0;\n```', encoding='utf-8')
    manifest = tmp_path / 'manifest.json'
    manifest.write_text(json.dumps({'jobs': [{'job_id': 'J00'}]}), encoding='utf-8')
    class EmptyBudget:
        def remaining(self, phase='analysis'):
            return 0
        def save(self):
            pass
    def no_workflow(*args, **kwargs):
        pytest.fail('Deferred task must not construct a model')
    monkeypatch.setattr(workflow, 'BoundedInvestigationWorkflow', no_workflow)
    output = tmp_path / 'results'
    completed, failures = asyncio.run(run_batch_dag(packets=[packet], manifest_path=manifest,
        output_dir=output, config=config, log=logging.getLogger('test'), sample_budget=EmptyBudget()))
    assert not completed and 'J00' in failures
    summary = json.loads((output / '_dag_summary.json').read_text(encoding='utf-8'))
    assert summary['jobs']['J00']['status'] == 'budget_deferred'


def _inputs(tmp_path):
    import hashlib
    source = tmp_path / 'source'
    (source / 'code').mkdir(parents=True)
    code = 'int main() { return 0; }'
    (source / 'code/F-0001.txt').write_text(code, encoding='utf-8')
    sample = {'name': 'fixture', 'sqlite_sha256': 'a'*64, 'sample_sha256': 'b'*64}
    pre = {'sample': sample, 'config': {'max_functions_per_unit': 80, 'max_chars_per_unit': 12000},
        'functions': [{'ref': 'F-0001', 'name': 'main', 'address': '0x1000',
        'decision': 'PROGRAM_ANALYZE', 'reason': 'entry', 'code_chars': len(code)}], 'call_edges': []}
    pp = source / 'preprocess.json'
    pp.write_text(json.dumps(pre), encoding='utf-8')
    tp = source / 'triage.json'
    tp.write_text(json.dumps({'sample': sample, 'preprocess_ref': {'sha256': hashlib.sha256(pp.read_bytes()).hexdigest()},
                             'final_verdicts': []}), encoding='utf-8')
    manual = tmp_path / 'manual'
    manual.mkdir()
    (manual / 'manifest.json').write_text(json.dumps({'jobs': [{'job_id': 'J00'}]}), encoding='utf-8')
    (manual / 'J00.md').write_text('## F-0001 main，原文件 L0001–L0001 / 共 1 行\n```c\nL0001: '+code+'\n```', encoding='utf-8')
    return pp, tp, manual


@pytest.mark.parametrize('total_tokens', [200000, 1])
def test_both_arms_execute_fresh_fixed_models_and_global(monkeypatch, tmp_path, total_tokens):
    import asyncio
    from pydantic_ai import Agent
    from pydantic_ai.models.test import TestModel
    from bounded_investigator import workflow
    from bounded_investigator.execution.llm import StageAgent
    from bounded_investigator.orchestration.baseline import run_baseline
    config = load_config('config.example.yaml')
    config.global_reconciliation.enabled = True
    for model in config.models.values():
        model.stream_output = False
    calls = []
    def build(*, config, stage, schema, instructions):
        calls.append(stage)
        cfg, model, provider = config.stage(stage)
        args = {'status': 'complete', 'summary': 'fixed response'}
        if stage == 'reviewer':
            args = {'requires_revision': False, 'review_summary': 'fixed review'}
        elif stage == 'global_reconciliation':
            args = {'keep_claim_ids': []}
        return StageAgent(stage, cfg.model, model, provider,
            Agent(TestModel(custom_output_args=args), output_type=schema), schema)
    monkeypatch.setattr(workflow, 'build_stage_agent', build)
    config.format_repair.enabled = False
    pp, tp, manual = _inputs(tmp_path)
    output = tmp_path / 'experiment'
    result = asyncio.run(run_baseline(preprocess=pp, triage=tp, manual_dir=manual,
                                     config=config, output_dir=output, total_tokens=total_tokens))
    for arm in ('automatic', 'manual'):
        metrics = result['arms'][arm]
        assert metrics['tasks_with_validated_result'] == ['J00']
        assert metrics['budget']['requests'] == (3 if total_tokens == 200000 else 1)
        assert (output / arm / 'results/_global_reconciliation.json').is_file() == (total_tokens == 200000)
        assert metrics['completion_rate'] == (1 if total_tokens == 200000 else 0)
        if total_tokens == 1:
            assert metrics['budget']['excess_tokens'] > 0
            assert metrics['jobs']['J00']['status'] == 'budget_partial'
            assert metrics['budget_stopped_stages']['J00'] == ['reviewer']
        assert not (output / arm / 'results/html_report').exists()
    assert calls.count('analyzer') == 4  # worker + global workflow for each fresh arm
    assert result['evaluation_status'] == 'pending_human_review'


def test_arm_failure_does_not_prevent_other_arm(monkeypatch, tmp_path):
    import asyncio
    from bounded_investigator.orchestration import batch
    from bounded_investigator.orchestration.baseline import run_baseline
    seen = []
    async def fake(**kwargs):
        arm = kwargs['output_dir'].parent.name
        seen.append(arm)
        if arm == 'automatic':
            raise RuntimeError('fixed failure')
        return {}, {}
    monkeypatch.setattr(batch, 'run_batch_dag', fake)
    pp, tp, manual = _inputs(tmp_path)
    result = asyncio.run(run_baseline(preprocess=pp, triage=tp, manual_dir=manual,
        config=load_config('config.example.yaml'), output_dir=tmp_path/'experiment'))
    assert seen == ['automatic', 'manual']
    assert result['arms']['automatic']['execution']['status'] == 'failed'
    assert result['arms']['manual']['execution']['status'] == 'finished'


@pytest.mark.parametrize('source', ['manual', 'upstream'])
def test_output_cannot_be_nested_in_protected_inputs(tmp_path, source):
    import asyncio
    from bounded_investigator.orchestration.baseline import run_baseline
    pp, tp, manual = _inputs(tmp_path)
    output = (manual if source == 'manual' else pp.parent) / 'experiment'
    with pytest.raises(ValueError, match='overlap'):
        asyncio.run(run_baseline(preprocess=pp, triage=tp, manual_dir=manual,
            config=load_config('config.example.yaml'), output_dir=output))
    assert not output.exists()
