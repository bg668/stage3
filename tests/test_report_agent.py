"""Report-only role loading and one-request composition, without network access."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic_ai import PartStartEvent, UnexpectedModelBehavior
from pydantic_ai.messages import TextPart
from pydantic_ai.usage import RunUsage

from bounded_investigator.config import load_config
from bounded_investigator.execution.stage_runner import RepairRuntime, StageOutcome
from bounded_investigator.schemas import DeliveryStatus
from factories import report_canonical as canonical


ROLE = """---
name: report
metadata:
  version: '1.0'
---
报告职责。
{{skill:malware-report-html}}
完成报告。
"""


class FixedSkill:
    name = 'malware-report-html'
    roles = ('report',)
    version = '1.0'
    fingerprint = 'fixed-skill-fingerprint'

    def instructions(self):
        return '固定技能指令。\n组件规则。'

    def snapshot(self):
        return {'name': self.name, 'roles': list(self.roles), 'version': self.version,
                'fingerprint': self.fingerprint, 'template_version': '1.0',
                'files': {'SKILL.md': '固定技能原文', 'references/blocks.md': '组件原文'}}


def config():
    settings = load_config('config.example.yaml')
    settings.format_repair.enabled = False
    return settings


def test_role_expands_skill_once_at_its_explicit_location():
    from bounded_investigator.reporting.agent import assemble_report_instructions
    assert assemble_report_instructions(ROLE, FixedSkill()) == (
        '报告职责。\n固定技能指令。\n组件规则。\n完成报告。')


@pytest.mark.parametrize('role', [
    ROLE.replace('{{skill:malware-report-html}}', ''),
    ROLE.replace('{{skill:malware-report-html}}', '{{skill:missing}}'),
    ROLE.replace('{{skill:malware-report-html}}', '{{skill:malware-report-html}}\n{{skill:malware-report-html}}'),
    ROLE.replace('{{skill:malware-report-html}}', '行内 {{skill:malware-report-html}}'),
    ROLE.replace('name: report', 'name: reviewer'),
])
def test_missing_duplicate_or_invalid_role_reference_is_rejected(role):
    from bounded_investigator.reporting.agent import assemble_report_instructions
    with pytest.raises(ValueError):
        assemble_report_instructions(role, FixedSkill())


def test_cross_role_skill_is_rejected():
    from bounded_investigator.reporting.agent import assemble_report_instructions
    skill = FixedSkill()
    skill.roles = ('reviewer',)
    with pytest.raises(ValueError, match='report'):
        assemble_report_instructions(ROLE, skill)


def test_skill_nested_reference_is_rejected():
    from bounded_investigator.reporting.agent import assemble_report_instructions
    skill = FixedSkill()
    skill.instructions = lambda: '{{skill:another}}'
    with pytest.raises(ValueError, match='嵌套'):
        assemble_report_instructions(ROLE, skill)


def test_constructor_loads_bundled_role_without_building_clients(monkeypatch):
    from bounded_investigator.reporting import agent
    monkeypatch.setattr(agent, 'build_stage_agent', lambda **kw: pytest.fail('client built in constructor'))
    monkeypatch.setattr(agent, 'build_text_stage_agent', lambda **kw: pytest.fail('repair client built in constructor'))
    reporter = agent.ReportAgent(config(), FixedSkill())
    snapshot = reporter.role_snapshot
    assert snapshot['name'] == 'report' and snapshot['version'] == '1.3'
    assert '全部' in snapshot['instructions'] and '尚未确认' in snapshot['instructions']
    assert '固定技能指令。' in snapshot['instructions']
    assert snapshot['skill']['files']['references/blocks.md'] == '组件原文'
    assert snapshot['fingerprint'] != FixedSkill.fingerprint
    assert reporter.last_outcome is None


def fixed_document():
    from bounded_investigator.reporting.composition import ReportComposition
    cited = {'text': '样本读取凭据并处理结果。', 'claim_ids': ['G:C001']}
    return ReportComposition.model_validate({
        'title': '凭据处理样本', 'findings': [],
        'summary': [cited], 'verdict': cited, 'risk': 'medium', 'risk_judgement': cited,
        'sections': [{'title': '凭据如何被获取并处理', 'blocks': [
            {'type': 'paragraph', **cited},
            {'type': 'kv', 'items': [{'label': '输出对象', 'value': {'fact_id': 'G:A001', 'field': 'value'}}]},
        ]}],
        'detection': [{'title': '关联核查', **cited}],
        'response': [{'title': '保全', **cited}], 'final': cited,
    })


def install_fixed_execution(monkeypatch, *, raw=None, failure=None, repaired=None):
    from bounded_investigator.reporting import agent
    calls, builds = [], []

    async def run(prompt, **kwargs):
        calls.append(('compose', prompt, kwargs['usage_limits']))
        kwargs['usage'].requests += 1
        if raw is not None:
            async def events():
                yield PartStartEvent(index=0, part=TextPart(content=raw))
            await kwargs['event_stream_handler'](None, events())
        if failure is not None:
            raise failure
        return SimpleNamespace(output=fixed_document())

    async def repair(prompt, **kwargs):
        calls.append(('repair', prompt, kwargs['usage_limits']))
        kwargs['usage'].requests += 1
        return SimpleNamespace(output=repaired)

    def build(**kwargs):
        builds.append(kwargs)
        return SimpleNamespace(stage='report_composition', schema=kwargs['schema'],
                               agent=SimpleNamespace(run=run),
                               model_config=SimpleNamespace(stream_output=False, output_mode='tool'))

    monkeypatch.setattr(agent, 'build_stage_agent', build)
    monkeypatch.setattr(agent, 'build_text_stage_agent', lambda **kwargs: SimpleNamespace(
        stage='format_repair', agent=SimpleNamespace(run=repair),
        model_config=SimpleNamespace(stream_output=False)))
    monkeypatch.setattr(agent, 'validate_composition', lambda document, facts, projection: None)
    return calls, builds


def test_compose_executes_once_with_report_budget_and_saves_separate_snapshot(monkeypatch, tmp_path):
    from bounded_investigator.reporting import agent
    calls, builds = install_fixed_execution(monkeypatch)
    reporter = agent.ReportAgent(config(), FixedSkill())
    assert builds == []
    usage, repair = RunUsage(), RepairRuntime()
    facts = canonical()
    document = asyncio.run(reporter.compose(facts=facts, projection={}, packet_name='sample',
        artifact_dir=tmp_path, usage=usage, repair_runtime=repair))
    assert document == fixed_document()
    assert len(calls) == 1 and calls[0][0] == 'compose'
    assert calls[0][2].request_limit == 1 and calls[0][2].total_tokens_limit == 64000
    assert usage.requests == 1 and repair.usage.requests == 0
    assert 'G:C001' in calls[0][1] and 'packet_dag' not in calls[0][1]
    assert builds[0]['instructions'] == reporter.role_snapshot['instructions']
    assert reporter.last_outcome.status == DeliveryStatus.SUCCESS
    saved = json.loads((tmp_path / 'report_agent_snapshot.json').read_text(encoding='utf-8'))
    assert saved == reporter.role_snapshot
    assert saved['skill']['files']['SKILL.md'] == '固定技能原文'
    assert (tmp_path / 'composition_input.json').exists()
    assert (tmp_path / 'report_composition/validated_result.json').exists()
    # A caller cannot mutate the already consumed role snapshot.
    reporter.role_snapshot['instructions'] = 'mutated'
    assert reporter.role_snapshot == saved


def test_format_repair_has_its_own_usage_and_request_limit(monkeypatch, tmp_path):
    from bounded_investigator.reporting import agent
    document = fixed_document()
    broken = document.model_dump(mode='json')
    broken['risk'] = 9
    calls, _ = install_fixed_execution(monkeypatch, raw=json.dumps(broken),
        failure=UnexpectedModelBehavior('structured output validation error'),
        repaired=document.model_dump_json())
    settings = config()
    settings.format_repair.enabled = True
    settings.format_repair.max_attempts = 2
    reporter = agent.ReportAgent(settings, FixedSkill())
    usage, repair = RunUsage(), RepairRuntime()
    output = asyncio.run(reporter.compose(facts=canonical(), projection={}, packet_name='sample',
        artifact_dir=tmp_path, usage=usage, repair_runtime=repair))
    assert output == document
    assert [c[0] for c in calls] == ['compose', 'repair']
    assert calls[1][2].request_limit == 2
    assert calls[1][2].total_tokens_limit == settings.format_repair.total_tokens_limit
    assert usage.requests == 1 and repair.usage.requests == 1
    assert reporter.last_outcome.status == DeliveryStatus.SUCCESS_AFTER_REPAIR


def test_model_failure_keeps_input_snapshot_and_diagnostic(monkeypatch, tmp_path):
    from bounded_investigator.reporting import agent
    install_fixed_execution(monkeypatch, failure=ConnectionError('fixed offline failure'))
    reporter = agent.ReportAgent(config(), FixedSkill())
    with pytest.raises(ConnectionError, match='fixed offline failure'):
        asyncio.run(reporter.compose(facts=canonical(), projection={}, packet_name='sample',
            artifact_dir=tmp_path, usage=RunUsage(), repair_runtime=RepairRuntime()))
    failure = json.loads((tmp_path / 'report_agent_failure.json').read_text(encoding='utf-8'))
    assert failure == {'type': 'ConnectionError', 'message': 'fixed offline failure'}
    assert (tmp_path / 'report_agent_snapshot.json').exists()
    assert (tmp_path / 'composition_input.json').exists()


def test_incomplete_stage_is_rejected_without_second_composition(monkeypatch, tmp_path):
    from bounded_investigator.reporting import agent
    calls, _ = install_fixed_execution(monkeypatch)
    async def incomplete(self, **kwargs):
        return StageOutcome(status=DeliveryStatus.RAW_ONLY)
    monkeypatch.setattr(agent.StageRunner, 'run', incomplete)
    reporter = agent.ReportAgent(config(), FixedSkill())
    with pytest.raises(ValueError, match='complete result'):
        asyncio.run(reporter.compose(facts=canonical(), projection={}, packet_name='sample',
            artifact_dir=tmp_path, usage=RunUsage(), repair_runtime=RepairRuntime()))
    assert calls == [] and reporter.last_outcome.status == DeliveryStatus.RAW_ONLY
    assert (tmp_path / 'report_agent_failure.json').exists()


@pytest.mark.parametrize('mode', ['tool', 'native', 'prompted'])
def test_snapshot_matches_actual_model_instructions_in_all_output_modes(monkeypatch, tmp_path, mode):
    from pydantic_ai.models.test import TestModel
    from bounded_investigator.execution import llm
    from bounded_investigator.reporting import agent
    settings = config()
    model_key = settings.stages['report_composition'].model
    settings.models[model_key].output_mode = mode
    monkeypatch.setattr(llm, 'build_model', lambda *args: TestModel())
    actual = []

    async def capture(self, **kwargs):
        actual.append('\n'.join(item.instruction for item in kwargs['stage_agent'].agent._instructions))
        return StageOutcome(status=DeliveryStatus.SUCCESS, output=fixed_document())

    monkeypatch.setattr(agent.StageRunner, 'run', capture)
    reporter = agent.ReportAgent(settings, FixedSkill())
    asyncio.run(reporter.compose(facts=canonical(), projection={}, packet_name='sample',
        artifact_dir=tmp_path, usage=RunUsage(), repair_runtime=RepairRuntime()))
    snapshot = reporter.role_snapshot
    assert snapshot['instructions'] == actual[0]
    if mode != 'tool':
        assert snapshot['instructions'].count('Return exactly one valid JSON object') == 1
        assert 'Return exactly one valid JSON object' not in snapshot['expanded_instructions']


def test_missing_skill_resource_rejects_before_client_construction(monkeypatch):
    from bounded_investigator.reporting import agent
    skill = FixedSkill()
    def missing():
        raise FileNotFoundError('references/blocks.md')
    skill.instructions = missing
    monkeypatch.setattr(agent, 'build_stage_agent', lambda **kwargs: pytest.fail('unexpected model client'))
    with pytest.raises(FileNotFoundError, match='blocks.md'):
        agent.ReportAgent(config(), skill)


def test_snapshot_write_failure_does_not_call_model(monkeypatch, tmp_path):
    from bounded_investigator.reporting import agent
    calls, _ = install_fixed_execution(monkeypatch)
    original = Path.write_text
    def deny_snapshot(path, data, **kwargs):
        if path.name == 'report_agent_snapshot.json':
            raise PermissionError('snapshot fixture')
        return original(path, data, **kwargs)
    monkeypatch.setattr(Path, 'write_text', deny_snapshot)
    reporter = agent.ReportAgent(config(), FixedSkill())
    with pytest.raises(PermissionError, match='snapshot fixture'):
        asyncio.run(reporter.compose(facts=canonical(), projection={}, packet_name='sample',
            artifact_dir=tmp_path, usage=RunUsage(), repair_runtime=RepairRuntime()))
    assert calls == [] and (tmp_path / 'report_agent_failure.json').exists()


def test_compose_timeout_is_diagnostic_and_does_not_retry(monkeypatch, tmp_path):
    from bounded_investigator.reporting import agent
    install_fixed_execution(monkeypatch)
    calls = []
    async def slow(self, **kwargs):
        calls.append(kwargs)
        await asyncio.sleep(1)
    monkeypatch.setattr(agent.StageRunner, 'run', slow)
    settings = config()
    settings.html_report.wall_clock_seconds = .01
    reporter = agent.ReportAgent(settings, FixedSkill())
    with pytest.raises(TimeoutError):
        asyncio.run(reporter.compose(facts=canonical(), projection={}, packet_name='sample',
            artifact_dir=tmp_path, usage=RunUsage(), repair_runtime=RepairRuntime()))
    assert len(calls) == 1 and reporter.last_outcome is None
    assert json.loads((tmp_path / 'report_agent_failure.json').read_text(encoding='utf-8'))['type'] == 'TimeoutError'


def test_invalid_composition_is_rejected_with_existing_stage_artifacts(monkeypatch, tmp_path):
    from bounded_investigator.reporting import agent
    calls, _ = install_fixed_execution(monkeypatch)
    def reject(document, facts, projection):
        assert projection == {'marker': 'projection'}
        raise ValueError('invalid canonical reference')
    monkeypatch.setattr(agent, 'validate_composition', reject)
    reporter = agent.ReportAgent(config(), FixedSkill())
    with pytest.raises(ValueError, match='invalid canonical reference'):
        asyncio.run(reporter.compose(facts=canonical(), projection={'marker': 'projection'}, packet_name='sample',
            artifact_dir=tmp_path, usage=RunUsage(), repair_runtime=RepairRuntime()))
    assert len(calls) == 1 and reporter.last_outcome.status == DeliveryStatus.SUCCESS
    assert (tmp_path / 'report_composition/validated_result.json').exists()
    assert (tmp_path / 'report_agent_failure.json').exists()


def test_real_html_skill_is_loaded_only_into_report_role():
    from bounded_investigator.harness.loader import load_harness
    from bounded_investigator.reporting.agent import ReportAgent
    from bounded_investigator.reporting.html_skill import HtmlReportSkill
    skill = HtmlReportSkill()
    reporter = ReportAgent(config(), skill)
    snapshot = reporter.role_snapshot
    assert '{{skill:' not in snapshot['expanded_instructions']
    assert skill.instructions() in snapshot['expanded_instructions']
    assert snapshot['skill']['fingerprint'] == skill.fingerprint
    assert set(snapshot['skill']['files']) == {
        'skills/malware-report-html/SKILL.md',
        'skills/malware-report-html/references/blocks.md',
        'skills/malware-report-html/references/modules.md',
        'html_template/assets/tier_rules.json',
    }
    harness = load_harness()
    for role in ('analyzer', 'reviewer', 'reviser'):
        assert skill.instructions() not in harness.instructions(role)
