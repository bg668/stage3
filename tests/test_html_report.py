"""End-to-end HTML preview acceptance, with fixed responses and no network."""

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from pydantic_ai import PartStartEvent, UnexpectedModelBehavior
from pydantic_ai.messages import TextPart
from typer.testing import CliRunner

from bounded_investigator.config import AppConfig, load_config
from bounded_investigator.schemas import EvidenceLevel, GlobalReconciliationEnvelope
from factories import canonical


def settings():
    config = load_config('config.example.yaml')
    config.format_repair.enabled = False
    return config


def input_result(tmp_path):
    source = tmp_path / 'analysis'
    source.mkdir()
    facts = canonical()
    facts.canonical_claims[0].evidence_level = EvidenceLevel.CONFIRMED
    envelope = GlobalReconciliationEnvelope(
        result=facts, provenance_valid=True, elapsed_seconds=0, delivery_status='success',
        source_claim_count=1, source_artifact_count=1,
    )
    path = source / '_global_reconciliation.json'
    path.write_text(envelope.model_dump_json(indent=2), encoding='utf-8')
    (source / '_knowledge_audit.json').write_text('{"original":true}', encoding='utf-8')
    return path


def fixed_composition(*, include_code=False):
    from bounded_investigator.reporting.composition import ReportComposition
    path = Path(__file__).parent / 'fixtures/html_composition.json'
    raw = json.loads(path.read_text(encoding='utf-8'))
    if include_code:
        raw['sections'][0]['blocks'].append({'type':'code','claim_id':'G:C001','excerpt_index':0})
    return ReportComposition.model_validate(raw)


def install_model(monkeypatch, *, output=None, error=None, raw=None, repaired=None, include_code=False):
    from bounded_investigator.reporting import agent
    calls = []

    async def run(prompt, **kwargs):
        calls.append(('compose', prompt, kwargs['usage_limits']))
        kwargs['usage'].requests += 1
        if raw:
            async def events():
                yield PartStartEvent(index=0, part=TextPart(content=raw))
            await kwargs['event_stream_handler'](None, events())
        if error:
            raise error
        return SimpleNamespace(output=output or fixed_composition(include_code=include_code))

    async def repair(prompt, **kwargs):
        calls.append(('repair', prompt, kwargs['usage_limits']))
        kwargs['usage'].requests += 1
        return SimpleNamespace(output=repaired)

    def build(**kwargs):
        assert kwargs['stage'] == 'report_composition'
        return SimpleNamespace(stage=kwargs['stage'], schema=kwargs['schema'], agent=SimpleNamespace(run=run),
                               model_config=SimpleNamespace(stream_output=False, output_mode='tool'))

    monkeypatch.setattr(agent, 'build_stage_agent', build)
    monkeypatch.setattr(agent, 'build_text_stage_agent', lambda **kw: SimpleNamespace(
        stage='format_repair', agent=SimpleNamespace(run=repair),
        model_config=SimpleNamespace(stream_output=False),
    ))
    return calls


def test_html_config_is_additive_and_has_separate_budget():
    config = settings()
    stage, model, _ = config.stage('report_composition')
    assert stage.model == 'global_reconciler' and stage.max_output_tokens == 16384
    assert (config.html_report.max_model_requests, config.html_report.total_tokens_limit,
            config.html_report.wall_clock_seconds) == (2, 64000, 300)
    data = config.model_dump()
    del data['stages']['report_composition']
    assert AppConfig.model_validate(data).workflow.max_model_requests == 3
    data['html_report']['max_model_requests'] = 3
    with pytest.raises(ValidationError):
        AppConfig.model_validate(data)


def test_fixed_model_writes_preview_and_preserves_input(monkeypatch, tmp_path):
    from bounded_investigator.reporting.html_report import create_html_report
    source = input_result(tmp_path)
    originals = {p: p.read_bytes() for p in source.parent.iterdir()}
    calls = install_model(monkeypatch)
    output = tmp_path / 'html'
    manifest = asyncio.run(create_html_report(global_result=source, output_dir=output, config=settings()))
    assert manifest['status'] == 'completed' and manifest['preview_only'] is True
    assert manifest['test_fills'] == [] and manifest['validation']['template_errors'] == []
    assert manifest['report_agent']['name'] == 'report'
    assert manifest['html_skill']['name'] == 'malware-report-html'
    assert manifest['report_agent']['fingerprint'] and manifest['html_skill']['fingerprint']
    assert manifest['usage']['composition']['requests'] == 1
    assert manifest['usage']['format_repair']['requests'] == 0
    assert calls[0][2].request_limit == 1 and calls[0][2].total_tokens_limit == 64000
    assert len(calls) == 1 and 'G:C001' in calls[0][1]
    assert 'packet_dag' not in calls[0][1]
    data = json.loads((output / '_html_report.json').read_text(encoding='utf-8'))
    html = (output / '_final_report.html').read_text(encoding='utf-8')
    assert 'HTML 预览' in html and '含测试数据' not in html
    assert data['ioc']['host'][0]['value'] == 'FIREFOX.abc'
    assert 'PK11SDR_Decrypt' in html
    assert all(p.read_bytes() == value for p, value in originals.items())
    assert list((output / '_html_report_artifacts').glob('*/report_composition/validated_result.json'))
    assert list((output / '_html_report_artifacts').glob('*/report_agent_snapshot.json'))
    document = json.loads((output / '_report_document.json').read_text(encoding='utf-8'))
    assert document['sections'][0]['blocks'][0]['type'] == 'paragraph'
    assert not any(internal in html for internal in ('G:C001','G:A001','J12:C001','J00:A001','来源 ID','证据等级'))
    assert json.loads((output / '_html_report_manifest.json').read_text(encoding='utf-8')) == manifest


def test_context_can_remove_all_test_fills(monkeypatch, tmp_path):
    from bounded_investigator.reporting.html_report import create_html_report
    source = input_result(tmp_path)
    install_model(monkeypatch)
    context = tmp_path / 'context.json'
    context.write_text(json.dumps({'meta': {'id':'X', 'title':'实际身份', 'date':'2026-10-23'},
        'hashes':[{'type':'SHA256','value':'a'*64,'source':'文件核验'}], 'attack':[],
        'attribution':None, 'yara':None}), encoding='utf-8')
    output = tmp_path / 'html'
    manifest = asyncio.run(create_html_report(global_result=source, output_dir=output,
                                             config=settings(), context_file=context))
    data = json.loads((output / '_html_report.json').read_text(encoding='utf-8'))
    assert manifest['test_fills'] == []
    assert data['meta']['title'] == '实际身份' and data['meta']['status'] == 'HTML 预览'
    assert data['hashes'][0]['value'] == 'a'*64 and data['attack'] == []
    assert data['attribution'] is None and data['yara'] is None


@pytest.mark.parametrize('problem', ['model', 'reference', 'coverage', 'hashes', 'compile', 'render'])
def test_failure_removes_previous_preview_and_saves_diagnostics(monkeypatch, tmp_path, problem):
    from bounded_investigator.reporting import html_report
    source = input_result(tmp_path)
    composition = fixed_composition()
    if problem == 'reference':
        composition.summary[0].claim_ids = ['G:C999']
    elif problem == 'coverage':
        composition.sections = []
    elif problem == 'compile':
        from bounded_investigator.reporting.composition import TableBlock
        composition.sections[0].blocks = [TableBlock(type='table',
            columns=[{'label': '边界', 'field': 'limitations'}], fact_ids=['G:C001'])]
    install_model(monkeypatch, output=composition, error=ConnectionError('offline fixture') if problem == 'model' else None)
    if problem == 'render':
        monkeypatch.setattr(html_report.HtmlReportSkill, 'render', lambda *_: (_ for _ in ()).throw(ValueError('render fixture')))
    output = tmp_path / 'html'
    output.mkdir()
    for name in ('_final_report.html', '_html_report.json', '_report_document.json'):
        (output / name).write_text('OLD SUCCESS', encoding='utf-8')
    context = None
    if problem == 'hashes':
        context = tmp_path / 'context.json'
        context.write_text('{"hashes":42}', encoding='utf-8')
    with pytest.raises((ValueError, ConnectionError)):
        asyncio.run(html_report.create_html_report(global_result=source, output_dir=output,
                                                   config=settings(), context_file=context))
    assert not (output / '_final_report.html').exists()
    assert not (output / '_html_report.json').exists()
    manifest = json.loads((output / '_html_report_manifest.json').read_text(encoding='utf-8'))
    assert manifest['status'] == 'failed' and manifest['error']['message']
    if problem == 'render':
        assert json.loads((output / '_report_document.json').read_text(encoding='utf-8')) == composition.model_dump(mode='json')
    else:
        assert not (output / '_report_document.json').exists()
    assert list((output / '_html_report_artifacts').glob('*/failure.json'))
    if problem == 'hashes':
        assert list((output / '_html_report_artifacts').glob('*/context_preflight.json'))
        assert manifest['validation']['template_errors']
    else:
        assert list((output / '_html_report_artifacts').glob('*/composition_input.json'))


def test_format_repair_uses_separate_requests(monkeypatch, tmp_path):
    from bounded_investigator.reporting.html_report import create_html_report
    source = input_result(tmp_path)
    # Correct fields with an invalid scalar: repair may fix syntax/type, never invent fields.
    response = fixed_composition().model_dump()
    response['chain'][0]['key'] = 'invalid boolean'
    calls = install_model(monkeypatch, raw=json.dumps(response),
                          error=UnexpectedModelBehavior('structured output validation error'),
                          repaired=fixed_composition().model_dump_json())
    config = settings()
    config.format_repair.enabled = True
    config.format_repair.max_attempts = 2
    manifest = asyncio.run(create_html_report(global_result=source, output_dir=tmp_path / 'html', config=config))
    assert [c[0] for c in calls] == ['compose', 'repair']
    assert calls[1][2].request_limit == 2
    assert manifest['usage']['composition']['requests'] == 1
    assert manifest['usage']['format_repair']['requests'] == 1
    assert manifest['usage']['total']['requests'] == 2


def test_same_input_directory_is_rejected_before_mutation(monkeypatch, tmp_path):
    from bounded_investigator.reporting.html_report import create_html_report
    source = input_result(tmp_path)
    (source.parent / '_final_report.html').write_text('preserve', encoding='utf-8')
    calls = install_model(monkeypatch)
    with pytest.raises(ValueError, match='output|输出'):
        asyncio.run(create_html_report(global_result=source, output_dir=source.parent, config=settings()))
    assert not calls and (source.parent / '_final_report.html').read_text() == 'preserve'


def test_provenance_and_missing_stage_reject_before_model(monkeypatch, tmp_path):
    from bounded_investigator.reporting.html_report import create_html_report
    source = input_result(tmp_path)
    calls = install_model(monkeypatch)
    config = settings()
    del config.stages['report_composition']
    with pytest.raises(ValueError, match='report_composition'):
        asyncio.run(create_html_report(global_result=source, output_dir=tmp_path / 'no_stage', config=config))
    envelope = json.loads(source.read_text(encoding='utf-8'))
    envelope['provenance_valid'] = False
    source.write_text(json.dumps(envelope), encoding='utf-8')
    with pytest.raises(ValueError, match='provenance|来源'):
        asyncio.run(create_html_report(global_result=source, output_dir=tmp_path / 'bad_source', config=settings()))
    assert calls == []


def test_new_cli_forwards_options_without_model(monkeypatch, tmp_path):
    from bounded_investigator import cli
    source = input_result(tmp_path)
    calls = []

    async def capture(**kwargs):
        calls.append(kwargs)
        return {'status':'completed'}

    monkeypatch.setattr(cli, 'create_html_report', capture)
    monkeypatch.setattr(cli, 'configure_logging', lambda _: None)
    output = tmp_path / 'html'
    result = CliRunner().invoke(cli.app, ['report-html', str(source), '--output-dir', str(output),
        '--packets-dir', str(source.parent), '--packet-results-dir', str(source.parent),
        '--config', 'config.example.yaml'])
    assert result.exit_code == 0, result.output
    assert calls[0]['global_result'] == source and calls[0]['output_dir'] == output
    assert calls[0]['packets_dir'] == source.parent
    assert calls[0]['packet_results_dir'] == source.parent


def test_packets_supply_verbatim_code_and_index_without_touching_inputs(monkeypatch, tmp_path):
    from bounded_investigator.reporting.html_report import create_html_report
    source = input_result(tmp_path)
    packets = tmp_path / 'packets'
    packets.mkdir()
    packet = packets / 'J12.md'
    packet.write_text('## F-0483 sub_demo，原文件 L0328–L0329 / 共 329 行\n```c\n'
                      'L0328: decoded = PK11SDR_Decrypt(input);\nL0329: return decoded;\n```\n', encoding='utf-8')
    original = packet.read_bytes()
    calls = install_model(monkeypatch, include_code=True)
    output = tmp_path / 'html'
    result = asyncio.run(create_html_report(global_result=source, output_dir=output,
                                           config=settings(), packets_dir=packets))
    data = json.loads((output / '_html_report.json').read_text(encoding='utf-8'))
    code = [b['text'] for s in data['sections'] for b in s['blocks'] if b['type'] == 'code']
    assert code == ['L0328: decoded = PK11SDR_Decrypt(input);\nL0329: return decoded;']
    assert 'sub_demo' in data['functions'][0]['function']
    assert 'decoded = PK11SDR_Decrypt(input);' in calls[0][1]
    assert result['validation']['source_audit']['evidence_reference_status'] != 'not_run'
    assert packet.read_bytes() == original


def test_incomplete_composition_keeps_raw_diagnostics_and_no_html(monkeypatch, tmp_path):
    from bounded_investigator.reporting.html_report import create_html_report
    source = input_result(tmp_path)
    calls = install_model(monkeypatch, raw='{}',
                          error=UnexpectedModelBehavior('structured output validation error'))
    config = settings()
    config.format_repair.enabled = True
    output = tmp_path / 'html'
    with pytest.raises(ValueError, match='complete result'):
        asyncio.run(create_html_report(global_result=source, output_dir=output, config=config))
    assert len(calls) == 1  # Missing semantic fields cannot be invented by repair.
    assert not (output / '_final_report.html').exists()
    attempt = next((output / '_html_report_artifacts').iterdir())
    assert (attempt / 'report_composition/validation_errors.json').exists()
    assert list((attempt / 'report_composition').glob('*.txt'))


def test_timeout_preserves_failed_attempt(monkeypatch, tmp_path):
    from bounded_investigator.reporting import html_report
    source = input_result(tmp_path)
    install_model(monkeypatch)

    async def too_slow(self, **kwargs):
        await asyncio.sleep(1)

    from bounded_investigator.reporting.agent import StageRunner
    monkeypatch.setattr(StageRunner, 'run', too_slow)
    config = settings()
    config.html_report.wall_clock_seconds = .01
    output = tmp_path / 'html'
    with pytest.raises(TimeoutError):
        asyncio.run(html_report.create_html_report(global_result=source, output_dir=output, config=config))
    manifest = json.loads((output / '_html_report_manifest.json').read_text(encoding='utf-8'))
    assert manifest['status'] == 'failed' and manifest['error']['type'] == 'TimeoutError'
    assert manifest['usage']['total']['requests'] == 0
    assert not (output / '_final_report.html').exists()


def test_failed_publication_removes_json_and_temporary_files(monkeypatch, tmp_path):
    from bounded_investigator.reporting import html_report
    source = input_result(tmp_path)
    install_model(monkeypatch)
    replace = Path.replace

    def fail_html(path, target):
        if target.name == '_final_report.html':
            raise OSError('publication fixture')
        return replace(path, target)

    monkeypatch.setattr(Path, 'replace', fail_html)
    output = tmp_path / 'html'
    with pytest.raises(OSError, match='publication fixture'):
        asyncio.run(html_report.create_html_report(global_result=source, output_dir=output, config=settings()))
    assert not any((output / name).exists() for name in ('_final_report.html', '_html_report.json'))
    assert not list(output.glob('.*.tmp'))
    assert json.loads((output / '_html_report_manifest.json').read_text(encoding='utf-8'))['status'] == 'failed'


@pytest.mark.parametrize('context_data', [{'unsupported':1}, {'meta':{'risk':'critical'}}, {'hashes':42},
                                          {'attack':[{'id':'incomplete'}]}])
def test_invalid_context_is_rejected_without_model(monkeypatch, tmp_path, context_data):
    from bounded_investigator.reporting.html_report import create_html_report
    source = input_result(tmp_path)
    calls = install_model(monkeypatch)
    context = tmp_path / 'context.json'
    context.write_text(json.dumps(context_data), encoding='utf-8')
    with pytest.raises(ValueError):
        asyncio.run(create_html_report(global_result=source, output_dir=tmp_path / 'html',
                                       config=settings(), context_file=context))
    assert calls == []


@pytest.mark.parametrize('stale', [False, True])
def test_default_command_reuses_only_matching_saved_source_excerpts(monkeypatch, tmp_path, stale):
    from bounded_investigator.reporting.html_report import create_html_report
    from bounded_investigator.reporting.knowledge_report import project_knowledge
    source = input_result(tmp_path)
    facts = GlobalReconciliationEnvelope.model_validate_json(source.read_text(encoding='utf-8')).result
    projection, _ = project_knowledge(facts)
    code = 'L0328: decoded = PK11SDR_Decrypt(input);\nL0329: return decoded;'
    projection['code_excerpts'] = {'G:C001': [{
        'function_id':'F-0483', 'evidence_location':'F-0483 L328-L329',
        'code':code, 'start_line':328, 'end_line':329, 'source_packets':['J12.md'],
    }]}
    if stale:
        projection['canonical_claims'][0]['behavior'] = 'stale behavior'
    saved = source.parent / '_report_ready.json'
    saved.write_text(json.dumps(projection), encoding='utf-8')
    before = saved.read_bytes()
    calls = install_model(monkeypatch, include_code=not stale)
    output = tmp_path / 'html'
    result = asyncio.run(create_html_report(global_result=source, output_dir=output, config=settings()))
    data = json.loads((output / '_html_report.json').read_text(encoding='utf-8'))
    blocks = [b['text'] for s in data['sections'] for b in s['blocks'] if b['type'] == 'code']
    if stale:
        assert blocks == [] and result['warnings']
    else:
        assert blocks == [code] and 'decoded = PK11SDR_Decrypt(input);' in calls[0][1]
        assert result['inputs']['saved_report_projection']['sha256']
    assert saved.read_bytes() == before


def test_example_generator_is_offline_and_keeps_all_fixture_behaviors(tmp_path):
    root = Path(__file__).resolve().parents[1]
    source = root / 'examples/html-preview-v11-input'
    originals = {p:p.read_bytes() for p in source.rglob('*') if p.is_file()}
    result = subprocess.run([sys.executable, str(root / 'examples/generate_html_preview.py'),
                             '--output-dir', str(tmp_path / 'html')],
                            cwd=root, capture_output=True, text=True, encoding='utf-8')
    assert result.returncode == 0, result.stdout + result.stderr
    manifest = json.loads((tmp_path / 'html/_html_report_manifest.json').read_text(encoding='utf-8'))
    assert manifest['usage']['total']['requests'] == 0
    assert manifest['model']['model_name'] == 'fixed-response/offline-preview-no-network'
    document = json.loads((tmp_path / 'html/_report_document.json').read_text(encoding='utf-8'))
    assert len(document['sections']) == 2
    assert document['gaps'][0]['claim_ids'] == ['G:C004']
    assert document['findings'][2]['state_note']
    assert any('G:C004' in block.get('claim_ids', [])
               for section in document['sections'] for block in section['blocks'])
    html = (tmp_path / 'html/_final_report.html').read_text(encoding='utf-8')
    assert '可能' in html and '模式' in html
    assert all(path.read_bytes() == original for path, original in originals.items())
