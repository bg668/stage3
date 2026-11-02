"""Shared budgets operate at provider request boundaries, including retries."""

import asyncio
import logging
from types import SimpleNamespace

import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.usage import RequestUsage, RunUsage, UsageLimits


def test_request_boundary_preserves_crossing_response_and_rejects_next(tmp_path):
    from bounded_investigator.execution.sample_budget import BudgetedModel, SampleBudget, SampleBudgetExceeded

    settings = []

    async def respond(messages, info):
        settings.append(info.model_settings)
        return ModelResponse(parts=[TextPart('saved result')], usage=RequestUsage(input_tokens=65, output_tokens=20,
                              cache_read_tokens=12, details={'reasoning_tokens': 5}))

    budget = SampleBudget(total_tokens=100, reserve_tokens=20, ledger_path=tmp_path / 'budget.json')
    agent = Agent(BudgetedModel(FunctionModel(respond), budget=budget, phase='analysis', packet_name='J00', stage='analyzer'))

    async def run():
        result = await agent.run('material', model_settings={'max_tokens': 90})
        assert result.output == 'saved result'
        with pytest.raises(SampleBudgetExceeded):
            await agent.run('must not start')

    asyncio.run(run())
    assert len(settings) == 1 and settings[0]['max_tokens'] == 80
    assert budget.remaining('analysis') == 0 and budget.remaining('global') == 15
    ledger = budget.to_dict()
    assert ledger['total_tokens'] == 85 and ledger['analysis_excess_tokens'] == 5
    assert ledger['output_tokens'] == 20 and ledger['cache_read_tokens'] == 12
    assert ledger['reasoning_tokens'] == 5 and ledger['requests'] == 1
    assert (tmp_path / 'budget.json').is_file()


def test_global_and_repair_use_same_sample_ledger():
    from bounded_investigator.execution.sample_budget import BudgetedModel, SampleBudget

    async def respond(messages, info):
        return ModelResponse(parts=[TextPart('ok')], usage=RequestUsage(input_tokens=8, output_tokens=2))

    budget = SampleBudget(total_tokens=40, reserve_tokens=10)
    async def run():
        for phase, stage in [('analysis', 'analyzer'), ('analysis', 'format_repair'), ('global', 'global_reconciliation')]:
            await Agent(BudgetedModel(FunctionModel(respond), budget=budget, phase=phase, packet_name='J', stage=stage)).run('x')
    asyncio.run(run())
    assert budget.to_dict()['requests'] == 3 and budget.remaining('global') == 10
    assert budget.to_dict()['records'][1]['stage'] == 'format_repair'


def test_failed_request_marks_unknown_usage_and_keeps_known_failure_usage():
    from bounded_investigator.execution.sample_budget import BudgetedModel, SampleBudget

    async def fail(messages, info):
        raise ConnectionError('fixture failure')
    budget = SampleBudget(total_tokens=100, reserve_tokens=0)
    async def run():
        with pytest.raises(ConnectionError):
            await Agent(BudgetedModel(FunctionModel(fail), budget=budget, phase='analysis', packet_name='J', stage='analyzer')).run('x')
    asyncio.run(run())
    assert budget.to_dict()['unknown_usage_requests'] == 1
    assert budget.to_dict()['records'][0]['status'] == 'failed'


def test_stage_budget_stop_is_not_format_repair(tmp_path):
    from bounded_investigator.execution.sample_budget import SampleBudget
    from bounded_investigator.execution.stage_runner import RepairRuntime, StageRunner
    from bounded_investigator.schemas import DeliveryStatus, InvestigationResult

    calls = []
    async def respond(messages, info):
        calls.append(1)
        return ModelResponse(parts=[TextPart('must not call')])
    stage = SimpleNamespace(stage='reviewer', schema=InvestigationResult, agent=Agent(FunctionModel(respond)),
                            model_config=SimpleNamespace(stream_output=False, output_mode='tool'))
    budget = SampleBudget(total_tokens=10, reserve_tokens=10)
    runner = StageRunner(config=SimpleNamespace(), format_repair=None, log=logging.getLogger('test.budget'), sample_budget=budget)
    runtime = RepairRuntime()
    result = asyncio.run(runner.run(stage_agent=stage, prompt='x', packet_name='J00', usage=RunUsage(),
                                    limits=UsageLimits(), repair_runtime=runtime, artifact_dir=tmp_path))
    assert result.status == DeliveryStatus.RAW_ONLY and not calls and runtime.triggered == 0
    assert result.validation_errors[0]['type'] == 'sample_budget_exceeded'


def test_validation_retry_is_denied_at_provider_boundary():
    from bounded_investigator.execution.sample_budget import BudgetedModel, SampleBudget, SampleBudgetExceeded

    calls = []
    async def invalid(messages, info):
        calls.append(1)
        return ModelResponse(parts=[TextPart('wrong type')], usage=RequestUsage(input_tokens=75, output_tokens=10))
    budget = SampleBudget(total_tokens=100, reserve_tokens=20)
    agent = Agent(BudgetedModel(FunctionModel(invalid), budget=budget, phase='analysis', packet_name='J', stage='analyzer'),
                  output_type=int, retries=2)
    with pytest.raises(SampleBudgetExceeded):
        asyncio.run(agent.run('x'))
    assert calls == [1] and budget.to_dict()['requests'] == 1


def test_stream_failure_accounts_partial_known_usage():
    from bounded_investigator.execution.sample_budget import BudgetedModel, SampleBudget

    async def partial(messages, info):
        yield 'partial text with measurable usage'
        raise TimeoutError('fixture timeout')
    budget = SampleBudget(total_tokens=100, reserve_tokens=0)
    agent = Agent(BudgetedModel(FunctionModel(stream_function=partial), budget=budget, phase='analysis',
                                packet_name='J', stage='analyzer'))
    async def run():
        async with agent.run_stream('x') as result:
            async for _ in result.stream_text():
                pass
    with pytest.raises(TimeoutError):
        asyncio.run(run())
    ledger = budget.to_dict()
    assert ledger['requests'] == 1 and ledger['output_tokens'] > 0
    assert ledger['records'][0]['status'] == 'failed' and ledger['unknown_usage_requests'] == 0


def test_global_budget_stop_never_creates_fallback_success(tmp_path):
    from bounded_investigator.execution.sample_budget import SampleBudget, SampleBudgetExceeded
    from bounded_investigator.execution.stage_runner import RepairRuntime, StageRunner

    stage = SimpleNamespace(stage='global_reconciliation', schema=None, model_config=SimpleNamespace(stream_output=False, output_mode='tool'))
    budget = SampleBudget(total_tokens=10, reserve_tokens=0)
    budget.record(phase='analysis', packet_name='J', stage='analyzer', started=0,
                  usage=RequestUsage(input_tokens=10), status='completed')
    runner = StageRunner(config=SimpleNamespace(), format_repair=None, log=logging.getLogger('test.budget'), sample_budget=budget)
    with pytest.raises(SampleBudgetExceeded):
        asyncio.run(runner.run(stage_agent=stage, prompt='x', packet_name='GLOBAL', usage=RunUsage(),
                               limits=UsageLimits(), repair_runtime=RepairRuntime(), artifact_dir=tmp_path))


def test_workflow_preserves_crossing_analyzer_draft_without_reviewer(monkeypatch, tmp_path):
    from pydantic_ai.models.function import DeltaToolCall
    from bounded_investigator import workflow
    from bounded_investigator.config import load_config
    from bounded_investigator.execution.sample_budget import SampleBudget
    from bounded_investigator.schemas import InvestigationResult

    output = InvestigationResult(status='complete', summary='validated draft')
    calls = []
    async def stream(messages, info):
        calls.append(1)
        yield {0: DeltaToolCall(name=info.output_tools[0].name, json_args=output.model_dump_json())}
    def stage(**kw):
        return SimpleNamespace(stage=kw['stage'], model_key='fixture', model_config=SimpleNamespace(
            stream_output=False, output_mode='tool', provider='fixture', model_name='fixture', api_mode='chat', reasoning='omit'),
            schema=kw['schema'], agent=Agent(FunctionModel(stream_function=stream), output_type=kw['schema']))
    config = load_config('config.example.yaml')
    config.format_repair.enabled = False
    monkeypatch.setattr(workflow, 'build_stage_agent', stage)
    budget = SampleBudget(total_tokens=10, reserve_tokens=0)
    runner = workflow.BoundedInvestigationWorkflow(config, sample_budget=budget)
    result = asyncio.run(runner.run(packet_name='J00', packet_text='material', artifact_dir=tmp_path))
    assert result.result.summary == 'validated draft' and not result.review_completed
    assert len(calls) == 1 and budget.to_dict()['excess_tokens'] > 0
    assert (tmp_path / 'analyzer/validated_result.json').is_file()
    assert result.validation_errors['reviewer'][0]['type'] == 'sample_budget_exceeded'


def test_absent_provider_usage_is_unknown_and_failure_usage_is_retained():
    from bounded_investigator.execution.sample_budget import BudgetedModel, SampleBudget

    budget = SampleBudget(total_tokens=100, reserve_tokens=0)
    budget.record(phase='analysis', packet_name='J', stage='analyzer', started=0,
                  usage=RequestUsage(), status='completed')
    assert budget.to_dict()['unknown_usage_requests'] == 1
    class FailedModel(FunctionModel):
        async def request(self, *args, **kwargs):
            failure = TimeoutError('known partial usage')
            failure.usage = RequestUsage(input_tokens=7, output_tokens=3)
            raise failure
    async def placeholder(*args):
        raise AssertionError('unused')
    agent = Agent(BudgetedModel(FailedModel(placeholder), budget=budget, phase='analysis', packet_name='J', stage='analyzer'))
    with pytest.raises(TimeoutError):
        asyncio.run(agent.run('x'))
    assert budget.to_dict()['total_tokens'] == 10 and budget.to_dict()['unknown_usage_requests'] == 1


def test_crossing_malformed_response_does_not_trigger_repair(tmp_path):
    from bounded_investigator.execution.sample_budget import SampleBudget
    from bounded_investigator.execution.stage_runner import RepairRuntime, StageRunner
    from bounded_investigator.schemas import InvestigationResult

    async def malformed(messages, info):
        yield 'not valid JSON material'
    calls = []
    async def forbidden(messages, info):
        calls.append(1)
        yield 'must not repair'
    cfg = SimpleNamespace(format_repair=SimpleNamespace(max_attempts=2, total_tokens_limit=1000),
                          workflow=SimpleNamespace(max_model_requests=3))
    model_cfg = SimpleNamespace(stream_output=False, output_mode='tool')
    stage = SimpleNamespace(stage='analyzer', schema=InvestigationResult, model_config=model_cfg,
                            agent=Agent(FunctionModel(stream_function=malformed), output_type=InvestigationResult, retries=0))
    repair = SimpleNamespace(stage='format_repair', schema=None, model_config=model_cfg,
                             agent=Agent(FunctionModel(stream_function=forbidden)))
    runtime = RepairRuntime()
    runner = StageRunner(config=cfg, format_repair=repair, log=logging.getLogger('test.budget'),
                          sample_budget=SampleBudget(total_tokens=1, reserve_tokens=0))
    result = asyncio.run(runner.run(stage_agent=stage, prompt='x', packet_name='J', usage=RunUsage(),
                                    limits=UsageLimits(), repair_runtime=runtime, artifact_dir=tmp_path))
    assert not calls and runtime.triggered == runtime.attempts == 0
    assert result.validation_errors[0]['type'] == 'sample_budget_exceeded'


@pytest.mark.parametrize('stage_name,packet_name,phase', [('analyzer', 'J', 'analysis'),
                                                       ('global_reconciliation', 'GLOBAL', 'global')])
def test_actual_format_repair_is_charged_to_target_phase(tmp_path, stage_name, packet_name, phase):
    from pydantic_ai.models.function import DeltaToolCall
    from bounded_investigator.execution.sample_budget import SampleBudget
    from bounded_investigator.execution.stage_runner import RepairRuntime, StageRunner
    from bounded_investigator.schemas import DeliveryStatus, InvestigationResult

    output = InvestigationResult(status='complete', summary='repaired fixture')
    async def malformed(messages, info):
        yield {0: DeltaToolCall(name=info.output_tools[0].name, json_args='{"status":"complete","summary":[]}')}
    async def repaired(messages, info):
        yield output.model_dump_json()
    cfg = SimpleNamespace(format_repair=SimpleNamespace(max_attempts=2, total_tokens_limit=10000),
                          workflow=SimpleNamespace(max_model_requests=3))
    model_cfg = SimpleNamespace(stream_output=False, output_mode='tool')
    stage = SimpleNamespace(stage=stage_name, schema=InvestigationResult, model_config=model_cfg,
                            agent=Agent(FunctionModel(stream_function=malformed), output_type=InvestigationResult, retries=0))
    repair = SimpleNamespace(stage='format_repair', schema=None, model_config=model_cfg,
                             agent=Agent(FunctionModel(stream_function=repaired)))
    runtime = RepairRuntime()
    budget = SampleBudget(total_tokens=10000, reserve_tokens=1000)
    runner = StageRunner(config=cfg, format_repair=repair, log=logging.getLogger('test.budget'), sample_budget=budget)
    result = asyncio.run(runner.run(stage_agent=stage, prompt='x', packet_name=packet_name, usage=RunUsage(),
                                    limits=UsageLimits(), repair_runtime=runtime, artifact_dir=tmp_path))
    assert result.status == DeliveryStatus.SUCCESS_AFTER_REPAIR and runtime.attempts == 1
    records = budget.to_dict()['records']
    assert [r['stage'] for r in records] == [stage_name, 'format_repair']
    assert all(r['phase'] == phase for r in records) and budget.to_dict()['total_tokens'] > 0
