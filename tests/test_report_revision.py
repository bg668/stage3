"""Report semantic revision contracts; all model responses are fixed locally."""

import asyncio
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic_ai.usage import RunUsage

from bounded_investigator.execution.stage_runner import RepairRuntime
from bounded_investigator.reporting import agent, composition
from bounded_investigator.reporting.html_skill import HtmlReportSkill
from bounded_investigator.schemas import GlobalReconciliationResult
from test_html_report import settings, fixed_composition
from factories import report_canonical as canonical


@pytest.fixture(autouse=True)
def no_real_models(monkeypatch):
    import pydantic_ai.models
    monkeypatch.setattr(pydantic_ai.models, 'ALLOW_MODEL_REQUESTS', False)


def frozen_case():
    data = json.loads((Path(__file__).parent / 'fixtures/report_coverage_20261027.json').read_text(encoding='utf-8'))
    raw = data['document']
    raw.update(title='凭据收集与处理样本', findings=[])
    raw['gaps'] = [{'claim_ids': [c['canonical_id'] for c in data['facts']['canonical_claims'] if c['evidence_level'] == 'suspected'],
        'text': '部分凭据处理路径的结果与用途尚未闭合', 'impact': '影响对凭据处理范围的判断', 'blocking': False}]
    return GlobalReconciliationResult.model_validate(data['facts']), composition.ReportComposition.model_validate(raw)


def real_patch(document):
    from bounded_investigator.reporting.revision import ReportRevisionPatch
    section = document.sections[0].model_dump(mode='json')
    section['blocks'].insert(0, {'type': 'paragraph', 'claim_ids': ['G:C036', 'G:C037'],
        'text': 'Credential Manager 路径受版本检查与凭据条目枚举控制；DPAPI 返回缓冲区中的 User: 与 Password: 字段被提取并累加到内部结果，字段追加分支未以解密调用成功为门槛。此路径与 IE Storage2 的字段处理分别存在。'})
    return ReportRevisionPatch.model_validate({'section_replacements': [{'section_index': 0, 'section': section}]})


def install_sequence(monkeypatch, responses, *, tokens=10):
    calls = []
    async def run(prompt, **kwargs):
        limit, usage = kwargs['usage_limits'], kwargs['usage']
        limit.check_before_request(usage)
        calls.append((prompt, kwargs))
        usage.requests += 1
        usage.input_tokens += tokens
        limit.check_tokens(usage)
        output = responses[len(calls)-1]
        if isinstance(output, Exception):
            raise output
        return SimpleNamespace(output=output)
    def build(**kwargs):
        assert kwargs.get('output_retries') == 0
        return SimpleNamespace(stage='report_composition', schema=kwargs['schema'], agent=SimpleNamespace(run=run),
            model_config=SimpleNamespace(stream_output=False, output_mode='tool'))
    monkeypatch.setattr(agent, 'build_stage_agent', build)
    return calls


def compose(reporter, facts, tmp_path, *, usage=None):
    return asyncio.run(reporter.compose(facts=facts, projection={}, packet_name='fixed', artifact_dir=tmp_path,
        usage=usage if usage is not None else RunUsage(), repair_runtime=RepairRuntime()))


def test_collector_returns_all_issues_with_exact_paths():
    facts, doc = frozen_case()
    doc.summary[0].claim_ids = ['G:C999', 'G:C999']
    doc.sections[0].blocks[0].text += ' 来源 J10'
    issues = composition.collect_composition_issues(doc, facts, {})
    codes = {i.code for i in issues}
    assert {'missing_behavior', 'unknown_reference', 'duplicate_reference', 'internal_metadata'} <= codes
    assert any(i.path == '/summary/0/claim_ids' for i in issues if i.code == 'unknown_reference')
    assert any(i.fact_ids == ['G:C037'] for i in issues if i.code == 'missing_behavior')
    assert all(i.message and i.correction for i in issues)


def test_real_omission_is_revised_once_without_mutating_draft(monkeypatch, tmp_path):
    facts, draft = frozen_case()
    before = draft.model_dump(mode='json')
    calls = install_sequence(monkeypatch, [draft, real_patch(draft)])
    reporter = agent.ReportAgent(settings(), HtmlReportSkill())
    usage = RunUsage()
    result = compose(reporter, facts, tmp_path, usage=usage)
    composition.validate_composition(result, facts, {})
    assert len(calls) == usage.requests == 2
    assert draft.model_dump(mode='json') == before
    assert result.sections[1:] == draft.sections[1:] and result.summary == draft.summary
    assert 'Credential Manager' in result.sections[0].blocks[0].text
    feedback = json.loads((tmp_path / 'revision_1/input.json').read_text(encoding='utf-8'))
    assert feedback['issues'][0]['fact_ids'] == ['G:C037']
    assert any(c['canonical_id'] == 'G:C037' for c in feedback['facts']['claims'])
    assert 'historical_gaps' not in feedback
    assert (tmp_path / 'report_composition/validated_result.json').is_file()
    assert (tmp_path / 'revision_1/report_composition/validated_result.json').is_file()
    assert (tmp_path / 'revision_1/candidate.json').is_file()
    assert reporter.revision_status['status'] == 'applied'
    assert reporter.usage_breakdown['draft']['requests'] == reporter.usage_breakdown['revision']['requests'] == 1


@pytest.mark.parametrize('kind', ['id_only', 'drops_original', 'root_edit', 'duplicate', 'out_of_range', 'noop'])
def test_patch_guards_reject_invalid_changes(kind):
    from bounded_investigator.reporting.revision import ReportRevisionPatch, apply_revision_patch
    facts, draft = frozen_case()
    raw = real_patch(draft).model_dump(mode='json', exclude_none=True)
    if kind == 'id_only':
        section = draft.sections[0].model_dump(mode='json')
        section['blocks'][0]['claim_ids'].append('G:C037')
        raw['section_replacements'][0]['section'] = section
    elif kind == 'drops_original':
        raw['section_replacements'][0]['section']['blocks'] = raw['section_replacements'][0]['section']['blocks'][:1]
    elif kind == 'root_edit':
        raw['summary'] = [{'text': '额外改写', 'claim_ids': ['G:C037']}]
    elif kind == 'duplicate':
        raw['section_replacements'] *= 2
    elif kind == 'out_of_range':
        raw['section_replacements'][0]['section_index'] = 999
    else:
        raw = {}
    with pytest.raises(ValueError):
        apply_revision_patch(draft, ReportRevisionPatch.model_validate(raw),
            composition.collect_composition_issues(draft, facts, {}), facts)


def test_revision_disabled_preserves_failure_without_program_completion(monkeypatch, tmp_path):
    facts, draft = frozen_case()
    cfg = settings(); cfg.html_report.max_model_requests = 1
    calls = install_sequence(monkeypatch, [draft])
    reporter = agent.ReportAgent(cfg, HtmlReportSkill())
    with pytest.raises(ValueError, match='遗漏'):
        compose(reporter, facts, tmp_path)
    assert len(calls) == 1 and reporter.revision_status['status'] == 'disabled'
    assert not (tmp_path / 'revision_1').exists()


def test_invalid_second_result_does_not_trigger_a_third_call(monkeypatch, tmp_path):
    from bounded_investigator.reporting.revision import ReportRevisionPatch
    facts, draft = frozen_case()
    patch = real_patch(draft)
    patch.section_replacements[0].section.blocks[0].artifact_ids = ['G:A999']
    calls = install_sequence(monkeypatch, [draft, patch])
    reporter = agent.ReportAgent(settings(), HtmlReportSkill())
    with pytest.raises(ValueError, match='未知'):
        compose(reporter, facts, tmp_path)
    assert len(calls) == 2 and reporter.revision_status['status'] == 'failed'
    assert json.loads((tmp_path / 'validation_final.json').read_text(encoding='utf-8'))['issues']


def test_budget_exhaustion_skips_semantic_revision(monkeypatch, tmp_path):
    facts, draft = frozen_case()
    cfg = settings(); tokens = cfg.html_report.total_tokens_limit
    calls = install_sequence(monkeypatch, [draft], tokens=tokens)
    reporter = agent.ReportAgent(cfg, HtmlReportSkill())
    with pytest.raises(ValueError):
        compose(reporter, facts, tmp_path)
    assert len(calls) == 1 and reporter.revision_status['status'] == 'skipped_budget'


def test_collector_rejects_empty_binding_before_render():
    doc, facts = fixed_composition(), canonical()
    doc.sections[0].blocks = [composition.TableBlock(type='table', columns=[{'label':'限制','field':'limitations'}], fact_ids=['G:C001'])]
    assert any(i.code == 'empty_bound_content' and i.path.startswith('/sections/0/')
        for i in composition.collect_composition_issues(doc, facts, {}))


def test_original_frozen_omission_is_still_rejected_by_independent_validator():
    facts, draft = frozen_case()
    assert len(facts.canonical_claims) == 58
    with pytest.raises(ValueError, match='正文遗漏分析行为: G:C037'):
        composition.validate_composition(draft, facts, {})
    with pytest.raises(ValueError, match='正文遗漏'):
        HtmlReportSkill().compile(draft, facts, projection={})


def test_multiple_omissions_and_root_error_are_fixed_in_one_round(monkeypatch, tmp_path):
    facts, draft = frozen_case()
    for section in draft.sections:
        for block in section.blocks:
            if hasattr(block, 'claim_ids'):
                block.claim_ids = [cid for cid in block.claim_ids if cid != 'G:C036']
    original_summary = deepcopy(draft.summary)
    draft.summary[0].claim_ids = ['G:C999']
    patch = real_patch(draft); patch.summary = original_summary
    calls = install_sequence(monkeypatch, [draft, patch])
    result = compose(agent.ReportAgent(settings(), HtmlReportSkill()), facts, tmp_path)
    composition.validate_composition(result, facts, {})
    issues = json.loads((tmp_path / 'revision_1/input.json').read_text(encoding='utf-8'))['issues']
    assert any(i['fact_ids'] == ['G:C036', 'G:C037'] for i in issues if i['code'] == 'missing_behavior')
    assert any(i['code'] == 'unknown_reference' for i in issues)
    assert len(calls) == 2


def test_new_section_can_present_an_unconfirmed_behavior(monkeypatch, tmp_path):
    from bounded_investigator.reporting.revision import ReportRevisionPatch
    from bounded_investigator.schemas import EvidenceLevel
    facts, draft = frozen_case()
    next(c for c in facts.canonical_claims if c.canonical_id == 'G:C037').evidence_level = EvidenceLevel.SUSPECTED
    patch = ReportRevisionPatch(appended_sections=[{
        'title': '凭据结果的结构化处理', 'blocks': [{'type':'paragraph', 'claim_ids':['G:C037'],
        'text':'版本检查与枚举条件满足后，可能从 Credential Manager 解密返回缓冲区提取 User: 和 Password: 并累加到结果；追加分支未检查解密调用返回值。'}]}])
    calls = install_sequence(monkeypatch, [draft, patch])
    result = compose(agent.ReportAgent(settings(), HtmlReportSkill()), facts, tmp_path)
    assert result.sections[:-1] == draft.sections
    assert '可能' in result.sections[-1].blocks[0].text and len(calls) == 2


def test_root_only_issue_does_not_authorize_chapter_edits():
    from bounded_investigator.reporting.revision import ReportRevisionPatch, apply_revision_patch
    facts, draft = canonical(), fixed_composition()
    original = deepcopy(draft.summary)
    draft.summary[0].claim_ids = ['G:C999']
    issues = composition.collect_composition_issues(draft, facts, {})
    patch = ReportRevisionPatch(summary=original)
    candidate = apply_revision_patch(draft, patch, issues, facts)
    composition.validate_composition(candidate, facts, {})
    assert candidate.sections == draft.sections
    patch.section_replacements = [real_patch(frozen_case()[1]).section_replacements[0]]
    with pytest.raises(ValueError, match='越权'):
        apply_revision_patch(draft, patch, issues, facts)


def test_citation_plus_unrelated_title_change_is_not_a_substantive_fix():
    from bounded_investigator.reporting.revision import ReportRevisionPatch, apply_revision_patch
    facts, draft = frozen_case()
    section = draft.sections[0].model_dump(mode='json')
    section['title'] += '机制'
    section['blocks'][0]['claim_ids'].append('G:C037')
    patch = ReportRevisionPatch(section_replacements=[{'section_index':0, 'section':section}])
    with pytest.raises(ValueError, match='正文'):
        apply_revision_patch(draft, patch, composition.collect_composition_issues(draft, facts, {}), facts)


@pytest.mark.parametrize('failure', ['render', 'second_model', 'second_validation', 'none'])
def test_manifest_and_publication_record_revision_without_touching_analysis(monkeypatch, tmp_path, failure):
    from bounded_investigator.reporting.html_report import create_html_report
    from bounded_investigator.schemas import GlobalReconciliationEnvelope
    facts, draft = frozen_case()
    source = tmp_path / 'analysis'; source.mkdir()
    path = source / '_global_reconciliation.json'
    path.write_text(GlobalReconciliationEnvelope(result=facts, provenance_valid=True, elapsed_seconds=0,
        delivery_status='success', source_claim_count=58, source_artifact_count=len(facts.canonical_artifacts)).model_dump_json(), encoding='utf-8')
    before = path.read_bytes()
    patch = real_patch(draft)
    if failure == 'second_validation':
        patch.section_replacements[0].section.blocks[0].artifact_ids = ['G:A999']
    calls = install_sequence(monkeypatch, [draft, ConnectionError('offline revision failure') if failure == 'second_model' else patch])
    if failure == 'render':
        monkeypatch.setattr(HtmlReportSkill, 'render', lambda *_: (_ for _ in ()).throw(ValueError('offline render failure')))
    output = tmp_path / 'html'; output.mkdir()
    for name in ('_final_report.html','_html_report.json','_report_document.json'):
        (output / name).write_text('OLD', encoding='utf-8')
    if failure == 'none':
        manifest = asyncio.run(create_html_report(global_result=path, output_dir=output, config=settings()))
        assert manifest['status'] == 'completed'
        assert 'User:' in (output / '_final_report.html').read_text(encoding='utf-8')
    else:
        with pytest.raises((ValueError, ConnectionError)):
            asyncio.run(create_html_report(global_result=path, output_dir=output, config=settings()))
        manifest = json.loads((output / '_html_report_manifest.json').read_text(encoding='utf-8'))
        assert manifest['status'] == 'failed'
        assert not (output / '_final_report.html').exists() and not (output / '_html_report.json').exists()
        assert (output / '_report_document.json').exists() == (failure == 'render')
    assert manifest['revision']['status'] == ('applied' if failure in {'none','render'} else 'failed')
    assert manifest['usage']['draft']['requests'] == manifest['usage']['revision']['requests'] == 1
    assert manifest['usage']['composition']['requests'] == manifest['usage']['total']['requests'] == 2
    assert len(calls) == 2 and path.read_bytes() == before


def test_second_call_is_inside_the_same_timeout(monkeypatch, tmp_path):
    from bounded_investigator.execution.stage_runner import StageOutcome
    from bounded_investigator.schemas import DeliveryStatus
    facts, draft = frozen_case()
    install_sequence(monkeypatch, [draft])
    calls = []
    async def run(self, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            await asyncio.sleep(.03)
            return StageOutcome(status=DeliveryStatus.SUCCESS, output=draft)
        await asyncio.sleep(.05)
        return StageOutcome(status=DeliveryStatus.SUCCESS, output=real_patch(draft))
    monkeypatch.setattr(agent.StageRunner, 'run', run)
    cfg = settings(); cfg.html_report.wall_clock_seconds = .06
    reporter = agent.ReportAgent(cfg, HtmlReportSkill())
    with pytest.raises(TimeoutError):
        compose(reporter, facts, tmp_path)
    assert len(calls) == 2 and reporter.revision_status['status'] == 'failed'


@pytest.mark.parametrize('problem,code,path', [
    ('missing_ref','missing_fact_reference','/sections/0/blocks/0'),
    ('field','invalid_fact_field','/sections/0/blocks/1/items/0/value/field'),
    ('excerpt','missing_excerpt','/sections/0/blocks/2/excerpt_index'),
    ('ioc','duplicate_ioc_note','/ioc_notes/1'),
    ('gap','unknown_gap','/gaps/0/gap_id'),
])
def test_collector_locates_supported_issue_types(problem, code, path):
    facts, doc = canonical(), fixed_composition()
    if problem == 'missing_ref':
        doc.sections[0].blocks[0].claim_ids = []
        doc.sections[0].blocks[0].artifact_ids = []
    elif problem == 'field':
        doc.sections[0].blocks[1] = composition.KeyValuesBlock(type='kv', items=[{'label':'对象',
            'value':{'fact_id':'G:A001','field':'behavior'}}])
    elif problem == 'excerpt':
        doc.sections[0].blocks = doc.sections[0].blocks[:2]
        doc.sections[0].blocks.append(composition.CodeBlock(type='code', claim_id='G:C001'))
    elif problem == 'ioc':
        doc.ioc_notes = [composition.IOCNote(artifact_id='G:A001', text='检索线索')] * 2
    else:
        doc.gaps = [composition.SelectedGap(gap_id='R:G999',text='关系未知',impact='影响判断',blocking=False)]
    assert any(i.code == code and i.path == path for i in composition.collect_composition_issues(doc, facts, {}))


def install_format_sequence(monkeypatch, draft, patch):
    from pydantic_ai import PartStartEvent, UnexpectedModelBehavior
    from pydantic_ai.messages import TextPart
    calls = []
    raw_draft = draft.model_dump(mode='json'); raw_draft['risk'] = 9
    raw_patch = patch.model_dump(mode='json'); raw_patch['section_replacements'][0]['section_index'] = 'BAD'
    async def run(prompt, **kwargs):
        kwargs['usage_limits'].check_before_request(kwargs['usage'])
        index = sum(c == 'semantic' for c in calls)
        calls.append('semantic'); kwargs['usage'].requests += 1
        async def events():
            yield PartStartEvent(index=0, part=TextPart(content=json.dumps(raw_draft if index == 0 else raw_patch)))
        await kwargs['event_stream_handler'](None, events())
        raise UnexpectedModelBehavior('structured output validation error')
    async def repair(prompt, **kwargs):
        kwargs['usage_limits'].check_before_request(kwargs['usage'])
        index = sum(c == 'format' for c in calls)
        calls.append('format'); kwargs['usage'].requests += 1
        return SimpleNamespace(output=(draft if index == 0 else patch).model_dump_json())
    monkeypatch.setattr(agent, 'build_stage_agent', lambda **kw: SimpleNamespace(
        stage='report_composition', schema=kw['schema'], agent=SimpleNamespace(run=run),
        model_config=SimpleNamespace(stream_output=False,output_mode='tool')))
    monkeypatch.setattr(agent, 'build_text_stage_agent', lambda **kw: SimpleNamespace(
        stage='format_repair', agent=SimpleNamespace(run=repair), model_config=SimpleNamespace(stream_output=False)))
    return calls


@pytest.mark.parametrize('repair_budget', [1,2])
def test_format_repair_budget_is_shared_across_semantic_calls(monkeypatch, tmp_path, repair_budget):
    from pydantic_ai.exceptions import UsageLimitExceeded
    facts, draft = frozen_case()
    calls = install_format_sequence(monkeypatch, draft, real_patch(draft))
    cfg = settings(); cfg.format_repair.enabled = True; cfg.format_repair.max_attempts = repair_budget
    reporter = agent.ReportAgent(cfg, HtmlReportSkill())
    usage, runtime = RunUsage(), RepairRuntime()
    task = reporter.compose(facts=facts, projection={}, packet_name='fixed', artifact_dir=tmp_path,
        usage=usage, repair_runtime=runtime)
    if repair_budget == 1:
        with pytest.raises(UsageLimitExceeded):
            asyncio.run(task)
        assert reporter.revision_status['status'] == 'failed'
    else:
        composition.validate_composition(asyncio.run(task), facts, {})
        assert reporter.revision_status['status'] == 'applied'
        assert (tmp_path / 'format_repair/report_composition_attempt_1.txt').is_file()
        assert (tmp_path / 'revision_1/format_repair/report_composition_attempt_1.txt').is_file()
    assert usage.requests == 2 and runtime.usage.requests == repair_budget
    assert runtime.attempts == repair_budget
    assert calls.count('semantic') == 2 and calls.count('format') == repair_budget


@pytest.mark.parametrize('reference_problem', ['artifact', 'claim', 'duplicate', 'missing'])
def test_legitimate_reference_repairs_do_not_require_rewriting_correct_prose(reference_problem):
    from bounded_investigator.reporting.revision import ReportRevisionPatch, apply_revision_patch
    facts, draft = canonical(), fixed_composition()
    block = draft.sections[0].blocks[0]
    if reference_problem == 'artifact':
        block.artifact_ids = ['G:A999']
    elif reference_problem == 'claim':
        block.claim_ids = ['G:C999']
    elif reference_problem == 'duplicate':
        block.claim_ids *= 2
    else:
        block.claim_ids, block.artifact_ids = [], []
    section = draft.sections[0].model_dump(mode='json')
    section['blocks'][0]['claim_ids'] = ['G:C001']
    if reference_problem == 'artifact':
        section['blocks'][0]['artifact_ids'] = []
    patch = ReportRevisionPatch(section_replacements=[{'section_index':0,'section':section}])
    result = apply_revision_patch(draft, patch, composition.collect_composition_issues(draft, facts, {}), facts)
    composition.validate_composition(result, facts, {})
    assert result.sections[0].blocks[0].text == block.text


def test_appended_chapter_cannot_copy_existing_prose_and_only_attach_missing_id():
    from bounded_investigator.reporting.revision import ReportRevisionPatch, apply_revision_patch
    facts, draft = frozen_case()
    block = draft.sections[0].blocks[0].model_dump(mode='json'); block['claim_ids'].append('G:C037')
    patch = ReportRevisionPatch(appended_sections=[{'title':'新机制', 'blocks':[block]}])
    with pytest.raises(ValueError, match='正文'):
        apply_revision_patch(draft, patch, composition.collect_composition_issues(draft, facts, {}), facts)


def test_report_sdk_retry_override_is_independent_of_workflow(monkeypatch):
    from bounded_investigator.execution import llm
    cfg = settings(); cfg.workflow.output_retries = 3
    captured = []
    monkeypatch.setattr(llm, 'build_model', lambda *args: object())
    monkeypatch.setattr(llm, 'Agent', lambda **kwargs: captured.append(kwargs) or object())
    llm.build_stage_agent(config=cfg, stage='report_composition', schema=composition.ReportComposition,
        instructions='报告 JSON', output_retries=0)
    assert captured[0]['retries'] == {'output': 0}


@pytest.mark.parametrize('placement', ['replacement', 'appended'])
def test_style_or_component_change_cannot_count_as_new_behavior_content(placement):
    from bounded_investigator.reporting.revision import ReportRevisionPatch, apply_revision_patch
    facts, draft = frozen_case()
    block = draft.sections[0].blocks[0].model_dump(mode='json')
    block.update(type='callout',tone='risk',label='关键发现')
    block['claim_ids'].append('G:C037')
    section = draft.sections[0].model_dump(mode='json')
    section['blocks'][0] = block
    patch = ReportRevisionPatch.model_validate({'section_replacements':[{'section_index':0,'section':section}]}
        if placement == 'replacement' else {'appended_sections':[{'title':'新机制','blocks':[block]}]})
    with pytest.raises(ValueError, match='正文'):
        apply_revision_patch(draft, patch, composition.collect_composition_issues(draft, facts, {}), facts)
