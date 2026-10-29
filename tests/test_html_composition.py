"""The HTML skill binds chosen display blocks to unchanged analysis facts."""

from copy import deepcopy
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from bounded_investigator.evidence.inventory import CodeSection, InputInventory
from bounded_investigator.reporting.composition import (
    ReportComposition, build_composition_input, validate_composition,
)
from bounded_investigator.reporting.html_skill import HtmlReportSkill, prepare_context
from bounded_investigator.reporting.knowledge_report import project_knowledge
from bounded_investigator.schemas import GlobalReconciliationResult


def facts():
    return GlobalReconciliationResult.model_validate({
        'status': 'complete', 'summary': '程序归并统计，不是报告摘要',
        'canonical_claims': [{
            'canonical_id': 'G:C001', 'behavior': '写出配置', 'condition': '配置解码成功',
            'source': '资源数据', 'action': 'WriteFile', 'sink': 'C:\\demo.cfg',
            'evidence': [{'location': 'F-0012 L10-L12', 'observation': '原始观察 <>&'}],
            'confidence': 'high', 'evidence_level': 'confirmed', 'limitations': [],
            'source_claim_ids': ['J00:C001'], 'resolution': 'unchanged', 'resolution_reason': '保留',
        }],
        'canonical_artifacts': [{
            'canonical_id': 'G:A001', 'kind': 'file', 'value': 'C:\\demo.cfg',
            'role': 'created_output', 'detection_specificity': 'high', 'meaning': '写出的配置',
            'evidence': [{'location': 'F-0012 L12', 'observation': '输出配置'}],
            'confidence': 'high', 'source_artifact_ids': ['J00:A001'],
        }],
        'gaps': [{'question': '配置键值未解析', 'reason': '键表不在当前材料中', 'impact': 'major'}],
    })


def composition_dict():
    cited = {'text': '配置解码成功后写出配置，供后续逻辑读取。', 'claim_ids': ['G:C001']}
    return {
        'title': '含配置解码与文件写出的样本',
        'findings': [{'rule': 'CONFIG-DECODE', 'title': '解码并写出配置', 'claim_ids': ['G:C001']}],
        'summary': [deepcopy(cited)], 'verdict': deepcopy(cited), 'risk': 'medium',
        'risk_judgement': {'text': '写出配置影响后续运行，应核对落地路径', 'artifact_ids': ['G:A001']},
        'chain': [{'name': '配置写出', 'action': '写出解码后的配置', 'claim_ids': ['G:C001'], 'key': False}],
        'sections': [{'title': '配置如何控制后续运行', 'question': '配置在什么条件下写出？', 'blocks': [
            {'type': 'paragraph', **deepcopy(cited)},
            {'type': 'kv', 'mono': True, 'items': [{'label': '配置路径', 'value': {'fact_id': 'G:A001', 'field': 'value'}}]},
            {'type': 'table', 'columns': [
                {'label': '行为', 'field': 'behavior'}, {'label': '条件', 'field': 'condition'},
                {'label': '来源', 'field': 'source'}, {'label': '动作', 'field': 'action'},
                {'label': '去向', 'field': 'sink'}, {'label': '依据', 'field': 'evidence'},
            ], 'fact_ids': ['G:C001']},
            {'type': 'code', 'claim_id': 'G:C001', 'excerpt_index': 0, 'caption': '配置写入分支'},
        ]}],
        'detection': [{'title': '关联筛查', 'text': '关联资源解码与配置写出行为', 'claim_ids': ['G:C001']}],
        'response': [{'title': '核查', 'text': '保全落地配置并核对写入进程', 'artifact_ids': ['G:A001']}],
        'ioc_notes': [{'artifact_id': 'G:A001', 'text': '配置落地关联对象'}],
        'gaps': [{'gap_id': 'R:G001', 'text': '配置项用途尚未明确', 'impact': '影响键值含义解释', 'blocking': False}],
        'final': deepcopy(cited),
    }


def report_projection(canonical):
    report, _ = project_knowledge(canonical)
    report['code_excerpts'] = {'G:C001': [{
        'function_id': 'F-0012', 'evidence_location': 'F-0012 L10-L12', 'start_line': 10,
        'end_line': 12, 'source_packets': ['J00.md'],
        'code': 'L0010: if (decoded) {\nL0011:   WriteFile(h, data, n, &n, 0);\nL0012: }',
    }]}
    return report


def compile_data(raw=None, canonical=None, projection=None, **kwargs):
    canonical = canonical or facts()
    return HtmlReportSkill().compile(
        ReportComposition.model_validate(raw or composition_dict()), canonical,
        projection=projection or report_projection(canonical), **kwargs,
    )


def test_compilation_copies_selected_facts_snippets_and_ioc_without_mutating_inputs():
    canonical = facts()
    before = canonical.model_dump()
    report = report_projection(canonical)
    source_report = deepcopy(report)
    data, fills = compile_data(canonical=canonical, projection=report)
    assert canonical.model_dump() == before and report == source_report
    assert data['meta']['risk'] == 'medium'
    assert data['meta']['edition'] == 'HTML 预览'
    assert data['ioc']['host'][0]['value'] == canonical.canonical_artifacts[0].value
    assert data['ioc']['host'][0]['note'] == '配置落地关联对象'
    assert 'F-0012' not in data['ioc']['host'][0]['note']
    assert data['ioc']['network'] == []
    assert data['chain'][0]['evidence'] == ['F-0012 L10-L12']
    blocks = data['sections'][0]['blocks']
    table = next(b for b in blocks if b['type'] == 'table')
    assert table['rows'][0][:5] == ['写出配置', '配置解码成功', '资源数据', 'WriteFile', 'C:\\demo.cfg']
    assert '原始观察 <>&' in table['rows'][0][5]
    assert next(b for b in blocks if b['type'] == 'code')['text'] == report['code_excerpts']['G:C001'][0]['code']
    assert '配置写入分支' in blocks[-2]['text'] and 'F-0012 L10-L12' in blocks[-2]['text']
    encoded = json.dumps(data, ensure_ascii=False)
    for internal in ('G:C001', 'G:A001', 'J00:C001', 'J00:A001', 'evidence_level', 'source_claim_ids'):
        assert internal not in encoded
    assert data['gaps'] == [{'gap': '配置项用途尚未明确', 'impact': '影响键值含义解释', 'blocking': False}]
    assert fills == [] and data['hashes'] == []


def test_selected_blocks_do_not_gain_automatic_tables_code_or_limitations():
    canonical = facts()
    canonical.canonical_claims[0].limitations = ['配置键表未恢复']
    raw = composition_dict()
    raw['sections'][0]['blocks'] = [raw['sections'][0]['blocks'][0]]
    data, _ = compile_data(raw, canonical)
    assert data['sections'][0]['blocks'] == [{'type': 'paragraph', 'text': raw['sections'][0]['blocks'][0]['text']}]
    assert data['functions'] == []


def test_all_display_components_compile_in_order_using_requested_fact_fields():
    raw = composition_dict()
    cited = {'text': '配置路径可用于关联落地行为', 'artifact_ids': ['G:A001']}
    raw['sections'][0]['blocks'] += [
        {'type': 'subheading', **cited},
        {'type': 'callout', 'tone': 'boundary', 'label': '待核实', **cited},
        {'type': 'list', 'items': [{'fact_id': 'G:A001', 'field': 'value'}, cited]},
        {'type': 'tags', 'label': '关联对象', 'items': [{'fact_id': 'G:A001', 'field': 'value'}]},
        {'type': 'cards', 'items': [{'title': '落地配置', 'content': cited}]},
        {'type': 'steps', 'items': [{'title': '核对配置', 'content': {'fact_id': 'G:C001', 'field': 'condition'}}]},
    ]
    data, _ = compile_data(raw)
    blocks = data['sections'][0]['blocks']
    assert [b['type'] for b in blocks] == ['paragraph', 'kv', 'table', 'subheading', 'code', 'subheading', 'callout', 'list', 'tags', 'cards', 'steps']
    assert blocks[1]['items'] == [{'k': '配置路径', 'v': 'C:\\demo.cfg'}]
    assert blocks[-1]['items'] == [{'title': '核对配置', 'text': '配置解码成功'}]
    assert blocks[-2]['items'][0]['text'] == cited['text']
    assert HtmlReportSkill().validate(data)[0] == []


def test_selected_limitations_keep_specific_clauses_and_leave_source_unchanged():
    canonical = facts()
    original = ['静态分析无法证明运行时成功', '仅静态分析，未执行样本。', '未进行动态验证',
                '配置键表未恢复，影响字段含义解释', '未执行样本，配置分支中仍有未解析的键值',
                '目标进程句柄未解析，无法确认运行时一定成功。']
    canonical.canonical_claims[0].limitations = original.copy()
    raw = composition_dict()
    raw['sections'][0]['blocks'].append({'type': 'list', 'items': [{'fact_id': 'G:C001', 'field': 'limitations'}]})
    data, _ = compile_data(raw, canonical)
    assert data['sections'][0]['blocks'][-1]['items'] == ['；'.join(original[3:])]
    assert canonical.canonical_claims[0].limitations == original


def test_real_context_and_explicit_empty_values_do_not_get_test_fills():
    context = {'meta': {'id': 'sample-1', 'title': '配置样本', 'date': '2026-10-23'},
               'hashes': [{'type': 'SHA256', 'value': 'a' * 64, 'source': '样本文件'}],
               'attack': [], 'attribution': None, 'yara': None}
    data, fills = compile_data(context=context)
    assert fills == [] and data['meta']['edition'] == 'HTML 预览'
    assert data['meta']['id'] == 'sample-1' and data['hashes'] == context['hashes']
    assert data['attack'] == [] and data['attribution'] is None and data['yara'] is None
    assert data['attribution_empty_reason']


@pytest.mark.parametrize('field,value', [('hashes', []), ('hashes', None), ('attack', None), ('attribution', None), ('yara', None)])
def test_explicit_empty_context_preserved(field, value):
    data, fills = compile_data(context={field: value})
    assert data[field] == ([] if field == 'hashes' and value is None else value)
    assert not any(f['path'] == field or f['path'].startswith(field + '[') or f['path'].startswith(field + '.') for f in fills)


@pytest.mark.parametrize('change,match', [
    (lambda d: d['summary'][0].update(claim_ids=['G:C404']), 'G:C404'),
    (lambda d: d['response'][0].update(artifact_ids=['G:A404']), 'G:A404'),
    (lambda d: d['summary'][0].update(claim_ids=[]), '引用'),
    (lambda d: d['chain'][0].update(claim_ids=[]), '引用'),
    (lambda d: d.update(sections=[]), 'G:C001'),
    (lambda d: d['gaps'][0].update(gap_id='R:G404'), 'R:G404'),
    (lambda d: d['sections'][0]['blocks'][3].update(excerpt_index=99), '99'),
])
def test_invalid_references_missing_behavior_and_bad_excerpt_rejected(change, match):
    raw = composition_dict()
    change(raw)
    with pytest.raises(ValueError, match=match):
        compile_data(raw)


@pytest.mark.parametrize('field', ['evidence_level', 'confidence', 'canonical_id', 'source_claim_ids', 'source_artifact_ids'])
def test_internal_management_fields_are_not_selectable(field):
    raw = composition_dict()
    raw['sections'][0]['blocks'][2]['columns'][0]['field'] = field
    with pytest.raises((ValueError, ValidationError)):
        compile_data(raw)


def test_incompatible_fact_fields_and_unknown_components_are_rejected():
    raw = composition_dict()
    raw['sections'][0]['blocks'][1]['items'][0]['value'] = {'fact_id': 'G:C001', 'field': 'value'}
    with pytest.raises(ValueError, match='value'):
        compile_data(raw)
    raw['sections'][0]['blocks'][1] = {'type': 'invented', 'text': 'not supported'}
    with pytest.raises(ValidationError):
        ReportComposition.model_validate(raw)


def test_unrecognized_model_fields_and_context_fact_overrides_rejected():
    raw = composition_dict()
    raw['ioc'] = {'network': ['invented.example']}
    with pytest.raises(ValidationError):
        ReportComposition.model_validate(raw)
    with pytest.raises(ValueError, match='canonical_claims'):
        compile_data(context={'canonical_claims': []})
    with pytest.raises(ValueError, match='risk'):
        compile_data(context={'meta': {'risk': 'critical'}})


def test_input_has_all_behaviors_and_selectable_excerpts_without_dag():
    canonical = facts()
    data = build_composition_input(canonical, report_projection(canonical))
    assert data['claims'][0]['canonical_id'] == 'G:C001'
    assert data['historical_gaps'][0]['gap_id'] == 'R:G001'
    assert data['all_behavior_ids'] == ['G:C001']
    assert data['code_excerpts']['G:C001'][0]['excerpt_index'] == 0
    assert 'summary' not in data and 'dag' not in data and 'core_claim_ids' not in data


def test_function_index_only_covers_selected_code_and_inventory_symbols():
    canonical = facts()
    canonical.canonical_artifacts[0].evidence[0].location = 'F-0020 L5-L7'
    inventory = InputInventory(code_sections=[CodeSection(function_id='F-0012', symbol='write_config', packet_id='J00', start_line=1, end_line=20, total_lines=20, source_path='J00.md')])
    data, _ = compile_data(canonical=canonical, inventory=inventory)
    assert data['functions'] == [{'function': 'F-0012 write_config', 'role': '配置写入分支', 'lines': 'L0010-L0012'}]
    assert all('address' not in row for row in data['functions'])
    assert not any('F-0020' in row['function'] for row in data['functions'])


def test_empty_chain_is_preserved_and_no_historical_gaps_added():
    raw = composition_dict()
    raw['chain'] = []
    raw['gaps'] = []
    data, _ = compile_data(raw)
    assert data['chain'] == [] and data['gaps'] == []


def test_non_ioc_artifacts_are_not_promoted_by_compilation():
    canonical = facts()
    canonical.canonical_artifacts[0].role = 'read_source'
    data, _ = compile_data(canonical=canonical)
    assert data['ioc']['host'][0]['specificity'] == 'medium'
    assert data['ioc']['network'] == []


def test_original_value_that_resembles_internal_id_is_preserved():
    canonical = facts()
    canonical.canonical_artifacts[0].value = 'G:A001-is-a-real-object-name'
    data, _ = compile_data(canonical=canonical)
    assert data['sections'][0]['blocks'][1]['items'][0]['v'] == canonical.canonical_artifacts[0].value
    assert data['ioc']['host'][0]['value'] == canonical.canonical_artifacts[0].value


def test_fixed_composition_fixture_passes_original_template_contract():
    raw = json.loads((Path(__file__).parent / 'fixtures/html_composition.json').read_text(encoding='utf-8'))
    compiled, _ = compile_data(raw)
    assert HtmlReportSkill().validate(compiled)[0] == []


def test_prepare_context_is_usable_for_preflight_without_composition():
    resolved, fills = prepare_context({'hashes': [], 'attack': None, 'attribution': None, 'yara': None})
    assert resolved['hashes'] == [] and resolved['attack'] is None
    assert resolved['attribution'] is None and resolved['yara'] is None
    assert resolved['meta']['id'] == 'HTML-REPORT'
    assert fills == []
    with pytest.raises(ValueError, match='risk'):
        prepare_context({'meta': {'risk': 'critical'}})

