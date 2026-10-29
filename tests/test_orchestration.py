"""Failure-path regression tests for scheduling and report artifact cleanup."""

import asyncio
import json
import logging

import pytest

from bounded_investigator.config import load_config
from bounded_investigator.harness import load_harness
from bounded_investigator.orchestration.batch import run_batch_dag
from bounded_investigator.orchestration import replay
from bounded_investigator.reporting.service import clear_report_outputs
from bounded_investigator.schemas import (
    DeliveryStatus, GlobalReconciliationEnvelope, InvestigationResult, RunEnvelope, UsageSummary,
)


def envelope(name='J00'):
    return RunEnvelope(packet_name=name, elapsed_seconds=0, revision_performed=False, usage=UsageSummary(),
                       result=InvestigationResult(status='complete', summary='fixture'))


def test_dag_runs_child_with_failed_upstream_and_shares_harness(monkeypatch, tmp_path):
    from pathlib import Path
    from bounded_investigator import workflow

    config = load_config('config.example.yaml')
    config.global_reconciliation.enabled = False
    packets = [tmp_path / f'J{i:02}.md' for i in range(2)]
    for path in packets:
        path.write_text('## F-0001 sub_main\n```c\nL0001: return 0;\n```', encoding='utf-8')
    manifest = tmp_path / 'manifest.json'
    manifest.write_text(json.dumps({'jobs': [
        {'job_id': 'J00', 'depends_on': []}, {'job_id': 'J01', 'depends_on': ['J00']},
    ]}), encoding='utf-8')
    harness = load_harness()
    seen = []

    class FakeWorkflow:
        def __init__(self, config, *, harness):
            assert harness is not None
            seen.append(harness)

        async def run(self, **kwargs):
            name = Path(kwargs['packet_name']).stem
            if name == 'J00':
                raise RuntimeError('upstream failed')
            assert 'status: unavailable' in kwargs['dependency_context']
            assert 'upstream failed' in kwargs['dependency_context']
            return envelope(name)

    monkeypatch.setattr(workflow, 'BoundedInvestigationWorkflow', FakeWorkflow)
    output = tmp_path / 'results'
    completed, failures = asyncio.run(run_batch_dag(
        packets=packets, output_dir=output, manifest_path=manifest,
        config=config, log=logging.getLogger('test.dag'), harness=harness,
    ))
    assert list(completed) == ['J01'] and list(failures) == ['J00']
    assert all(item is harness for item in seen) and len(seen) == 2
    assert (output / 'J00.error.json').is_file()
    assert (output / 'J01.json').is_file()


def test_report_cleanup_preserves_unrelated_files(tmp_path):
    reports = ['_report_ready.json', '_final_report.md', '_knowledge_audit.json', '_evidence_reference_audit.json']
    for name in [*reports, 'J00.json', 'notes.txt']:
        (tmp_path / name).write_text('saved', encoding='utf-8')
    clear_report_outputs(tmp_path)
    assert all(not (tmp_path / name).exists() for name in reports)
    assert (tmp_path / 'J00.json').read_text(encoding='utf-8') == 'saved'
    assert (tmp_path / 'notes.txt').is_file()


@pytest.mark.parametrize('phase', ['global', 'report'])
def test_replay_failure_clears_all_report_outputs(monkeypatch, tmp_path, phase):
    from bounded_investigator import workflow

    class FakeWorkflow:
        def __init__(self, config):
            pass

        async def run_global_reconciliation(self, **kwargs):
            if phase == 'global':
                raise RuntimeError('global failed')
            return GlobalReconciliationEnvelope(delivery_status=DeliveryStatus.SUCCESS,
                                                elapsed_seconds=0, provenance_valid=True)

    def fail_report(*args, **kwargs):
        (output / '_report_ready.json').write_text('incomplete', encoding='utf-8')
        raise RuntimeError('report failed')

    monkeypatch.setattr(workflow, 'BoundedInvestigationWorkflow', FakeWorkflow)
    monkeypatch.setattr(replay, 'save_final_outputs', fail_report)
    source = tmp_path / 'saved'
    source.mkdir()
    (source / 'J00.json').write_text(envelope().model_dump_json(), encoding='utf-8')
    output = tmp_path / 'replayed'
    output.mkdir()
    reports = ['_report_ready.json', '_final_report.md', '_knowledge_audit.json', '_evidence_reference_audit.json']
    for name in reports:
        (output / name).write_text('old success', encoding='utf-8')
    with pytest.raises(RuntimeError, match=f'{phase} failed'):
        asyncio.run(replay.reconcile_saved_results(
            packet_results_dir=source, output_dir=output, config=load_config('config.example.yaml'),
        ))
    assert all(not (output / name).exists() for name in reports)
    assert (output / '_global_reconciliation.error.json').is_file()
