"""Validate editorial blocks without changing investigation output contracts."""

from copy import deepcopy

import pytest
from pydantic import ValidationError

from bounded_investigator.reporting.composition import ReportComposition, build_composition_input, validate_composition
from bounded_investigator.schemas import EvidenceLevel
from factories import report_canonical as canonical


def document():
    cited = {'text':'数据处理与结果写出构成主要机制。', 'claim_ids':['G:C001']}
    return {
        'title': '数据处理样本', 'findings': [],
        'summary':[deepcopy(cited)], 'verdict':deepcopy(cited), 'risk':'medium',
        'risk_judgement':deepcopy(cited), 'final':deepcopy(cited),
        'sections':[{'title':'敏感数据的读取与处理', 'question':'数据如何进入处理流程？', 'blocks':[
            {'type':'paragraph', **deepcopy(cited)},
            {'type':'kv', 'items':[{'label':'结果对象', 'value':{'fact_id':'G:A001','field':'value'}}]},
        ]}],
        'detection':[{'title':'关联核查','text':'关联输入读取、处理及输出活动。','claim_ids':['G:C001']}],
        'response':[{'title':'保全','text':'核对结果文件及关联进程。','artifact_ids':['G:A001']}],
    }


def test_input_includes_unconfirmed_behaviors_and_numbered_existing_excerpts():
    facts = canonical()
    facts.canonical_claims[0].evidence_level = EvidenceLevel.SUSPECTED
    projection = {'code_excerpts':{'G:C001':[{'code':'L0328: decrypt();','evidence_location':'F-0483 L328'}]}}
    before = deepcopy(projection)
    payload = build_composition_input(facts, projection)
    assert payload['all_behavior_ids'] == ['G:C001']
    assert 'core_claim_ids' not in payload
    assert payload['code_excerpts']['G:C001'][0]['excerpt_index'] == 0
    assert projection == before


def test_block_content_validates_without_automatic_field_tables():
    doc = ReportComposition.model_validate(document())
    validate_composition(doc, canonical(), {})
    assert [b.type for b in doc.sections[0].blocks] == ['paragraph','kv']
    assert 'claim_ids' not in type(doc.sections[0]).model_fields


@pytest.mark.parametrize('level', list(EvidenceLevel))
def test_every_behavior_level_belongs_in_body(level):
    facts = canonical()
    facts.canonical_claims[0].evidence_level = level
    raw = document()
    if level in {EvidenceLevel.LEAD, EvidenceLevel.UNCLASSIFIED}:
        raw['gaps'] = [{'claim_ids': ['G:C001'], 'text': '行为的证据状态待核查', 'impact': '影响能力认定', 'blocking': False}]
    doc = ReportComposition.model_validate(raw)
    validate_composition(doc, facts, {})
    doc.sections[0].blocks = doc.sections[0].blocks[1:]
    if level in {EvidenceLevel.CONFIRMED, EvidenceLevel.SUSPECTED}:
        with pytest.raises(ValueError, match='G:C001'):
            validate_composition(doc, facts, {})
    else:
        validate_composition(doc, facts, {})


@pytest.mark.parametrize('block', [
    {'type':'paragraph','text':'没有事实引用'},
    {'type':'list','items':[{'fact_id':'G:A404','field':'value'}]},
    {'type':'kv','items':[{'label':'对象','value':{'fact_id':'G:A001','field':'condition'}}]},
    {'type':'table','columns':[{'label':'行为','field':'behavior'}],'fact_ids':['G:A001']},
    {'type':'code','claim_id':'G:C404','excerpt_index':0},
    {'type':'code','claim_id':'G:C001','excerpt_index':9},
])
def test_invalid_block_references_fail_before_rendering(block):
    raw = document()
    raw['sections'][0]['blocks'].append(block)
    with pytest.raises(ValueError):
        validate_composition(ReportComposition.model_validate(raw), canonical(), {})


@pytest.mark.parametrize('field', ['confidence','evidence_level','canonical_id','source_claim_ids'])
def test_internal_fields_are_not_display_field_options(field):
    raw = document()
    raw['sections'][0]['blocks'].append({'type':'table','columns':[{'label':'字段','field':field}], 'fact_ids':['G:C001']})
    with pytest.raises(ValidationError):
        ReportComposition.model_validate(raw)


def test_arbitrary_html_and_cells_are_not_model_contract():
    raw = document()
    raw['sections'][0]['blocks'].append({'type':'html','text':'<div>bad</div>'})
    with pytest.raises(ValidationError):
        ReportComposition.model_validate(raw)
    raw['sections'][0]['blocks'][-1] = {'type':'table','columns':[{'label':'原值','field':'value'}],
                                      'fact_ids':['G:A001'],'rows':[['invented value']]}
    with pytest.raises(ValidationError):
        ReportComposition.model_validate(raw)


@pytest.mark.parametrize('internal', ['G:C001','G:A001','J12:C001','J00:A001'])
@pytest.mark.parametrize('surrounding', [('需要查看 ', ' 对应的行为。'), ('根据', '可见配置被写出。')])
def test_generated_narrative_cannot_expose_internal_references(internal, surrounding):
    raw = document()
    raw['summary'][0]['text'] = surrounding[0] + internal + surrounding[1]
    with pytest.raises(ValueError, match='内部'):
        validate_composition(ReportComposition.model_validate(raw), canonical(), {})
