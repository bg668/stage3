"""Deterministic HTML skill: load guidance, bind facts, validate and render.

This capability has no model or workflow dependency. Its input document chooses
presentation; original fact values and selected source excerpts remain read-only.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from importlib.resources import files
import json
from typing import Any

import yaml

from ..evidence.inventory import InputInventory
from ..evidence.snippets import normalize_function_id, parse_evidence_location
from ..schemas import EvidenceLevel, GlobalReconciliationResult
from .display import field_text as _field_text
from .composition import ReportComposition, validate_composition
from .html_template import TEMPLATE_VERSION, render_html, validate_report

_SKILL_ROOT = 'skills/malware-report-html'
_SKILL_FILES = (f'{_SKILL_ROOT}/SKILL.md', f'{_SKILL_ROOT}/references/blocks.md',
                f'{_SKILL_ROOT}/references/modules.md', 'html_template/assets/tier_rules.json')
_TEMPLATE_FILES = ('__init__.py', 'scripts/render.py', 'scripts/validate.py', 'assets/report.css', 'assets/tier_rules.json')
_CONTEXT_FIELDS = {'meta', 'hashes', 'attack', 'attribution', 'attribution_empty_reason', 'yara'}
_META_FIELDS = {'id', 'title', 'date', 'product', 'edition', 'method', 'tlp', 'sample'}
_ROLE_LABELS = {
    'created_output': '输出落地', 'created_mutex': '实例控制', 'persistence_value': '持久化',
    'config_marker': '配置标记', 'read_source': '读取来源', 'environment_probe': '环境探测',
    'system_path': '系统路径', 'parser_marker': '解析标记', 'other': '其他对象',
}


def prepare_context(context: dict | None) -> tuple[dict, list[dict]]:
    """Supply report metadata and truthful empty modules, never sample facts."""
    if context is None:
        context = {}
    if not isinstance(context, dict):
        raise ValueError('context 必须是 JSON 对象')
    unknown = set(context) - _CONTEXT_FIELDS
    if unknown:
        raise ValueError(f"context 不支持字段: {', '.join(sorted(unknown))}")
    meta = context.get('meta', {})
    if meta is not None and not isinstance(meta, dict):
        raise ValueError('context.meta 必须是对象或 null')
    unknown_meta = set(meta or {}) - _META_FIELDS
    if unknown_meta:
        raise ValueError(f"context.meta 不支持字段: {', '.join(sorted(unknown_meta))}")
    result = deepcopy(context)
    if 'meta' not in result:
        result['meta'] = {}
    if isinstance(result['meta'], dict):
        result['meta'].setdefault('id', 'HTML-REPORT')
        result['meta'].setdefault('date', datetime.now(timezone(timedelta(hours=8))).date().isoformat())
        result['meta'].setdefault('method', '静态分析（基于提供的源码与结构化分析结果）')
    if result.get('hashes') is None:
        result['hashes'] = []
    result.setdefault('attack', [])
    result.setdefault('attribution', None)
    result.setdefault('yara', None)
    if result['attribution'] is None:
        result.setdefault('attribution_empty_reason', '暂无归因资料。')
    return result, []


def _audit_meta(projection: dict, audit: dict | None) -> dict:
    reference = (audit or {}).get('evidence_reference_audit', {})
    executed = reference.get('status') == 'completed' or (
        'status' not in reference and reference.get('schema') == 'evidence_reference_validation_v1'
        and reference.get('scope') == 'full_material'
        and isinstance(reference.get('passed'), bool)
        and isinstance(reference.get('total_segments'), int))
    if projection.get('status') == 'incomplete_provenance' or (audit and not audit.get('passed')):
        status = 'incomplete'
    elif not audit or not executed:
        status = 'not_run'
    else:
        status = 'passed' if (audit.get('passed') is True and reference.get('passed', True) is True
                              and reference.get('invalid_segments', 0) == 0) else 'incomplete'
    result = {'status': status}
    if executed:
        for target, source in (('refs_total', 'total_segments'), ('refs_invalid', 'invalid_segments')):
            if source in reference:
                result[target] = reference[source]
    if status == 'incomplete' and audit and audit.get('issues'):
        # Audit detail can contain internal fact IDs; keep it in the manifest.
        result['note'] = '报告来源或源码引用审计存在未通过项，详见原始审计记录。'
    return result


def _compile_iocs(projection: dict, artifacts: dict, notes: dict) -> dict:
    ioc: dict[str, Any] = {'host': [], 'network': []}
    values: dict[str, dict] = {}
    for group, specificity in (('iocs', 'high'), ('detection_features', 'medium'), ('context_artifacts', 'context')):
        for item in projection.get(group, []):
            artifact = artifacts[item['canonical_id']]
            key = artifact.value.strip().lower()
            role = _ROLE_LABELS[artifact.role]
            evidence = list(dict.fromkeys(ref.location for ref in artifact.evidence))
            note = notes.get(artifact.canonical_id, '')
            if key in values:
                existing = values[key]
                existing['role'] = '；'.join(dict.fromkeys([*existing['role'].split('；'), role]))
                existing['evidence'] = list(dict.fromkeys([*existing['evidence'], *evidence]))
                if note and note not in existing['note'].split('；'):
                    existing['note'] = '；'.join(filter(None, [existing['note'], note]))
                continue
            record = {'type': artifact.kind, 'value': artifact.value, 'role': role,
                      'specificity': specificity, 'note': note, 'evidence': evidence}
            values[key] = record
            ioc['network' if artifact.kind == 'network' else 'host'].append(record)
    for kind in ('host', 'network'):
        if not ioc[kind]:
            ioc[f'{kind}_empty_reason'] = '暂无主机指标或相关对象。' if kind == 'host' else '暂无网络指标或相关对象。'
    return ioc


def _compile_blocks(section: dict, claims: dict, artifacts: dict, projection: dict) -> tuple[list[dict], list[tuple[dict, str]]]:
    blocks: list[dict] = []
    selected: list[tuple[dict, str]] = []
    facts = {**claims, **artifacts}

    def value(item: dict) -> str:
        text = _field_text(facts[item['fact_id']], item['field']) if 'fact_id' in item else item['text']
        if not text.strip():
            raise ValueError('展示内容绑定为空；请选择有内容的字段或用正文解释该行为')
        return text

    for block in section['blocks']:
        kind = block['type']
        if kind in {'paragraph', 'subheading', 'callout'}:
            rendered = {'type': kind, 'text': block['text']}
            if kind == 'callout':
                rendered['tone'] = block['tone']
                if block.get('label'):
                    rendered['label'] = block['label']
        elif kind in {'list', 'tags'}:
            rendered = {'type': kind, 'items': [value(item) for item in block['items']]}
            if block.get('label'):
                rendered['label'] = block['label']
        elif kind == 'kv':
            rendered = {'type': kind, 'mono': block['mono'], 'items': [
                {'k': item['label'], 'v': value(item['value'])} for item in block['items']
            ]}
        elif kind in {'cards', 'steps'}:
            rendered = {'type': kind, 'items': [
                {'title': item['title'], 'text': value(item['content'])} for item in block['items']
            ]}
        elif kind == 'table':
            rows = [[_field_text(facts[fact_id], column['field']) for column in block['columns']]
                    for fact_id in block['fact_ids']]
            if any(not any(cell.strip() for cell in row) for row in rows):
                raise ValueError('表格存在全空事实行；请选择有内容的字段或用正文解释该行为')
            rendered = {
                'type': kind, 'columns': [column['label'] for column in block['columns']],
                'rows': rows,
                'mono_columns': [i for i, column in enumerate(block['columns'])
                                 if column['field'] in {'source', 'action', 'sink', 'value', 'evidence'}],
            }
            if block.get('note'):
                rendered['note'] = block['note']
        elif kind == 'code':
            excerpt = projection['code_excerpts'][block['claim_id']][block['excerpt_index']]
            caption = block.get('caption')
            location = excerpt['evidence_location']
            blocks.append({'type': 'subheading', 'text': f'{caption} · {location}' if caption else location})
            rendered = {'type': kind, 'language': 'c', 'text': excerpt['code']}
            selected.append((excerpt, caption or claims[block['claim_id']].behavior))
        else:
            raise ValueError(f'不支持的展示组件: {kind}')
        blocks.append(rendered)
    return blocks, selected


def _function_index(selected: list[tuple[dict, str]], inventory: InputInventory | None) -> list[dict]:
    symbols = {}
    if inventory:
        for section in inventory.code_sections:
            symbols.setdefault(normalize_function_id(section.function_id), section.symbol)
    indexed: dict[tuple[str, int, int], dict] = {}
    for excerpt, role in selected:
        for fid, start, end in parse_evidence_location(excerpt['evidence_location']):
            key = (fid, start, end)
            if key not in indexed:
                symbol = symbols.get(fid)
                indexed[key] = {
                    'function': f'{fid} {symbol}' if symbol else fid,
                    'role': role, 'lines': f'L{start:04d}-L{end:04d}',
                }
            elif role not in indexed[key]['role']:
                indexed[key]['role'] += '；' + role
    return list(indexed.values())


class HtmlReportSkill:
    """One frozen HTML skill and its packaged template, with no model calls."""

    def __init__(self) -> None:
        self._resources = files(__package__)
        self._sources = {}
        for name in _SKILL_FILES:
            try:
                self._sources[name] = self._resources.joinpath(name).read_bytes().decode('utf-8')
            except (OSError, UnicodeError) as exc:
                raise ValueError(f'无法加载 HTML skill 资源: {name}') from exc
        skill_text = self._sources[_SKILL_FILES[0]]
        lines = skill_text.splitlines(keepends=True)
        if not lines or lines[0].strip() != '---':
            raise ValueError('HTML skill 缺少 YAML frontmatter')
        try:
            end = next(index for index, line in enumerate(lines[1:], 1) if line.strip() == '---')
            metadata = yaml.safe_load(''.join(lines[1:end]))
        except (StopIteration, yaml.YAMLError) as exc:
            raise ValueError('HTML skill frontmatter 无效') from exc
        if not isinstance(metadata, dict):
            raise ValueError('HTML skill metadata 必须是对象')
        if metadata.get('name') != 'malware-report-html':
            raise ValueError('HTML skill name 必须为 malware-report-html')
        if not isinstance(metadata.get('description'), str) or not metadata['description'].strip():
            raise ValueError('HTML skill description 不能为空')
        if metadata.get('roles') != ['report']:
            raise ValueError('HTML skill roles 必须为 [report]')
        meta = metadata.get('metadata')
        version = meta.get('version') if isinstance(meta, dict) else None
        if not isinstance(version, str) or not version.strip():
            raise ValueError('HTML skill metadata.version 不能为空')
        body = ''.join(lines[end + 1:]).strip()
        references = [self._sources[name].strip() for name in _SKILL_FILES[1:]]
        if not body or not all(references):
            raise ValueError('HTML skill 正文、组件、模块说明和规则表不能为空')
        self._version = version
        self._instructions = '\n\n'.join([body, *references[:-1], '## 能力档位规则表\n```json\n' + references[-1] + '\n```'])
        self._template_resources = {}
        for name in _TEMPLATE_FILES:
            try:
                raw = self._resources.joinpath('html_template', name).read_bytes()
            except OSError as exc:
                raise ValueError(f'无法加载 HTML 模板资源: {name}') from exc
            self._template_resources[name] = sha256(raw).hexdigest()
        self._template_fingerprint = sha256(json.dumps(self._template_resources, sort_keys=True).encode('utf-8')).hexdigest()
        self._fingerprint = sha256(json.dumps({
            'files': self._sources, 'template_version': TEMPLATE_VERSION,
            'template_resources': self._template_resources,
        }, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()

    @property
    def name(self) -> str:
        return 'malware-report-html'

    @property
    def roles(self) -> tuple[str, ...]:
        return ('report',)

    @property
    def version(self) -> str:
        return self._version

    @property
    def fingerprint(self) -> str:
        return self._fingerprint

    def instructions(self) -> str:
        return self._instructions

    def snapshot(self) -> dict:
        return {
            'name': self.name, 'roles': list(self.roles), 'version': self.version,
            'fingerprint': self.fingerprint, 'files': deepcopy(self._sources),
            'template_version': TEMPLATE_VERSION, 'template_fingerprint': self._template_fingerprint,
            'template_resources': dict(self._template_resources),
        }

    def compile(
        self, composition: ReportComposition, facts: GlobalReconciliationResult,
        context: dict | None = None, inventory: InputInventory | None = None, *, projection: dict,
        audit: dict | None = None,
    ) -> tuple[dict, list[dict]]:
        """Compile chosen display blocks without modifying facts or source inputs."""
        validate_composition(composition, facts, projection)
        data, fills = prepare_context(context)
        if isinstance(data.get('meta'), dict):
            label = 'HTML 预览'
            edition = data['meta'].get('edition')
            data['meta'].update(
                edition=f'{edition} · {label}' if edition else label,
                status=label, risk=composition.risk, verdict=composition.verdict.text,
                audit=_audit_meta(projection, audit),
            )
            data['meta'].setdefault('title', composition.title)
        claims = {claim.canonical_id: claim for claim in facts.canonical_claims}
        artifacts = {artifact.canonical_id: artifact for artifact in facts.canonical_artifacts}
        notes = {note.artifact_id: note.text for note in composition.ioc_notes}
        ioc = _compile_iocs(projection, artifacts, notes)
        sections = []
        selected = []
        for index, section in enumerate(composition.sections, 1):
            blocks, excerpts = _compile_blocks(section.model_dump(mode='json'), claims, artifacts, projection)
            sections.append({
                'id': f'mechanism-{index}', 'title': section.title,
                **({'question': section.question} if section.question else {}), 'blocks': blocks,
            })
            selected.extend(excerpts)
        data.update({
            'schema_version': '1.1',
            'findings': [{
                'rule': finding.rule, 'title': finding.title,
                'state': 'partial' if any(claims[cid].evidence_level == EvidenceLevel.SUSPECTED
                                          for cid in finding.claim_ids) else 'confirmed',
                'evidence': list(dict.fromkeys(ref.location for cid in finding.claim_ids for ref in claims[cid].evidence)),
                **({'state_note': finding.state_note} if finding.state_note else {}),
                **({'section': f'mechanism-{finding.section_index + 1}'} if finding.section_index is not None else {}),
                **({'tier': finding.tier, 'category': finding.category, 'tier_reason': finding.tier_reason}
                   if finding.rule == 'CUSTOM' else {}),
            } for finding in composition.findings],
            'summary': [item.text for item in composition.summary],
            'risk_judgement': composition.risk_judgement.text,
            'chain': [{
                'name': step.name, 'action': step.action,
                'evidence': list(dict.fromkeys(ref.location for cid in step.claim_ids for ref in claims[cid].evidence)),
                'key': step.key, **({'phase': step.phase} if step.phase is not None else {}),
                **({'brief': step.brief} if step.brief else {}),
            } for step in composition.chain],
            'sections': sections, 'ioc': ioc,
            'detection': [{'type': 'steps', 'items': [{'title': item.title, 'text': item.text} for item in composition.detection]}],
            'response': [{'title': item.title, 'text': item.text} for item in composition.response],
            'gaps': [{'gap': item.text, 'impact': item.impact, 'blocking': item.blocking} for item in composition.gaps],
            'functions': _function_index(selected, inventory), 'final': composition.final.text,
        })
        return data, fills

    def validate(self, data: dict) -> tuple[list[str], list[str]]:
        return validate_report(data)

    def render(self, data: dict) -> str:
        return render_html(data)
