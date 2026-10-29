from __future__ import annotations

from copy import deepcopy
from html.parser import HTMLParser
import json
from pathlib import Path
import re
from zipfile import ZipFile

import pytest


ARCHIVE = Path(__file__).resolve().parents[1] / 'src/malware-report-html-1.1.zip'
PREFIX = 'malware-report-html/'
OLD_FOOTER = '本报告仅基于提供的分析输入；运行时行为以隔离环境动态分析为准。'
NEW_FOOTER = '行为结论与关键对象可通过函数及行号复核。'
OLD_EMPTY_GAPS = '当前输入足以回答报告涉及的全部问题。'
NEW_EMPTY_GAPS = '当前报告未列出影响主要结论的未决问题。'
OLD_CHAIN_DESCRIPTION = '样本的完整执行路径，每一步均可追溯到证据。'
NEW_CHAIN_DESCRIPTION = '展示有依据的关键行为关系，具体机制见下文章节。'
OLD_EMPTY_CHAIN = '当前输入无法串联出可达的执行路径；已确认的单项能力见下文各章节。'
NEW_EMPTY_CHAIN = '当前材料未建立可串联的行为路径；单项行为见下文章节。'


def archive_text(name: str) -> str:
    if not ARCHIVE.exists():
        pytest.skip('Original template archive is not included in installed distributions')
    with ZipFile(ARCHIVE) as archive:
        return archive.read(PREFIX + name).decode('utf-8')


def example(name: str = 'two-step') -> dict:
    return json.loads(archive_text(f'references/examples/{name}.json'))


def original_render(data: dict, *, layout: str = 'auto', toc_on: bool = True) -> str:
    from bounded_investigator.reporting.html_template.scripts import render

    source = archive_text('scripts/render.py')
    source = source.replace('sys.path.insert(0, str(HERE))\n', '')
    source = source.replace('from validate import validate, load_rules, resolve_finding  # noqa: E402',
                            'from bounded_investigator.reporting.html_template.scripts.validate import validate, load_rules, resolve_finding')
    namespace = {'__file__': render.__file__, '__name__': 'original_template_render'}
    exec(compile(source, 'original_template_render', 'exec'), namespace)
    return (namespace['render'](data, layout, toc_on)
            .replace(OLD_EMPTY_GAPS, NEW_EMPTY_GAPS)
            .replace(OLD_CHAIN_DESCRIPTION, NEW_CHAIN_DESCRIPTION)
            .replace(OLD_EMPTY_CHAIN, NEW_EMPTY_CHAIN))


@pytest.mark.parametrize('name', ['two-step', '07b8-full'])
def test_original_examples_preserve_template_except_approved_copy(name):
    from bounded_investigator.reporting.html_template import render_html, validate_report

    data = example(name)
    errors, warnings = validate_report(data)
    assert errors == []
    assert isinstance(warnings, list)
    html = render_html(data)
    assert html == original_render(data).replace(OLD_FOOTER, NEW_FOOTER)
    assert OLD_FOOTER not in html


def test_css_is_identical_to_original_archive():
    from importlib.resources import files

    css = files('bounded_investigator.reporting.html_template').joinpath('assets/report.css').read_bytes()
    if not ARCHIVE.exists():
        pytest.skip('Original template archive is not included in installed distributions')
    with ZipFile(ARCHIVE) as archive:
        assert css == archive.read(PREFIX + 'assets/report.css')


def test_large_chain_uses_original_vertical_layout_and_explicit_override():
    from bounded_investigator.reporting.html_template import render_html

    data = example()
    data['chain'] = [deepcopy(data['chain'][0]) for _ in range(9)]
    auto = render_html(data)
    assert auto == original_render(data).replace(OLD_FOOTER, NEW_FOOTER)
    assert '<div class="vflow">' in auto
    assert '<div class="flow-scroll">' not in auto
    horizontal = render_html(data, layout='horizontal')
    assert '<div class="flow-scroll">' in horizontal
    assert horizontal == original_render(data, layout='horizontal').replace(OLD_FOOTER, NEW_FOOTER)


def test_empty_states_and_fixed_sections_remain():
    from bounded_investigator.reporting.html_template import render_html, validate_report

    data = example()
    data.update(chain=[], sections=[], attack=[], attribution=None, gaps=[], functions=[], yara=None)
    data['ioc'] = dict(host=[], network=[], host_empty_reason='未提供主机指标。', network_empty_reason='未提供网络指标。')
    html = render_html(data)
    assert validate_report(data)[0] == []
    assert validate_report(data)[1]
    for sid in ('summary', 'ioc', 'detection', 'response', 'attribution', 'gaps', 'final'):
        assert f'<section id="{sid}"' in html
    for label in ('未建立行为链', '无主机 IOC', '无网络 IOC', '未归因', '无未决缺口'):
        assert label in html
    assert NEW_EMPTY_GAPS in html
    assert OLD_EMPTY_GAPS not in html
    assert '<nav class="toc"' in html
    assert '<nav class="toc"' not in render_html(data, toc_on=False)
    assert html == original_render(data).replace(OLD_FOOTER, NEW_FOOTER)


@pytest.mark.parametrize('has_chain', [True, False])
def test_chain_copy_does_not_claim_complete_execution_or_exclude_uncertain_behaviors(has_chain):
    from bounded_investigator.reporting.html_template import render_html

    data = example()
    if not has_chain:
        data['chain'] = []
    html = render_html(data)
    assert NEW_CHAIN_DESCRIPTION in html
    assert OLD_CHAIN_DESCRIPTION not in html
    assert OLD_EMPTY_CHAIN not in html
    if not has_chain:
        assert NEW_EMPTY_CHAIN in html


def test_text_code_and_embedded_json_escape_markup_without_changing_data():
    from bounded_investigator.reporting.html_template import render_html

    data = example()
    payload = '</script><script src="https://invalid.example/payload.js">&</script>'
    data['meta']['title'] = payload
    data['summary'] = [payload + ' `<img src=x onerror=alert(1)>`']
    data['sections'] = [{'title': '源码', 'blocks': [{'type': 'code', 'text': payload}]}]
    html = render_html(data)
    assert '&lt;/script&gt;&lt;script src=&quot;https://invalid.example/payload.js&quot;&gt;&amp;' in html
    assert '<code>&lt;img src=x onerror=alert(1)&gt;</code>' in html
    parser = ResourceParser()
    parser.feed(html)
    assert parser.resource_tags == []
    embedded = re.search(r'<script type="application/json" id="report-data">\s*(.*?)\s*</script>', html, re.S)
    assert embedded is not None
    assert json.loads(embedded.group(1)) == data


class ResourceParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.resource_tags: list[tuple[str, dict]] = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag in {'link', 'img', 'iframe', 'object', 'embed', 'video', 'audio'} or (tag == 'script' and attributes.get('src')):
            self.resource_tags.append((tag, attributes))


def test_report_embeds_css_and_does_not_load_external_resources():
    from bounded_investigator.reporting.html_template import render_html, TEMPLATE_VERSION

    html = render_html(example('07b8-full'))
    parser = ResourceParser()
    parser.feed(html)
    assert parser.resource_tags == []
    assert '<style>' in html
    assert '@import' not in html
    assert '<meta name="generator" content="malware-report-html 1.1">' in html
    assert TEMPLATE_VERSION == '1.1'


def test_renderer_rejects_invalid_report_instead_of_rendering_it():
    from bounded_investigator.reporting.html_template import render_html, validate_report

    errors, warnings = validate_report({})
    assert errors
    assert isinstance(warnings, list)
    with pytest.raises(ValueError, match='meta'):
        render_html({})


def test_renderer_rejects_invalid_layout_without_changing_data():
    from bounded_investigator.reporting.html_template import render_html

    data = example()
    snapshot = deepcopy(data)
    with pytest.raises(ValueError, match='layout'):
        render_html(data, layout='diagonal')
    assert data == snapshot
