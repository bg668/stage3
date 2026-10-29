"""Batch completes its saved analysis before composing HTML, entirely offline."""

import asyncio
import json
import socket
from pathlib import Path

import pytest
import pydantic_ai.models
from typer.testing import CliRunner

from bounded_investigator.config import load_config
from bounded_investigator.orchestration import batch
from bounded_investigator.schemas import (
    ArtifactFinding, BehaviorClaim, EvidenceLevel, GlobalReconciliationEnvelope,
    InvestigationResult, RunEnvelope, UsageSummary,
)
from factories import canonical
from test_html_report import install_model


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.setattr(pydantic_ai.models, 'ALLOW_MODEL_REQUESTS', False)
    connect = socket.socket.connect

    def denied(sock, address):
        # Windows asyncio implements its wakeup socketpair over loopback.
        if isinstance(address, tuple) and address[0] in {'127.0.0.1', '::1'}:
            return connect(sock, address)
        raise AssertionError('Batch HTML acceptance must not connect to a model provider')
    monkeypatch.setattr(socket.socket, 'connect', denied)


def prepare(monkeypatch, tmp_path, *, global_error=False, provenance=True, result=True):
    from bounded_investigator import workflow

    config = load_config('config.example.yaml')
    config.format_repair.enabled = False
    packets = tmp_path / 'packets'
    packets.mkdir()
    (packets / 'J00.md').write_text(
        '## F-0483 sub_409EF8，原文件 L0328–L0329 / 共 522 行\n```c\n'
        'L0328: decoded = PK11SDR_Decrypt(input);\nL0329: return decoded;\n```\n', encoding='utf-8')
    manifest = tmp_path / 'manifest.json'
    manifest.write_text(json.dumps({'jobs': [{'job_id': 'J00', 'depends_on': []}]}), encoding='utf-8')
    facts = canonical()
    facts.canonical_claims[0].source_claim_ids = ['J00:C001']
    facts.canonical_claims[0].evidence_level = EvidenceLevel.CONFIRMED
    calls = []

    class FixedWorkflow:
        def __init__(self, config, *, harness):
            pass

        async def run(self, **kwargs):
            calls.append('investigate')
            claim = facts.canonical_claims[0].model_dump()
            artifact = facts.canonical_artifacts[0].model_dump()
            investigation = InvestigationResult(status='complete', summary='offline fixture',
                claims=[BehaviorClaim.model_validate({k: v for k, v in claim.items() if k in BehaviorClaim.model_fields})],
                artifacts=[ArtifactFinding.model_validate({k: v for k, v in artifact.items() if k in ArtifactFinding.model_fields})])
            return RunEnvelope(packet_name='J00', elapsed_seconds=0, revision_performed=False,
                               usage=UsageSummary(), result=investigation)

        async def run_global_reconciliation(self, **kwargs):
            calls.append('global')
            if global_error:
                raise RuntimeError('offline global failure')
            return GlobalReconciliationEnvelope(elapsed_seconds=0, delivery_status='success',
                provenance_valid=provenance, result=facts if result else None,
                source_claim_count=1, source_artifact_count=1)

    monkeypatch.setattr(workflow, 'BoundedInvestigationWorkflow', FixedWorkflow)
    return config, packets, manifest, tmp_path / 'results', calls


def run(config, packets, manifest, output, **kwargs):
    return asyncio.run(batch.run_batch(packets, output, config, 'J*.md', manifest, **kwargs))


def test_batch_generates_html_after_global_results_with_the_existing_agent(monkeypatch, tmp_path):
    config, packets, manifest, output, analysis_calls = prepare(monkeypatch, tmp_path)
    model_calls = install_model(monkeypatch)
    run(config, packets, manifest, output)
    assert analysis_calls == ['investigate', 'global']
    assert len(model_calls) == 1 and model_calls[0][0] == 'compose'
    assert (output / 'J00.json').is_file()
    assert (output / '_global_reconciliation.json').is_file()
    assert (output / '_final_report.md').is_file()
    assert (output / 'html_report/_final_report.html').is_file()
    html_manifest = json.loads((output / 'html_report/_html_report_manifest.json').read_text(encoding='utf-8'))
    summary = json.loads((output / '_dag_summary.json').read_text(encoding='utf-8'))
    assert html_manifest['inputs']['global_result']['path'] == str((output / '_global_reconciliation.json').resolve())
    assert summary['html_report']['status'] == 'completed'
    assert summary['html_report']['output'] == 'html_report/_final_report.html'
    assert summary['html_report']['usage'] == html_manifest['usage']


def test_html_failure_retains_analysis_and_reports_a_failed_batch(monkeypatch, tmp_path):
    config, packets, manifest, output, _ = prepare(monkeypatch, tmp_path)
    install_model(monkeypatch, error=RuntimeError('offline composition failure'))
    with pytest.raises(RuntimeError, match='HTML'):
        run(config, packets, manifest, output)
    assert (output / 'J00.json').is_file()
    assert (output / '_global_reconciliation.json').is_file()
    assert (output / '_final_report.md').is_file()
    assert not (output / 'html_report/_final_report.html').exists()
    summary = json.loads((output / '_dag_summary.json').read_text(encoding='utf-8'))
    assert summary['global_reconciliation']['status'] == 'success'
    assert summary['html_report']['status'] == 'failed'
    assert 'offline composition failure' in summary['html_report']['error']
    assert json.loads((output / 'html_report/_html_report_manifest.json').read_text(encoding='utf-8'))['status'] == 'failed'


@pytest.mark.parametrize('reason', ['global_error', 'disabled_global', 'disabled_html', 'invalid_provenance', 'missing_result'])
def test_skip_paths_do_not_reuse_old_html_or_call_composition(monkeypatch, tmp_path, reason):
    config, packets, manifest, output, _ = prepare(monkeypatch, tmp_path,
        global_error=reason == 'global_error', provenance=reason != 'invalid_provenance', result=reason != 'missing_result')
    if reason == 'disabled_global':
        config.global_reconciliation.enabled = False
    html = output / 'html_report'
    html.mkdir(parents=True)
    for name in ['_final_report.html', '_html_report.json', '_report_document.json', '_html_report_manifest.json']:
        (html / name).write_text('old success', encoding='utf-8')
    (html / 'notes.txt').write_text('keep', encoding='utf-8')
    model_calls = install_model(monkeypatch)
    kwargs = {'generate_html': False} if reason == 'disabled_html' else {}
    run(config, packets, manifest, output, **kwargs)
    assert model_calls == []
    assert not (html / '_final_report.html').exists()
    assert not (html / '_html_report_manifest.json').exists()
    assert (html / 'notes.txt').read_text(encoding='utf-8') == 'keep'
    status = json.loads((output / '_dag_summary.json').read_text(encoding='utf-8'))['html_report']
    assert status['status'] == 'skipped' and status['reason']


def test_independent_mode_remains_packet_only_and_does_not_use_a_stale_global(monkeypatch, tmp_path):
    config, packets, manifest, output, analysis_calls = prepare(monkeypatch, tmp_path)
    manifest.unlink()
    output.mkdir()
    (output / '_global_reconciliation.json').write_text('stale global', encoding='utf-8')
    calls = install_model(monkeypatch)
    run(config, packets, None, output)
    assert analysis_calls == ['investigate'] and calls == []
    assert (output / 'J00.json').is_file()
    assert not (output / 'html_report/_final_report.html').exists()


def test_custom_pattern_uses_exact_batch_sources_and_still_audits_code(monkeypatch, tmp_path):
    config, packets, manifest, output, _ = prepare(monkeypatch, tmp_path)
    source = packets / 'J00.md'
    selected = source.with_suffix('.txt')
    source.rename(selected)
    # Not part of this manifest or pattern, and must not enter report source checks.
    (packets / 'J99.md').write_text('unrelated packet', encoding='utf-8')
    calls = install_model(monkeypatch, include_code=True)
    asyncio.run(batch.run_batch(packets, output, config, 'J*.txt', manifest))
    report = output / 'html_report'
    m = json.loads((report / '_html_report_manifest.json').read_text(encoding='utf-8'))
    assert [item['path'] for item in m['inputs']['packets']] == [str(selected.resolve())]
    assert m['validation']['source_audit']['evidence_reference_status'] == 'completed'
    assert m['validation']['source_audit']['passed'] is True
    assert len(calls) == 1
    document = json.loads((report / '_html_report.json').read_text(encoding='utf-8'))
    code = [block['text'] for section in document['sections'] for block in section['blocks'] if block['type'] == 'code']
    assert code == ['L0328: decoded = PK11SDR_Decrypt(input);\nL0329: return decoded;']


def test_auto_report_reuses_saved_expert_scope_and_additional_inventory(monkeypatch, tmp_path):
    config, packets, manifest, output, _ = prepare(monkeypatch, tmp_path)
    scope = tmp_path / 'scope.json'
    scope.write_text(json.dumps({'materials': [{'scope_id': 'runtime_trace', 'label': '运行轨迹',
        'kind': 'runtime_trace', 'target': 'execution trace', 'status': 'unavailable', 'impact': 'major',
        'source': 'expert attestation', 'details': '本次输入不含运行轨迹'}]}), encoding='utf-8')
    extras = tmp_path / 'extra_material'
    extras.mkdir()
    extra = extras / 'resource.bin'
    extra.write_bytes(b'actual supplied bytes')
    install_model(monkeypatch)
    run(config, packets, manifest, output, scope_file=scope, additional_input_dir=extras)
    report = output / 'html_report'
    m = json.loads((report / '_html_report_manifest.json').read_text(encoding='utf-8'))
    assert m['inputs']['evidence_scope']['path'] == str((output / '_evidence_scope.json').resolve())
    assert m['inputs']['saved_inventory']['path'] == str((output / '_evidence_inventory.json').resolve())
    projection = json.loads((report / m['artifacts']['attempt_dir'] / 'knowledge_projection.json').read_text(encoding='utf-8'))
    original = json.loads((output / '_report_ready.json').read_text(encoding='utf-8'))
    assert projection['material_suggestions'] == original['material_suggestions']
    inventory = json.loads((output / '_evidence_inventory.json').read_text(encoding='utf-8'))
    assert any(item['path'] == str(extra.resolve()) for item in inventory['additional_files'])


def test_missing_composition_stage_is_rejected_before_investigation(monkeypatch, tmp_path):
    config, packets, manifest, output, calls = prepare(monkeypatch, tmp_path)
    del config.stages['report_composition']
    with pytest.raises(ValueError, match='report_composition'):
        run(config, packets, manifest, output)
    assert calls == []


def test_context_cannot_be_removed_by_batch_html_cleanup(monkeypatch, tmp_path):
    config, packets, manifest, output, calls = prepare(monkeypatch, tmp_path)
    context = output / 'html_report/_html_report.json'
    context.parent.mkdir(parents=True)
    context.write_text('{"meta":{"id":"preserve-this-input"}}', encoding='utf-8')
    before = context.read_bytes()
    with pytest.raises(ValueError, match='input|Input'):
        run(config, packets, manifest, output, context_file=context)
    assert context.read_bytes() == before and calls == []


def test_html_directory_alias_is_rejected_before_old_output_cleanup(monkeypatch, tmp_path):
    config, packets, manifest, output, calls = prepare(monkeypatch, tmp_path)
    html = output / 'html_report'
    html.mkdir(parents=True)
    old = html / '_final_report.html'
    old.write_text('keep until output paths are validated', encoding='utf-8')
    resolve = Path.resolve

    def alias(path, *args, **kwargs):
        return resolve(output) if path == html else resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'resolve', alias)
    with pytest.raises(ValueError, match='directory'):
        run(config, packets, manifest, output)
    assert old.is_file() and calls == []


@pytest.mark.parametrize('generate_html', [False, True])
def test_additional_input_directory_cannot_be_cleaned_as_html_output(monkeypatch, tmp_path, generate_html):
    config, packets, manifest, output, calls = prepare(monkeypatch, tmp_path)
    install_model(monkeypatch)
    extra = output / 'html_report'
    extra.mkdir(parents=True)
    material = extra / '_html_report.json'
    material.write_text('{"actual":"input material"}', encoding='utf-8')
    before = material.read_bytes()
    with pytest.raises(ValueError, match='directory'):
        run(config, packets, manifest, output, additional_input_dir=extra, generate_html=generate_html)
    assert material.read_bytes() == before and calls == []


@pytest.mark.parametrize('kind', ['scope', 'manifest'])
def test_other_explicit_input_files_are_protected_from_html_cleanup(monkeypatch, tmp_path, kind):
    config, packets, manifest, output, calls = prepare(monkeypatch, tmp_path)
    install_model(monkeypatch)
    html = output / 'html_report'
    html.mkdir(parents=True)
    name = '_html_report.json' if kind == 'scope' else '_html_report_manifest.json'
    material = html / name
    material.write_text('{"materials":[]}' if kind == 'scope' else manifest.read_text(encoding='utf-8'), encoding='utf-8')
    before = material.read_bytes()
    kwargs = {'scope_file': material} if kind == 'scope' else {}
    with pytest.raises(ValueError, match='input|Input'):
        run(config, packets, material if kind == 'manifest' else manifest, output, **kwargs)
    assert material.read_bytes() == before and calls == []


def test_batch_cli_passes_context_and_prints_the_generated_html_path(monkeypatch, tmp_path):
    from bounded_investigator import cli

    _, packets, manifest, output, _ = prepare(monkeypatch, tmp_path)
    install_model(monkeypatch)
    monkeypatch.setattr(cli, 'configure_logging', lambda settings: None)
    context = tmp_path / 'context.json'
    context.write_text(json.dumps({'meta': {'id': 'batch-sample', 'title': 'Batch report', 'date': '2026-10-27'},
        'hashes': [{'type': 'SHA256', 'value': 'a' * 64, 'source': 'fixture'}],
        'attack': [], 'attribution': None, 'yara': None}), encoding='utf-8')
    result = CliRunner().invoke(cli.app, ['batch', str(packets), '--manifest', str(manifest),
        '--output-dir', str(output), '--config', 'config.example.yaml', '--context-file', str(context)])
    assert result.exit_code == 0, result.output
    assert str(output / 'html_report/_final_report.html') in result.output
    data = json.loads((output / 'html_report/_html_report.json').read_text(encoding='utf-8'))
    assert data['meta']['id'] == 'batch-sample'


def test_batch_cli_can_explicitly_disable_html(monkeypatch, tmp_path):
    from bounded_investigator import cli

    _, packets, manifest, output, _ = prepare(monkeypatch, tmp_path)
    calls = install_model(monkeypatch)
    monkeypatch.setattr(cli, 'configure_logging', lambda settings: None)
    result = CliRunner().invoke(cli.app, ['batch', str(packets), '--manifest', str(manifest),
        '--output-dir', str(output), '--config', 'config.example.yaml', '--no-html-report'])
    assert result.exit_code == 0, result.output
    assert calls == []
    assert json.loads((output / '_dag_summary.json').read_text(encoding='utf-8'))['html_report']['status'] == 'skipped'
