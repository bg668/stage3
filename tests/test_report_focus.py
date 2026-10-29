"""Reader-facing choices are separate from immutable investigation records."""

import json

import pytest

from bounded_investigator.evidence.inventory import CodeSection, InputInventory
from bounded_investigator.reporting.composition import ReportComposition, build_composition_input, validate_composition
from bounded_investigator.reporting.html_skill import HtmlReportSkill
from bounded_investigator.schemas import EvidenceLevel
from test_html_composition import composition_dict, facts, report_projection


def document():
    raw = composition_dict()
    raw['gaps'] = []
    raw.pop('ioc_notes', None)
    return raw


def test_historical_questions_are_context_not_current_report_gaps():
    canonical = facts()
    before = canonical.model_dump()
    data = build_composition_input(canonical, report_projection(canonical))
    assert 'gaps' not in data
    assert data['historical_gaps'][0]['question'] == '配置键值未解析'
    assert data['historical_gaps'][0]['gap_id'] == 'R:G001'
    assert canonical.model_dump() == before


@pytest.mark.parametrize('second_start,complete,ranges', [
    (275, True, [{'start_line': 1, 'end_line': 522}]),
    (280, False, [{'start_line': 1, 'end_line': 274}, {'start_line': 280, 'end_line': 522}]),
])
def test_cross_packet_material_ranges_are_combined_without_inventing_coverage(second_start, complete, ranges):
    inventory = InputInventory(code_sections=[
        CodeSection(function_id='F-0483', symbol='sub_409EF8', packet_id='J11',
                    start_line=1, end_line=274, total_lines=522, source_path='J11.md'),
        CodeSection(function_id='F-0483', symbol='sub_409EF8', packet_id='J12',
                    start_line=second_start, end_line=522, total_lines=522, source_path='J12.md'),
    ])
    before = inventory.model_dump()
    canonical = facts()
    data = build_composition_input(canonical, report_projection(canonical), inventory)
    assert len(data['functions']) == 1
    record = data['functions'][0]
    assert record['provided_ranges'] == ranges
    assert record['complete'] is complete
    assert record['packet_ids'] == ['J11', 'J12']
    assert 'source_path' not in record
    assert inventory.model_dump() == before


def test_important_limit_uses_report_wording_not_the_historical_material_request():
    canonical = facts()
    canonical.gaps[0].question = '请补齐函数并重新分析所有材料'
    raw = document()
    raw['gaps'] = [{'gap_id': 'R:G001', 'text': '配置项控制的功能尚未确定',
                    'impact': '影响对启用能力范围的判断', 'blocking': False}]
    before = canonical.model_dump()
    data, _ = HtmlReportSkill().compile(ReportComposition.model_validate(raw), canonical,
                                       projection=report_projection(canonical))
    assert data['gaps'] == [{'gap': '配置项控制的功能尚未确定',
                            'impact': '影响对启用能力范围的判断', 'blocking': False}]
    assert '请补齐' not in json.dumps(data, ensure_ascii=False)
    assert canonical.model_dump() == before


def test_ioc_values_are_bound_but_optional_notes_are_reader_authored():
    canonical = facts()
    artifact = canonical.canonical_artifacts[0]
    artifact.meaning = 'J00 已验证的输出；内部处理记录'
    artifact.evidence[0].observation = '内部审计信息不能自动成为 IOC 说明'
    raw = document()
    raw['ioc_notes'] = [{'artifact_id': 'G:A001', 'text': '用于关联配置落地活动'}]
    before = canonical.model_dump()
    data, _ = HtmlReportSkill().compile(ReportComposition.model_validate(raw), canonical,
                                       projection=report_projection(canonical))
    assert data['ioc']['host'] == [{'type': 'file', 'value': artifact.value, 'note': '用于关联配置落地活动',
                                  'role': '输出落地', 'specificity': 'high', 'evidence': ['F-0012 L12']}]
    assert canonical.model_dump() == before


def test_ioc_without_note_does_not_gain_raw_evidence_or_internal_meaning():
    canonical = facts()
    canonical.canonical_artifacts[0].meaning = 'J00 已验证的输出'
    raw = document()
    data, _ = HtmlReportSkill().compile(ReportComposition.model_validate(raw), canonical,
                                       projection=report_projection(canonical))
    item = data['ioc']['host'][0]
    assert not item.get('note')
    assert item['value'] == canonical.canonical_artifacts[0].value


@pytest.mark.parametrize('notes', [
    [{'artifact_id': 'G:A404', 'text': '未知对象'}],
    [{'artifact_id': 'G:A001', 'text': '说明一'}, {'artifact_id': 'G:A001', 'text': '说明二'}],
    [{'artifact_id': 'G:A001', 'text': '依据 J00 的分析结果'}],
])
def test_unknown_duplicate_and_internal_ioc_notes_are_rejected(notes):
    raw = document()
    raw['ioc_notes'] = notes
    with pytest.raises(ValueError):
        validate_composition(ReportComposition.model_validate(raw), facts())


@pytest.mark.parametrize('binding', [False, True])
def test_packet_identifiers_are_rejected_in_authored_and_bound_prose(binding):
    canonical = facts()
    raw = document()
    if binding:
        canonical.canonical_artifacts[0].meaning = 'J00 已验证的固定输出'
        raw['sections'][0]['blocks'].append({'type': 'kv', 'items': [
            {'label': '用途', 'value': {'fact_id': 'G:A001', 'field': 'meaning'}},
        ]})
    else:
        raw['sections'][0]['blocks'][0]['text'] = '依据 J00 的分析结果写出配置'
    with pytest.raises(ValueError, match='内部'):
        validate_composition(ReportComposition.model_validate(raw), canonical)


def test_concise_purpose_grouping_preserves_unconfirmed_behavior_without_a_gap_section():
    canonical = facts()
    second = canonical.canonical_claims[0].model_copy(deep=True, update={
        'canonical_id': 'G:C002', 'behavior': '配置可选择附加收集路径', 'evidence_level': EvidenceLevel.SUSPECTED,
        'condition': '模式标志非零', 'source_claim_ids': ['J00:C002'],
    })
    canonical.canonical_claims.append(second)
    raw = document()
    raw['sections'][0]['blocks'] = [
        {'type': 'paragraph', 'text': '解码后的配置被保存，模式标志非零时还可能选择附加收集路径。',
         'claim_ids': ['G:C001', 'G:C002']},
        {'type': 'list', 'items': [{'fact_id': 'G:A001', 'field': 'value'}]},
    ]
    data, _ = HtmlReportSkill().compile(ReportComposition.model_validate(raw), canonical,
                                       projection=report_projection(canonical))
    assert data['gaps'] == []
    assert [b['type'] for b in data['sections'][0]['blocks']] == ['paragraph', 'list']
    assert '可能' in data['sections'][0]['blocks'][0]['text']
    assert canonical.canonical_claims[1].condition == '模式标志非零'
