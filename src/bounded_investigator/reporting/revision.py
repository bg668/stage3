"""Report-only, scoped semantic revisions of an immutable first draft."""

from copy import deepcopy
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ..schemas import GlobalReconciliationResult
from .composition import (
    ARTIFACT_FIELDS, CLAIM_FIELDS, CitedText, ChainStep, IOCNote, ReportAdvice, ReportComposition, ReportSection,
    ReportFinding, ReportValidationIssue, SelectedGap, Text, gap_records,
)
from .display import field_text


class SectionReplacement(BaseModel):
    model_config = ConfigDict(extra='forbid')
    section_index: int = Field(ge=0)
    section: ReportSection


class ReportRevisionPatch(BaseModel):
    """Omitted or null root fields are unchanged; lists replace the whole field."""
    model_config = ConfigDict(extra='forbid')
    section_replacements: list[SectionReplacement] = Field(default_factory=list)
    appended_sections: list[ReportSection] = Field(default_factory=list)
    title: Text | None = None
    findings: list[ReportFinding] | None = None
    summary: list[CitedText] | None = None
    verdict: CitedText | None = None
    risk: Literal['critical', 'high', 'medium', 'low', 'info'] | None = None
    risk_judgement: CitedText | None = None
    chain: list[ChainStep] | None = None
    detection: list[ReportAdvice] | None = None
    response: list[ReportAdvice] | None = None
    ioc_notes: list[IOCNote] | None = None
    gaps: list[SelectedGap] | None = None
    final: CitedText | None = None


def fact_references(item) -> set[str]:
    if isinstance(item, list):
        return set().union(*(fact_references(v) for v in item)) if item else set()
    if not isinstance(item, dict):
        return set()
    result = set()
    for key, value in item.items():
        if key in {'claim_ids', 'artifact_ids', 'fact_ids'}:
            result.update(value)
        elif key in {'claim_id', 'artifact_id', 'fact_id', 'gap_id'} and value is not None:
            result.add(value)
        else:
            result.update(fact_references(value))
    return result


def _visible(item):
    """Citation-only changes cannot count as fixing an omitted behavior."""
    if isinstance(item, list):
        return [_visible(v) for v in item]
    if isinstance(item, dict):
        return {k: _visible(v) for k, v in item.items() if k not in {'claim_ids', 'artifact_ids'}}
    return item


def _permissions(issues):
    missing = {cid for i in issues if i.code == 'missing_behavior' for cid in i.fact_ids}
    sections = {int(i.path.split('/')[2]) for i in issues
                if re.match(r'^/sections/\d+(?:/|$)', i.path)}
    roots = {i.path.split('/')[1] for i in issues if i.path.startswith('/') and not i.path.startswith('/sections')}
    return missing, sections, roots


def _citation_edits_authorized(before, after, path: str, issues) -> bool:
    if before == after:
        return True
    if path.rsplit('/', 1)[-1] in {'claim_ids', 'artifact_ids'}:
        return any(i.code in {'unknown_reference', 'duplicate_reference', 'missing_fact_reference'}
                   and (path == i.path or path.startswith(i.path + '/')) for i in issues)
    if isinstance(before, dict) and isinstance(after, dict) and before.keys() == after.keys():
        return all(_citation_edits_authorized(before[k], after[k], path + '/' + k, issues) for k in before)
    if isinstance(before, list) and isinstance(after, list) and len(before) == len(after):
        return all(_citation_edits_authorized(a, b, f'{path}/{i}', issues)
                   for i, (a, b) in enumerate(zip(before, after)))
    return False


def _body_content(item, facts: dict) -> set[tuple]:
    """Content atoms exclude citation lists, headings, style and component shape."""
    if isinstance(item, list):
        return set().union(*(_body_content(v, facts) for v in item)) if item else set()
    if not isinstance(item, dict):
        return set()
    if 'fact_id' in item:
        fact = facts.get(item['fact_id'])
        field = item.get('field')
        if fact is not None and field in (CLAIM_FIELDS if hasattr(fact, 'behavior') else ARTIFACT_FIELDS):
            return {('text', field_text(fact, field))}
        return set()  # The final validator reports invalid bindings.
    if item.get('type') == 'code':
        return {('code', item['claim_id'], item['excerpt_index'])}
    if item.get('type') == 'table':
        return set().union(*(_body_content({'fact_id': fid, 'field': column['field']}, facts)
                            for fid in item['fact_ids'] for column in item['columns']))
    content = {('text', item[k]) for k in ('text', 'caption', 'note') if item.get(k)}
    for key in ('items', 'value', 'content', 'blocks'):
        if key in item:
            content.update(_body_content(item[key], facts))
    return content


def apply_revision_patch(draft: ReportComposition, patch: ReportRevisionPatch,
                         issues: list[ReportValidationIssue], facts: GlobalReconciliationResult) -> ReportComposition:
    missing, allowed_sections, allowed_roots = _permissions(issues)
    raw = draft.model_dump(mode='json')
    known_claims = {c.canonical_id for c in facts.canonical_claims}
    fact_map = {c.canonical_id: c for c in (*facts.canonical_claims, *facts.canonical_artifacts)}
    original_content = _body_content(raw['sections'], fact_map)
    seen = set()
    for replacement in patch.section_replacements:
        index = replacement.section_index
        if index in seen or index >= len(draft.sections):
            raise ValueError('修订章节下标重复或越界')
        seen.add(index)
        if not missing and index not in allowed_sections:
            raise ValueError('修订越权：章节未涉及校验问题')
        original = raw['sections'][index]
        revised = replacement.section.model_dump(mode='json')
        old_refs = fact_references(original) & known_claims
        new_refs = fact_references(revised) & known_claims
        if not old_refs <= new_refs:
            raise ValueError('修订不得丢失原章节合法行为引用')
        section_path = f'/sections/{index}'
        if original == revised or (_visible(original) == _visible(revised)
                and not _citation_edits_authorized(original, revised, section_path, issues)):
            raise ValueError('无效修订：不能只补引用或返回未变章节')
        for cid in new_refs & missing:
            if not any(cid in fact_references(block) and block['type'] != 'subheading'
                       and (bool(_body_content(block, fact_map) - original_content)
                            or (bi < len(original['blocks']) and original['blocks'][bi] != block
                                and _citation_edits_authorized(original['blocks'][bi], block,
                                    f'{section_path}/blocks/{bi}', issues)))
                       for bi, block in enumerate(revised['blocks'])):
                raise ValueError('无效修订：遗漏行为必须修改正文或绑定内容，不能仅补引用')
        if missing and index not in allowed_sections and not (new_refs & missing):
            raise ValueError('修订越权：章节没有处理遗漏行为')
        raw['sections'][index] = revised
    if patch.appended_sections and not missing:
        raise ValueError('修订越权：只有遗漏行为允许追加机制章节')
    for section in patch.appended_sections:
        new = section.model_dump(mode='json')
        new_missing = fact_references(new) & missing
        if not new_missing:
            raise ValueError('新增章节必须处理遗漏行为')
        for cid in new_missing:
            if not any(cid in fact_references(b) and b['type'] != 'subheading'
                       and bool(_body_content(b, fact_map) - original_content)
                       for b in new['blocks']):
                raise ValueError('新增章节必须实质修改正文，不能复制原文后只补引用')
        raw['sections'].append(new)
    for key, value in patch.model_dump(mode='json', exclude_none=True).items():
        if key in {'section_replacements', 'appended_sections'}:
            continue
        if key not in allowed_roots:
            raise ValueError(f'修订越权：顶层字段 {key} 未获授权')
        raw[key] = value
    if raw == draft.model_dump(mode='json'):
        raise ValueError('无效修订：补丁没有修改任何内容')
    return ReportComposition.model_validate(raw)


def build_revision_input(draft: ReportComposition, issues: list[ReportValidationIssue],
                         facts: GlobalReconciliationResult, projection: dict) -> dict:
    """Use focused facts and chapters, never replay the original investigation input."""
    missing, section_indices, roots = _permissions(issues)
    document = draft.model_dump(mode='json')
    relevant = {cid for issue in issues for cid in issue.fact_ids}
    for issue in issues:
        current = document
        try:
            for part in issue.path.strip('/').split('/'):
                current = current[int(part)] if isinstance(current, list) else current[part]
            # For prose/reference errors, recover the owning block's citations.
            parts = issue.path.strip('/').split('/')
            if parts[-1] in {'text', 'claim_ids', 'artifact_ids', 'field', 'fact_id'}:
                current = document
                for part in parts[:-1]:
                    current = current[int(part)] if isinstance(current, list) else current[part]
            if issue.code != 'missing_behavior':
                relevant.update(fact_references(current))
        except (KeyError, IndexError, TypeError):
            pass
    claims = {c.canonical_id: c for c in facts.canonical_claims}
    # Shared function evidence supplies context only, never chapter placement.
    functions = {fid for cid in relevant if cid in claims for ref in claims[cid].evidence
                 for fid in re.findall(r'F-\d+', ref.location)}
    related = {cid for cid, claim in claims.items() if cid not in relevant
               and any(set(re.findall(r'F-\d+', ref.location)) & functions for ref in claim.evidence)}
    detailed = relevant | related
    return {
        'mode': 'revision',
        'issues': [i.model_dump(mode='json') for i in issues],
        'permissions': {'section_indices': list(range(len(draft.sections))) if missing else sorted(section_indices),
                        'append_sections': bool(missing), 'root_fields': sorted(roots)},
        'outline': [{'section_index': i, 'title': s.title,
                     'fact_ids': sorted(fact_references(document['sections'][i]))} for i, s in enumerate(draft.sections)],
        'sections': [{'section_index': i, 'section': document['sections'][i]}
                     for i in range(len(draft.sections)) if missing or i in section_indices],
        'root_fields': {name: document[name] for name in roots if name in document},
        'facts': {'claims': [c.model_dump(mode='json') for c in claims.values() if c.canonical_id in detailed],
                  'artifacts': [a.model_dump(mode='json') for a in facts.canonical_artifacts if a.canonical_id in relevant],
                  'gaps': [v for k, v in gap_records(facts).items() if k in relevant]},
        'related_fact_ids': sorted(related),
        'catalog': {'claims': [{'id': c.canonical_id, 'behavior': c.behavior, 'evidence_level': c.evidence_level.value}
                              for c in facts.canonical_claims],
                    'artifacts': [{'id': a.canonical_id, 'kind': a.kind, 'value': a.value}
                                  for a in facts.canonical_artifacts]},
        'code_excerpts': {cid: deepcopy(records) for cid, records in projection.get('code_excerpts', {}).items()
                          if cid in detailed},
    }
