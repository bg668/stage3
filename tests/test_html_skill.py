"""HTML skill is a deterministic, separately usable rendering capability."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest


def test_report_skill_resources_and_template_are_frozen_and_fingerprinted(tmp_path, monkeypatch):
    from bounded_investigator.reporting import html_skill

    root = tmp_path / 'reporting'
    resource = root / 'skills/malware-report-html'
    (resource / 'references').mkdir(parents=True)
    original = '---\nname: malware-report-html\ndescription: HTML rendering\nroles: [report]\nmetadata:\n  version: "1.0"\n---\nChoose blocks for readers.\n'
    (resource / 'SKILL.md').write_bytes(original.encode('utf-8'))
    (resource / 'references/blocks.md').write_text('Block reference.\n', encoding='utf-8')
    (resource / 'references/modules.md').write_text('Modules.', encoding='utf-8')
    template = Path(html_skill.__file__).parent / 'html_template'
    for name in ('__init__.py', 'scripts/render.py', 'scripts/validate.py', 'assets/report.css', 'assets/tier_rules.json'):
        target = root / 'html_template' / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((template / name).read_bytes())
    monkeypatch.setattr(html_skill, 'files', lambda package: root)
    skill = html_skill.HtmlReportSkill()
    snapshot = skill.snapshot()
    assert skill.name == 'malware-report-html'
    assert skill.roles == ('report',)
    assert skill.version == '1.0'
    assert 'Choose blocks for readers.' in skill.instructions()
    assert 'Block reference.' in skill.instructions()
    assert snapshot['files']['skills/malware-report-html/SKILL.md'] == original
    assert snapshot['template_version'] == '1.1'
    assert snapshot['template_resources']['assets/report.css'] == hashlib.sha256((template / 'assets/report.css').read_bytes()).hexdigest()
    original_fingerprint = skill.fingerprint
    (resource / 'references/blocks.md').write_text('Changed reference.', encoding='utf-8')
    assert skill.snapshot() == snapshot
    assert skill.fingerprint == original_fingerprint
    assert html_skill.HtmlReportSkill().fingerprint != original_fingerprint
    changed = skill.snapshot()
    changed['files'].clear()
    assert skill.snapshot() == snapshot


@pytest.mark.parametrize('change,match', [
    ('roles: [report]', 'roles'),
    ('name: malware-report-html', 'name'),
    ('description: HTML rendering', 'description'),
    ('  version: "1.0"', 'version'),
])
def test_invalid_skill_metadata_is_rejected_before_model_use(tmp_path, monkeypatch, change, match):
    from bounded_investigator.reporting import html_skill

    root = tmp_path / 'reporting'
    resource = root / 'skills/malware-report-html'
    (resource / 'references').mkdir(parents=True)
    text = '---\nname: malware-report-html\ndescription: HTML rendering\nroles: [report]\nmetadata:\n  version: "1.0"\n---\nPolicy.\n'
    replacement = 'roles: [analyzer]' if change.startswith('roles') else ''
    (resource / 'SKILL.md').write_text(text.replace(change, replacement), encoding='utf-8')
    (resource / 'references/blocks.md').write_text('Blocks.', encoding='utf-8')
    (resource / 'references/modules.md').write_text('Modules.', encoding='utf-8')
    rules = root / 'html_template/assets/tier_rules.json'
    rules.parent.mkdir(parents=True, exist_ok=True)
    rules.write_text('{}', encoding='utf-8')
    monkeypatch.setattr(html_skill, 'files', lambda package: root)
    with pytest.raises(ValueError, match=match):
        html_skill.HtmlReportSkill()


def test_missing_skill_reference_is_rejected(tmp_path, monkeypatch):
    from bounded_investigator.reporting import html_skill

    monkeypatch.setattr(html_skill, 'files', lambda package: tmp_path)
    with pytest.raises(ValueError, match='SKILL.md'):
        html_skill.HtmlReportSkill()


def test_skill_compiles_validates_and_renders_without_model_dependencies():
    from bounded_investigator.reporting.html_skill import HtmlReportSkill
    from bounded_investigator.reporting.composition import ReportComposition
    from test_html_composition import composition_dict, facts, report_projection

    skill = HtmlReportSkill()
    canonical = facts()
    data, fills = skill.compile(ReportComposition.model_validate(composition_dict()), canonical, projection=report_projection(canonical))
    assert skill.validate(data)[0] == []
    html = skill.render(data)
    assert '<!doctype html>' in html
    assert fills == []
    assert 'id="report-data"' in html
    assert 'G:C001' not in html and 'J00:C001' not in html and 'G:A001' not in html


@pytest.mark.parametrize('kind', ['list', 'tags', 'kv', 'cards', 'steps', 'table'])
@pytest.mark.parametrize('limitations', [[], ['仅静态分析']])
def test_empty_bound_content_cannot_count_as_presented_behavior(kind, limitations):
    from bounded_investigator.reporting.composition import ReportComposition
    from bounded_investigator.reporting.html_skill import HtmlReportSkill
    from test_html_composition import composition_dict, facts, report_projection

    canonical = facts()
    canonical.canonical_claims[0].limitations = limitations
    raw = composition_dict()
    bound = {'fact_id': 'G:C001', 'field': 'limitations'}
    if kind == 'table':
        block = {'type': kind, 'columns': [{'label': '边界', 'field': 'limitations'}], 'fact_ids': ['G:C001']}
    elif kind == 'kv':
        block = {'type': kind, 'items': [{'label': '边界', 'value': bound}]}
    elif kind in {'cards', 'steps'}:
        block = {'type': kind, 'items': [{'title': '边界', 'content': bound}]}
    else:
        block = {'type': kind, 'items': [bound]}
    raw['sections'][0]['blocks'] = [block]
    with pytest.raises(ValueError, match='空'):
        HtmlReportSkill().compile(ReportComposition.model_validate(raw), canonical, projection=report_projection(canonical))


def test_one_nonempty_table_row_does_not_hide_an_empty_unconfirmed_behavior():
    from bounded_investigator.reporting.composition import ReportComposition
    from bounded_investigator.reporting.html_skill import HtmlReportSkill
    from bounded_investigator.schemas import EvidenceLevel
    from test_html_composition import composition_dict, facts, report_projection

    canonical = facts()
    second = canonical.canonical_claims[0].model_copy(deep=True, update={
        'canonical_id': 'G:C002', 'behavior': '可能调用附加处理例程',
        'evidence_level': EvidenceLevel.SUSPECTED, 'limitations': [],
    })
    canonical.canonical_claims[0].limitations = ['配置键表未恢复']
    canonical.canonical_claims.append(second)
    raw = composition_dict()
    raw['sections'][0]['blocks'] = [{
        'type': 'table', 'columns': [{'label': '边界', 'field': 'limitations'}],
        'fact_ids': ['G:C001', 'G:C002'],
    }]
    with pytest.raises(ValueError, match='空'):
        HtmlReportSkill().compile(ReportComposition.model_validate(raw), canonical, projection=report_projection(canonical))

