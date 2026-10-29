"""Report writing contract: editorial content and references to immutable facts."""

from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
import re
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from ..evidence.inventory import InputInventory
from ..schemas import EvidenceLevel, GlobalReconciliationResult
from .display import field_text
from .html_template.scripts.validate import HEDGE_WORDS, load_rules

Text = Annotated[str, StringConstraints(min_length=1, pattern=r"\S")]
FactField = Literal['behavior', 'condition', 'source', 'action', 'sink', 'limitations',
                    'kind', 'value', 'meaning', 'evidence']
CLAIM_FIELDS = frozenset({'behavior','condition','source','action','sink','limitations','evidence'})
ARTIFACT_FIELDS = frozenset({'kind','value','meaning','evidence'})


class MissingReportBehaviors(ValueError):
    """Behavior IDs absent from the document body; other checks still apply."""

    def __init__(self, missing: set[str]):
        self.missing_ids = tuple(sorted(missing))
        super().__init__('正文遗漏分析行为: ' + ', '.join(self.missing_ids))


class _CompositionModel(BaseModel):
    model_config = ConfigDict(extra='forbid')


class CitedText(_CompositionModel):
    text: Text
    claim_ids: list[str] = Field(default_factory=list)
    artifact_ids: list[str] = Field(default_factory=list)


class FactValue(_CompositionModel):
    fact_id: str
    field: FactField


DisplayValue = CitedText | FactValue


class ParagraphBlock(CitedText):
    type: Literal['paragraph']


class SubheadingBlock(CitedText):
    type: Literal['subheading']


class CalloutBlock(CitedText):
    type: Literal['callout']
    tone: Literal['info','risk','boundary','warning'] = 'info'
    label: Text | None = None


class ListBlock(_CompositionModel):
    type: Literal['list']
    items: list[DisplayValue] = Field(min_length=1)


class TagsBlock(_CompositionModel):
    type: Literal['tags']
    items: list[DisplayValue] = Field(min_length=1)
    label: Text | None = None


class KeyValueItem(_CompositionModel):
    label: Text
    value: DisplayValue


class KeyValuesBlock(_CompositionModel):
    type: Literal['kv']
    items: list[KeyValueItem] = Field(min_length=1)
    mono: bool = False


class DisplayItem(_CompositionModel):
    title: Text
    content: DisplayValue


class CardsBlock(_CompositionModel):
    type: Literal['cards']
    items: list[DisplayItem] = Field(min_length=1)


class StepsBlock(_CompositionModel):
    type: Literal['steps']
    items: list[DisplayItem] = Field(min_length=1)


class TableColumn(_CompositionModel):
    label: Text
    field: FactField


class TableBlock(_CompositionModel):
    type: Literal['table']
    columns: list[TableColumn] = Field(min_length=1)
    fact_ids: list[str] = Field(min_length=1)
    note: Text | None = None


class CodeBlock(_CompositionModel):
    type: Literal['code']
    claim_id: str
    excerpt_index: int = Field(default=0, ge=0)
    caption: Text | None = None


ReportBlock = Annotated[
    ParagraphBlock | SubheadingBlock | CalloutBlock | ListBlock | TagsBlock |
    KeyValuesBlock | CardsBlock | StepsBlock | TableBlock | CodeBlock,
    Field(discriminator='type'),
]


class ReportSection(_CompositionModel):
    title: Text
    question: Text | None = None
    blocks: list[ReportBlock] = Field(min_length=1)


class ChainStep(_CompositionModel):
    name: Text
    brief: Text | None = None
    action: Text
    claim_ids: list[str]
    phase: Text | None = None
    key: bool = False


class ReportAdvice(CitedText):
    title: Text


class SelectedGap(_CompositionModel):
    gap_id: Text | None = None
    claim_ids: list[str] = Field(default_factory=list)
    text: Text = Field(description='面向读者的重要结论限制，简短概括；不是补料请求或历史问题原文')
    impact: Text
    blocking: bool


class IOCNote(_CompositionModel):
    artifact_id: Text
    text: Text = Field(description='该对象的简短用途或关联价值；不重复对象原值、证据清单或内部编号')


class ReportFinding(_CompositionModel):
    rule: Text = Field(description='tier_rules.json 中的规则 ID；没有合适规则时使用 CUSTOM')
    title: Text
    claim_ids: list[str] = Field(min_length=1)
    state_note: Text | None = Field(default=None, description='引用疑似行为时，说明尚缺的证据')
    section_index: int | None = Field(default=None, ge=0, description='目标机制章节的零起始索引')
    tier: Literal['malicious', 'suspicious', 'info'] | None = None
    category: Text | None = None
    tier_reason: Text | None = None


class ReportComposition(_CompositionModel):
    """A structured report; its fact references are never reader-facing fields."""

    title: Text
    findings: list[ReportFinding]
    summary: list[CitedText] = Field(min_length=1)
    verdict: CitedText
    risk: Literal['critical','high','medium','low','info']
    risk_judgement: CitedText
    chain: list[ChainStep] = Field(default_factory=list)
    sections: list[ReportSection] = Field(default_factory=list)
    detection: list[ReportAdvice] = Field(min_length=1)
    response: list[ReportAdvice] = Field(min_length=1)
    ioc_notes: list[IOCNote] = Field(default_factory=list)
    gaps: list[SelectedGap] = Field(default_factory=list)
    final: CitedText


def gap_records(canonical: GlobalReconciliationResult) -> dict[str, dict[str, Any]]:
    return {
        gap.gap_id or f'R:G{index:03d}': {
            **gap.model_dump(mode='json'), 'gap_id': gap.gap_id or f'R:G{index:03d}',
        }
        for index, gap in enumerate(canonical.gaps, 1)
    }


def _material_coverage(inventory: InputInventory | None) -> list[dict]:
    """Combine supplied source ranges; completeness says nothing about semantics."""
    grouped = defaultdict(list)
    for section in inventory.code_sections if inventory else []:
        grouped[section.function_id].append(section)
    coverage = []
    for function_id, sections in sorted(grouped.items()):
        ranges: list[dict[str, int]] = []
        for section in sorted(sections, key=lambda s: (s.start_line, s.end_line)):
            if ranges and section.start_line <= ranges[-1]['end_line'] + 1:
                ranges[-1]['end_line'] = max(ranges[-1]['end_line'], section.end_line)
            else:
                ranges.append({'start_line': section.start_line, 'end_line': section.end_line})
        totals = {section.total_lines for section in sections}
        total = next(iter(totals)) if len(totals) == 1 else None
        coverage.append({
            'function_id': function_id, 'symbol': sections[0].symbol,
            'provided_ranges': ranges, 'total_lines': total,
            'complete': total is not None and ranges == [{'start_line': 1, 'end_line': total}],
            'packet_ids': sorted({section.packet_id for section in sections}),
        })
    return coverage


def build_composition_input(
    canonical: GlobalReconciliationResult,
    report: dict,
    inventory: InputInventory | None = None,
) -> dict:
    excerpts = deepcopy(report.get('code_excerpts', {}))
    for records in excerpts.values():
        for index, excerpt in enumerate(records):
            excerpt['excerpt_index'] = index
    return {
        'claims': [c.model_dump(mode='json') for c in canonical.canonical_claims],
        'artifacts': [a.model_dump(mode='json') for a in canonical.canonical_artifacts],
        'all_behavior_ids': [c.canonical_id for c in canonical.canonical_claims],
        'iocs': deepcopy(report.get('iocs', [])),
        'detection_features': deepcopy(report.get('detection_features', [])),
        'context_artifacts': deepcopy(report.get('context_artifacts', [])),
        'code_excerpts': excerpts,
        'historical_gaps': list(gap_records(canonical).values()),
        'unresolved_conflicts': [c.model_dump(mode='json') for c in canonical.unresolved_conflicts],
        'functions': _material_coverage(inventory),
    }


class ReportValidationIssue(_CompositionModel):
    code: str
    path: str
    fact_ids: list[str] = Field(default_factory=list)
    message: str
    correction: str


class ReportValidationError(ValueError):
    def __init__(self, issues: list[ReportValidationIssue]):
        self.issues = issues
        super().__init__('; '.join(f'{i.path}: {i.message}' for i in issues))


def collect_composition_issues(
    composition: ReportComposition,
    canonical: GlobalReconciliationResult,
    report: dict | None = None,
) -> list[ReportValidationIssue]:
    """Collect deterministic contract errors, without claiming prose is true."""
    issues: list[ReportValidationIssue] = []

    def add(code: str, path: str, message: str, correction: str, refs=()) -> None:
        issues.append(ReportValidationIssue(code=code, path=path, fact_ids=sorted(set(refs)),
            message=message, correction=correction))

    claims = {c.canonical_id: c for c in canonical.canonical_claims}
    artifacts = {a.canonical_id: a for a in canonical.canonical_artifacts}
    internal_ids = set(claims) | set(artifacts)
    internal_ids.update(s for claim in claims.values() for s in claim.source_claim_ids)
    internal_ids.update(s for artifact in artifacts.values() for s in artifact.source_artifact_ids)
    internal_ids.update(gap_records(canonical))
    standardized = [re.escape(item) for item in internal_ids if re.fullmatch(r'(?:G|R|J\d+):[CAG]\d+', item)]
    packet_ids = {source.split(':', 1)[0] for item in [*claims.values(), *artifacts.values()]
                  for source in (item.source_claim_ids if item.canonical_id in claims else item.source_artifact_ids)}
    packet_ids.update(packet for gap in canonical.gaps for packet in gap.source_packets)
    standardized.extend(re.escape(packet) for packet in packet_ids if re.fullmatch(r'J\d+', packet))
    internal_pattern = re.compile(r'(?<![A-Za-z0-9_:])(?:' + '|'.join(standardized) + r')(?![A-Za-z0-9_:])') if standardized else None

    def public_text(item: Any, path: str) -> None:
        if isinstance(item, dict):
            for key, child in item.items():
                if key not in {'claim_ids','artifact_ids','fact_ids','fact_id','claim_id','gap_id','artifact_id'}:
                    public_text(child, path + '/' + key)
        elif isinstance(item, list):
            for index, child in enumerate(item):
                public_text(child, path + '/' + str(index))
        elif isinstance(item, str) and internal_pattern is not None and internal_pattern.search(item):
            add('internal_metadata', path, '正文不得展示内部事实或来源 ID；原始对象值请使用事实绑定',
                '移除正文内部编号；需要展示原始对象值时使用事实绑定')

    public_text(composition.model_dump(mode='json'), '')
    facts = claims | artifacts
    uncertainty_count = 0
    uncertainty_refs: set[str] = set()

    def prose(text: str, refs=()) -> None:
        nonlocal uncertainty_count
        count = sum(text.count(word) for word in HEDGE_WORDS)
        uncertainty_count += count
        if count:
            uncertainty_refs.update(refs)

    def ids(values: list[str], allowed: dict, path: str) -> set[str]:
        invalid = set(values) - allowed.keys()
        if invalid:
            add('unknown_reference', path, f'未知事实引用 {", ".join(sorted(invalid))}',
                '只使用提供的事实 ID，移除或纠正非法引用', invalid)
        if len(values) != len(set(values)):
            add('duplicate_reference', path, '事实引用重复', '去除重复引用，保留正文含义', values)
        return set(values) & allowed.keys()

    def value(item: DisplayValue, path: str) -> set[str]:
        if isinstance(item, FactValue):
            fields = CLAIM_FIELDS if item.fact_id in claims else ARTIFACT_FIELDS if item.fact_id in artifacts else None
            if fields is None:
                add('unknown_reference', path + '/fact_id', f'未知事实引用 {item.fact_id}',
                    '改用已有事实 ID', [item.fact_id])
                return set()
            if item.field not in fields:
                add('invalid_fact_field', path + '/field', f'字段 {item.field} 不适用于 {item.fact_id}',
                    '选择该类事实支持的字段，或用有引用的正文解释', [item.fact_id])
                return set()
            if item.field != 'value':
                bound = getattr(facts[item.fact_id], item.field)
                if item.field == 'evidence':
                    bound = [ref.model_dump(mode='json') for ref in bound]
                public_text(bound, path)
            if not field_text(facts[item.fact_id], item.field).strip():
                add('empty_bound_content', path, '展示内容绑定为空；请选择有内容的字段或用正文解释该行为',
                    '选择有实际内容的字段，或改为有事实引用的简短正文', [item.fact_id])
            prose(field_text(facts[item.fact_id], item.field), [item.fact_id] if item.fact_id in claims else [])
            return {item.fact_id} if item.fact_id in claims else set()
        if not item.claim_ids and not item.artifact_ids:
            add('missing_fact_reference', path, '缺少事实引用', '为当前陈述关联已有事实，不新增事实')
        cited = ids(item.claim_ids, claims, path + '/claim_ids')
        ids(item.artifact_ids, artifacts, path + '/artifact_ids')
        if path.startswith(('/sections/', '/summary/')) or path in {'/verdict', '/risk_judgement', '/final'}:
            prose(item.text, cited)
        return cited

    for name in ('verdict','risk_judgement','final'):
        value(getattr(composition, name), '/' + name)
    for name in ('summary','detection','response'):
        for index, item in enumerate(getattr(composition, name)):
            value(item, f'/{name}/{index}')
    for index, step in enumerate(composition.chain):
        if not step.claim_ids:
            add('missing_fact_reference', f'/chain/{index}', '缺少事实引用', '只表达有事实依据的顺序，无法支持时移除链条')
        ids(step.claim_ids, claims, f'/chain/{index}/claim_ids')
        prose(step.action, set(step.claim_ids) & claims.keys())

    noted: set[str] = set()
    note_values: dict[str, list[str]] = {}
    for index, note in enumerate(composition.ioc_notes):
        ids([note.artifact_id], artifacts, f'/ioc_notes/{index}/artifact_id')
        if note.artifact_id in noted:
            add('duplicate_ioc_note', f'/ioc_notes/{index}', f'重复对象说明 {note.artifact_id}',
                '每个对象只保留一项简短说明', [note.artifact_id])
        noted.add(note.artifact_id)
        if note.artifact_id in artifacts:
            key = artifacts[note.artifact_id].value.strip().lower()
            if note.text not in note_values.setdefault(key, []):
                prose(note.text)
                note_values[key].append(note.text)

    represented: set[str] = set()
    for si, section in enumerate(composition.sections):
        for bi, block in enumerate(section.blocks):
            path = f'/sections/{si}/blocks/{bi}'
            if isinstance(block, (ParagraphBlock, CalloutBlock, SubheadingBlock)):
                cited = value(block, path)
                if not isinstance(block, SubheadingBlock):
                    represented.update(cited)
            elif isinstance(block, (ListBlock, TagsBlock)):
                for index, item in enumerate(block.items):
                    represented.update(value(item, f'{path}/items/{index}'))
            elif isinstance(block, KeyValuesBlock):
                for index, item in enumerate(block.items):
                    cited = value(item.value, f'{path}/items/{index}/value')
                    represented.update(cited)
                    prose(item.label, cited)
            elif isinstance(block, (CardsBlock, StepsBlock)):
                for index, item in enumerate(block.items):
                    cited = value(item.content, f'{path}/items/{index}/content')
                    represented.update(cited)
                    prose(item.title, cited)
            elif isinstance(block, TableBlock):
                ids(block.fact_ids, facts, path + '/fact_ids')
                if block.note:
                    prose(block.note, set(block.fact_ids) & claims.keys())
                for ri, fact_id in enumerate(block.fact_ids):
                    row = []
                    for ci, column in enumerate(block.columns):
                        fields = CLAIM_FIELDS if fact_id in claims else ARTIFACT_FIELDS if fact_id in artifacts else ()
                        if fact_id not in facts:
                            continue
                        if column.field not in fields:
                            add('invalid_fact_field', f'{path}/columns/{ci}/field',
                                f'字段 {column.field} 不适用于 {fact_id}', '改用所有行都支持的字段或拆分表格', [fact_id])
                            continue
                        text = field_text(facts[fact_id], column.field)
                        row.append(text)
                        prose(text, [fact_id] if fact_id in claims else [])
                        if column.field != 'value':
                            public_text(text, f'{path}/fact_ids/{ri}')
                    if fact_id in claims:
                        represented.add(fact_id)
                    if row and not any(text.strip() for text in row):
                        add('empty_bound_content', f'{path}/fact_ids/{ri}', '表格存在全空事实行',
                            '选择有内容的列或用正文解释该行为', [fact_id])
            elif isinstance(block, CodeBlock):
                known = ids([block.claim_id], claims, path + '/claim_id')
                if block.caption:
                    prose(block.caption, known)
                if report is not None:
                    records = report.get('code_excerpts', {}).get(block.claim_id, [])
                    if block.excerpt_index >= len(records):
                        add('missing_excerpt', path + '/excerpt_index',
                            f'源码片段引用不存在 {block.claim_id}[{block.excerpt_index}]',
                            '选择已有片段；无片段时改用有引用的正文，不补造源码', [block.claim_id])
                represented.update(known)
    eligible = {cid for cid, claim in claims.items()
                if claim.evidence_level in {EvidenceLevel.CONFIRMED, EvidenceLevel.SUSPECTED}}
    missing = eligible - represented
    if missing:
        add('missing_behavior', '/sections', '正文遗漏分析行为: ' + ', '.join(sorted(missing)),
            '在合适机制章节实质表述这些行为及关键条件，不能只补引用', missing)

    known_gaps = gap_records(canonical)
    seen: set[str] = set()
    gap_claims: set[str] = set()
    for index, gap in enumerate(composition.gaps):
        path = f'/gaps/{index}'
        if gap.gap_id is None and not gap.claim_ids:
            add('missing_gap_reference', path, '缺口缺少事实依据', '引用已有 gap_id 或 claim_ids')
        gap_claims.update(ids(gap.claim_ids, claims, path + '/claim_ids'))
        if gap.gap_id is not None and gap.gap_id not in known_gaps:
            add('unknown_gap', f'/gaps/{index}/gap_id', f'未知问题引用 {gap.gap_id}',
                '只使用已有且影响主要判断的问题，其他移除', [gap.gap_id])
        if gap.gap_id is not None and gap.gap_id in seen:
            add('duplicate_gap', f'/gaps/{index}', f'重复问题引用 {gap.gap_id}',
                '合并重复的重要限制', [gap.gap_id])
        if gap.gap_id is not None:
            seen.add(gap.gap_id)
    for cid in sorted(claims.keys() - eligible - gap_claims):
        add('missing_lead_gap', '/gaps', '调查线索或未分类行为缺少缺口说明',
            '在 gaps 中说明当前未知点并用 claim_ids 关联；不得认定为已证实能力', [cid])

    rules, categories = load_rules()
    for index, finding in enumerate(composition.findings):
        path = f'/findings/{index}'
        cited = ids(finding.claim_ids, claims, path + '/claim_ids')
        prose(finding.title, cited)
        if finding.state_note:
            prose(finding.state_note, cited)
        ineligible = cited - eligible
        if ineligible:
            add('ineligible_finding', path, '调查线索和未分类行为不能进入关键发现',
                '将线索写入 gaps；关键发现只引用已确认或疑似行为', ineligible)
        if finding.rule == 'CUSTOM':
            if not finding.tier or finding.category not in categories or not finding.tier_reason:
                add('invalid_custom_finding', path, 'CUSTOM 必须提供合法 tier、category 和 tier_reason',
                    '从规则表选择合适规则，或给出自定义档位、类别和理由', cited)
        elif finding.rule not in rules:
            add('unknown_finding_rule', path + '/rule', f'未知发现规则 {finding.rule}',
                '使用 tier_rules.json 中的规则 ID，或使用完整的 CUSTOM 规则', cited)
        elif any(value is not None for value in (finding.tier, finding.category, finding.tier_reason)):
            add('finding_rule_override', path, '普通规则不能覆盖档位、类别或理由',
                '移除 tier、category、tier_reason，档位和类别由规则表决定', cited)
        if finding.section_index is not None and finding.section_index >= len(composition.sections):
            add('unknown_finding_section', path + '/section_index', '关键发现指向不存在的机制章节',
                '选择已有章节的零起始索引，或省略跳转', cited)
        suspected = {cid for cid in cited if claims[cid].evidence_level == EvidenceLevel.SUSPECTED}
        if suspected and not finding.state_note:
            add('missing_state_note', path + '/state_note', '部分证实的发现缺少证据缺口说明',
                '用一句话说明疑似行为尚缺什么，不得升级事实确定程度', suspected)
        if suspected - gap_claims:
            add('missing_finding_gap', '/gaps', '部分证实的发现必须关联缺口',
                '在 gaps 中说明未闭合部分，并用 claim_ids 关联对应疑似行为', suspected - gap_claims)
    if not composition.gaps and uncertainty_count >= 3:
        add('missing_prose_gap', '/gaps', '正文多次描述未闭合的未知点，但 gaps 为空',
            '将仍成立的未知点和具体影响写入 gaps，并关联已有行为或历史问题', uncertainty_refs)
    return issues


def validate_composition(composition: ReportComposition, canonical: GlobalReconciliationResult,
                         report: dict | None = None) -> None:
    issues = collect_composition_issues(composition, canonical, report)
    if issues:
        if all(i.code == 'missing_behavior' for i in issues):
            raise MissingReportBehaviors({cid for i in issues for cid in i.fact_ids})
        raise ReportValidationError(issues)
