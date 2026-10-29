"""Report 1.1 binds tiers, evidence states and audit to existing facts."""

from copy import deepcopy
import json

import pytest

from bounded_investigator.reporting.composition import ReportComposition, collect_composition_issues
from bounded_investigator.reporting.html_skill import HtmlReportSkill, prepare_context
from bounded_investigator.reporting.knowledge_report import project_knowledge
from bounded_investigator.schemas import DetectionSpecificity, EvidenceLevel
from test_html_composition import composition_dict, facts, report_projection


def document():
    raw = composition_dict()
    raw.update(title='含配置解码与文件写出的样本', findings=[{
        'rule': 'CONFIG-DECODE', 'title': '解码并写出配置',
        'claim_ids': ['G:C001'], 'section_index': 0,
    }])
    raw['chain'][0]['brief'] = '写出解码配置'
    return raw


def compile_report(raw=None, canonical=None, **kwargs):
    canonical = canonical or facts()
    return HtmlReportSkill().compile(ReportComposition.model_validate(raw or document()), canonical,
        projection=report_projection(canonical), **kwargs)


def test_missing_context_has_no_synthetic_facts_and_no_hash_is_renderable():
    context, fills = prepare_context(None)
    assert fills == []
    assert context['hashes'] == [] and context['attack'] == []
    assert context['attribution'] is None and context['yara'] is None
    data, fills = compile_report()
    assert fills == [] and data['schema_version'] == '1.1'
    assert data['meta']['title'] == document()['title']
    assert data['meta']['id'] == 'HTML-REPORT' and data['meta']['method']
    html = HtmlReportSkill().render(data)
    assert '未提供样本哈希' in html and '示例家族' not in html
    assert '含测试数据' not in html and 'T0000' not in html


def test_findings_bind_state_evidence_and_section_without_changing_facts():
    canonical = facts()
    before = canonical.model_dump()
    data, _ = compile_report(canonical=canonical)
    assert data['findings'] == [{'rule': 'CONFIG-DECODE', 'title': '解码并写出配置',
        'state': 'confirmed', 'evidence': ['F-0012 L10-L12'], 'section': 'mechanism-1'}]
    assert data['chain'][0]['brief'] == '写出解码配置'
    assert canonical.model_dump() == before
    assert not any('证据引用格式' in w for w in HtmlReportSkill().validate(data)[1])
    html = HtmlReportSkill().render(data)
    assert '关键发现' in html and 'href="#mechanism-1"' in html
    assert 'G:C001' not in html


def test_partial_finding_requires_note_and_a_gap_linked_to_the_suspected_fact():
    canonical = facts()
    canonical.canonical_claims[0].evidence_level = EvidenceLevel.SUSPECTED
    raw = document()
    issues = collect_composition_issues(ReportComposition.model_validate(raw), canonical)
    assert {'missing_state_note', 'missing_finding_gap'} <= {i.code for i in issues}
    raw['findings'][0]['state_note'] = '配置键表尚未恢复'
    raw['gaps'] = [{'claim_ids': ['G:C001'], 'text': '配置键表尚未恢复',
        'impact': '影响对后续功能范围的判断', 'blocking': False}]
    data, _ = compile_report(raw, canonical)
    assert data['findings'][0]['state'] == 'partial'
    assert data['findings'][0]['state_note'] == raw['findings'][0]['state_note']
    assert HtmlReportSkill().validate(data)[0] == []


@pytest.mark.parametrize('level', [EvidenceLevel.LEAD, EvidenceLevel.UNCLASSIFIED])
def test_leads_belong_in_gaps_and_cannot_be_promoted_to_findings(level):
    canonical = facts()
    canonical.canonical_claims[0].evidence_level = level
    raw = document()
    codes = {i.code for i in collect_composition_issues(ReportComposition.model_validate(raw), canonical)}
    assert {'ineligible_finding', 'missing_lead_gap'} <= codes
    raw.update(findings=[], sections=[], chain=[], gaps=[{
        'claim_ids': ['G:C001'], 'text': '配置写出行为的证据状态待核查',
        'impact': '当前不能作为已证实能力', 'blocking': False}])
    data, _ = compile_report(raw, canonical)
    assert data['findings'] == [] and data['gaps']


@pytest.mark.parametrize('change,code', [
    ({'rule': 'MISSING'}, 'unknown_finding_rule'),
    ({'tier': 'malicious'}, 'finding_rule_override'),
    ({'section_index': 99}, 'unknown_finding_section'),
    ({'claim_ids': ['G:C999']}, 'unknown_reference'),
    ({'rule': 'CUSTOM'}, 'invalid_custom_finding'),
])
def test_invalid_finding_contracts_are_reported_for_scoped_revision(change, code):
    raw = document()
    raw['findings'][0].update(change)
    issues = collect_composition_issues(ReportComposition.model_validate(raw), facts())
    assert code in {i.code for i in issues}
    assert any(i.path.startswith('/findings') for i in issues if i.code == code)


def test_custom_tier_requires_reason_and_emits_review_warning():
    raw = document()
    raw['findings'][0].update(rule='CUSTOM', tier='info', category='config', tier_reason='配置处理辅助行为')
    data, _ = compile_report(raw)
    errors, warnings = HtmlReportSkill().validate(data)
    assert errors == [] and any('人工复核' in w for w in warnings)
    assert data['findings'][0]['tier'] == 'info'


def test_ioc_table_includes_three_projection_groups_and_deduplicates_values():
    canonical = facts()
    original = canonical.canonical_artifacts[0]
    for index, role, specificity, value in [
        (2, 'read_source', 'medium', 'Software\\Vendor'),
        (3, 'system_path', 'none', 'C:\\Windows'),
        (4, 'read_source', 'medium', original.value.upper()),
    ]:
        canonical.canonical_artifacts.append(original.model_copy(deep=True, update={
            'canonical_id': f'G:A{index:03}', 'source_artifact_ids': [f'J00:A{index:03}'],
            'role': role, 'detection_specificity': DetectionSpecificity(specificity), 'value': value,
        }))
    before = canonical.model_dump()
    data, _ = compile_report(canonical=canonical)
    items = data['ioc']['host']
    assert [item['specificity'] for item in items] == ['high', 'medium', 'context']
    assert items[0]['value'] == original.value and '输出落地' in items[0]['role']
    assert '读取来源' in items[0]['role']
    assert all(item['evidence'] for item in items)
    assert canonical.model_dump() == before
    assert HtmlReportSkill().validate(data)[0] == []


@pytest.mark.parametrize('audit_status,passed,projection_status,expected', [
    ('completed', True, 'complete', 'passed'),
    ('completed', False, 'incomplete_provenance', 'incomplete'),
    ('not_run', True, 'complete', 'not_run'),
])
def test_audit_badge_uses_real_execution_and_counts(audit_status, passed, projection_status, expected):
    canonical = facts()
    _, audit = project_knowledge(canonical)
    projection = report_projection(canonical)
    projection['status'] = projection_status
    audit.update(passed=passed, evidence_reference_audit={
        'status': audit_status, 'total_segments': 9, 'invalid_segments': 0 if passed else 2})
    data, _ = HtmlReportSkill().compile(ReportComposition.model_validate(document()), canonical,
        projection=projection, audit=audit)
    assert data['meta']['audit']['status'] == expected
    if audit_status != 'not_run':
        assert data['meta']['audit']['refs_total'] == 9
        assert data['meta']['audit']['refs_invalid'] == (0 if passed else 2)
    else:
        assert 'refs_total' not in data['meta']['audit']


def test_context_can_supply_sample_but_cannot_override_audit():
    data, _ = compile_report(context={'meta': {'sample': {'filename': 'sample.exe', 'arch': 'x64'}}})
    assert data['meta']['sample'] == {'filename': 'sample.exe', 'arch': 'x64'}
    assert data['meta']['audit'] == {'status': 'not_run'}
    with pytest.raises(ValueError, match='audit'):
        prepare_context({'meta': {'audit': {'status': 'passed'}}})


def test_new_resources_are_in_frozen_instructions_and_template_fingerprint():
    skill = HtmlReportSkill()
    snapshot = skill.snapshot()
    assert snapshot['version'] == '1.2' and snapshot['template_version'] == '1.1'
    assert 'assets/tier_rules.json' in snapshot['template_resources']
    assert 'skills/malware-report-html/references/modules.md' in snapshot['files']
    assert 'CRED-DECRYPT' in skill.instructions() and 'section_index' in skill.instructions()


def test_title_and_findings_are_required_in_the_model_contract():
    raw = document()
    for field in ('title', 'findings'):
        candidate = deepcopy(raw)
        candidate.pop(field)
        with pytest.raises(ValueError, match=field):
            ReportComposition.model_validate(candidate)


def test_repeated_unknowns_are_a_gap_issue_before_semantic_revision():
    raw = document()
    raw['gaps'] = []
    raw['summary'][0]['text'] = '配置键未知，提交内容未恢复，接收用途尚未确认。'
    issues = collect_composition_issues(ReportComposition.model_validate(raw), facts())
    assert any(i.code == 'missing_prose_gap' and i.path == '/gaps' and i.fact_ids == ['G:C001'] for i in issues)


def test_literal_code_is_not_counted_as_a_report_uncertainty():
    canonical = facts()
    raw = document()
    raw['gaps'] = []
    projection = report_projection(canonical)
    projection['code_excerpts']['G:C001'][0]['code'] = '// 未知 未知 未知，样本中的原始注释\nL0010: decode();'
    data, _ = HtmlReportSkill().compile(ReportComposition.model_validate(raw), canonical, projection=projection)
    assert HtmlReportSkill().validate(data)[0] == []


def test_explicit_empty_analysis_method_is_invalid_context():
    data, _ = compile_report(context={'meta': {'method': ''}})
    assert any('meta.method' in error for error in HtmlReportSkill().validate(data)[0])


@pytest.mark.parametrize('status', ['unknown', None, 'pending'])
def test_unrecognized_audit_execution_status_never_displays_passed(status):
    data, _ = compile_report(audit={'passed': True, 'evidence_reference_audit': {'status': status}})
    assert data['meta']['audit'] == {'status': 'not_run'}


def test_real_reference_audit_without_status_field_is_recognized():
    data, _ = compile_report(audit={'passed': True, 'evidence_reference_audit': {
        'schema': 'evidence_reference_validation_v1', 'scope': 'full_material',
        'passed': True, 'total_segments': 2, 'invalid_segments': 0}})
    assert data['meta']['audit'] == {'status': 'passed', 'refs_total': 2, 'refs_invalid': 0}


@pytest.mark.parametrize('method', [123, [], False])
def test_nontext_analysis_method_is_rejected(method):
    data, _ = compile_report(context={'meta': {'method': method}})
    assert any('meta.method' in e for e in HtmlReportSkill().validate(data)[0])


@pytest.mark.parametrize('location', ['ioc_note', 'table_note', 'cards_title', 'steps_title', 'kv_label', 'code_caption'])
def test_all_rendered_prose_positions_trigger_gap_revision(location):
    raw = document()
    raw['gaps'] = []
    unknown = '用途未知，配置未恢复，接收地址尚未解析。'
    if location == 'ioc_note':
        raw['ioc_notes'][0]['text'] = unknown
    elif location == 'table_note':
        raw['sections'][0]['blocks'][2]['note'] = unknown
    elif location == 'kv_label':
        raw['sections'][0]['blocks'][1]['items'][0]['label'] = unknown
    elif location == 'code_caption':
        raw['sections'][0]['blocks'][3]['caption'] = unknown
    else:
        raw['sections'][0]['blocks'].append({'type': location.split('_')[0],
            'items': [{'title': unknown, 'content': {'text': '处理已有配置', 'claim_ids': ['G:C001']}}]})
    issues = collect_composition_issues(ReportComposition.model_validate(raw), facts())
    assert any(i.code == 'missing_prose_gap' and i.path == '/gaps' for i in issues)


@pytest.mark.parametrize('problem', ['rule', 'gap', 'title'])
def test_new_contract_issues_are_repaired_in_the_existing_single_revision(monkeypatch, tmp_path, problem):
    from test_report_revision import compose, install_sequence
    from test_html_report import settings
    from bounded_investigator.reporting.agent import ReportAgent
    from bounded_investigator.reporting.revision import ReportRevisionPatch
    from bounded_investigator.schemas import GlobalReconciliationResult
    canonical = facts()
    raw = document()
    # Revision tests do not select source excerpts.
    raw['sections'][0]['blocks'] = raw['sections'][0]['blocks'][:1]
    if problem == 'rule':
        valid = deepcopy(raw['findings'])
        raw['findings'][0]['rule'] = 'MISSING'
        patch = ReportRevisionPatch.model_validate({'findings': valid})
    elif problem == 'gap':
        canonical.canonical_claims[0].evidence_level = EvidenceLevel.SUSPECTED
        raw['findings'][0]['state_note'] = '配置键表尚未恢复'
        raw['gaps'] = []
        patch = ReportRevisionPatch.model_validate({'gaps': [{'claim_ids': ['G:C001'],
            'text': '配置键表尚未恢复', 'impact': '影响后续能力范围判断', 'blocking': False}]})
    else:
        raw['title'] = 'G:C001 的配置处理'
        patch = ReportRevisionPatch(title='含配置解码与文件写出的样本')
    draft = ReportComposition.model_validate(raw)
    before = draft.model_dump()
    calls = install_sequence(monkeypatch, [draft, patch])
    reporter = ReportAgent(settings(), HtmlReportSkill())
    result = compose(reporter, canonical, tmp_path)
    assert len(calls) == 2 and reporter.revision_status['status'] == 'applied'
    assert draft.model_dump() == before and result.sections == draft.sections
    assert collect_composition_issues(result, canonical) == []
