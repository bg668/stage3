"""Exercise stage execution independently from the investigation workflow."""

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic_ai import PartStartEvent, UnexpectedModelBehavior
from pydantic_ai.messages import TextPart
from pydantic_ai.usage import RunUsage, UsageLimits

from bounded_investigator.execution.stage_runner import RepairRuntime, StageRunner
from bounded_investigator.schemas import DeliveryStatus, InvestigationResult


def test_stage_runner_saves_success_without_constructing_workflow(tmp_path):
    output = InvestigationResult(status='complete', summary='fixture')

    async def run(*args, **kwargs):
        return SimpleNamespace(output=output)

    stage = SimpleNamespace(
        stage='analyzer', schema=InvestigationResult,
        agent=SimpleNamespace(run=run),
        model_config=SimpleNamespace(stream_output=False, output_mode='tool'),
    )
    runner = StageRunner(config=SimpleNamespace(), format_repair=None, log=logging.getLogger('test.stage'))
    result = asyncio.run(runner.run(
        stage_agent=stage, prompt='material', packet_name='J00', usage=RunUsage(),
        limits=UsageLimits(), repair_runtime=RepairRuntime(), artifact_dir=tmp_path,
    ))
    assert result.status == DeliveryStatus.SUCCESS
    assert result.output == output
    saved = InvestigationResult.model_validate_json((tmp_path / 'analyzer/validated_result.json').read_text(encoding='utf-8'))
    assert saved == output


def failing_stage(raw):
    async def events():
        yield PartStartEvent(index=0, part=TextPart(content=raw))

    async def run(*args, **kwargs):
        kwargs['usage'].requests += 1
        await kwargs['event_stream_handler'](None, events())
        raise UnexpectedModelBehavior('structured output validation error')

    return SimpleNamespace(
        stage='analyzer', schema=InvestigationResult, agent=SimpleNamespace(run=run),
        model_config=SimpleNamespace(stream_output=False, output_mode='tool'),
    )


def test_valid_visible_output_is_recovered_without_repair_model(tmp_path):
    output = InvestigationResult(status='complete', summary='visible structured result')

    async def forbidden(*args, **kwargs):
        raise AssertionError('deterministic repair must not call a model')

    runner = StageRunner(config=SimpleNamespace(), format_repair=SimpleNamespace(agent=SimpleNamespace(run=forbidden)),
                         log=logging.getLogger('test.stage'))
    runtime, usage = RepairRuntime(), RunUsage()
    result = asyncio.run(runner.run(
        stage_agent=failing_stage(output.model_dump_json()), prompt='original material', packet_name='J00',
        usage=usage, limits=UsageLimits(), repair_runtime=runtime, artifact_dir=tmp_path,
    ))
    assert result.status == DeliveryStatus.SUCCESS_AFTER_REPAIR
    assert result.output == output
    assert (runtime.triggered, runtime.successful, runtime.attempts) == (1, 1, 0)
    assert usage.requests == 1 and runtime.usage.requests == 0


def test_missing_semantic_field_stops_repair_but_saves_valid_partial(tmp_path):
    raw = '{"artifacts":[{"kind":"file","value":"out.dat","role":"created_output",' \
          '"detection_specificity":"high","meaning":"output","confidence":"high",' \
          '"evidence":[{"location":"F-1 L1","observation":"write out.dat"}]}]}'

    async def forbidden(*args, **kwargs):
        raise AssertionError('required semantic fields cannot be invented')

    runner = StageRunner(config=SimpleNamespace(), format_repair=SimpleNamespace(agent=SimpleNamespace(run=forbidden)),
                         log=logging.getLogger('test.stage'))
    runtime = RepairRuntime()
    result = asyncio.run(runner.run(
        stage_agent=failing_stage(raw), prompt='material', packet_name='J00', usage=RunUsage(),
        limits=UsageLimits(), repair_runtime=runtime, artifact_dir=tmp_path,
    ))
    assert result.status == DeliveryStatus.PARTIAL_VALIDATED
    assert result.partial.artifacts[0].value == 'out.dat'
    assert result.partial.summary is None
    assert runtime.attempts == 0 and runtime.failed == 1
    assert (tmp_path / 'analyzer/partial_result.json').is_file()
    assert (tmp_path / 'analyzer/validation_errors.json').is_file()
    assert Path(result.raw_output_path).read_text(encoding='utf-8') == raw


def test_repair_has_separate_budget_and_stops_at_attempt_limit(tmp_path):
    calls = []

    async def repair(prompt, **kwargs):
        calls.append(prompt)
        assert 'original full material' not in prompt
        assert kwargs['usage_limits'].request_limit == 6
        kwargs['usage'].requests += 1
        return SimpleNamespace(output='still not json')

    config = SimpleNamespace(format_repair=SimpleNamespace(max_attempts=2, total_tokens_limit=10000),
                             workflow=SimpleNamespace(max_model_requests=3))
    repair_agent = SimpleNamespace(stage='format_repair', agent=SimpleNamespace(run=repair),
                                    model_config=SimpleNamespace(stream_output=False))
    runner = StageRunner(config=config, format_repair=repair_agent, log=logging.getLogger('test.stage'))
    runtime, usage = RepairRuntime(), RunUsage()
    result = asyncio.run(runner.run(
        stage_agent=failing_stage('not json'), prompt='original full material', packet_name='J00',
        usage=usage, limits=UsageLimits(), repair_runtime=runtime, artifact_dir=tmp_path,
    ))
    assert result.status == DeliveryStatus.RAW_ONLY and result.partial is None
    assert len(calls) == runtime.attempts == 2
    assert usage.requests == 1 and runtime.usage.requests == 2
    assert (tmp_path / 'format_repair/analyzer_attempt_2.txt').is_file()


def test_transport_failure_is_propagated_without_format_repair():
    async def run(*args, **kwargs):
        raise ConnectionError('connection reset')

    stage = SimpleNamespace(stage='analyzer', schema=InvestigationResult, agent=SimpleNamespace(run=run),
                            model_config=SimpleNamespace(stream_output=False, output_mode='tool'))
    runner = StageRunner(config=SimpleNamespace(), format_repair=None, log=logging.getLogger('test.stage'))
    with pytest.raises(ConnectionError, match='connection reset'):
        asyncio.run(runner.run(stage_agent=stage, prompt='material', packet_name='J00', usage=RunUsage(),
                               limits=UsageLimits(), repair_runtime=RepairRuntime(), artifact_dir=None))


def test_global_resolver_failure_keeps_facts_and_marks_degraded_delivery(monkeypatch, tmp_path):
    from bounded_investigator import workflow
    from bounded_investigator.config import load_config
    from bounded_investigator.execution.stage_runner import StageOutcome
    from bounded_investigator.schemas import BehaviorClaim, RunEnvelope, UsageSummary, ReconciliationStatus
    from factories import canonical

    config = load_config('config.example.yaml')
    config.format_repair.enabled = False
    monkeypatch.setattr(workflow, 'build_stage_agent', lambda **kw: SimpleNamespace(stage=kw['stage']))
    runner = workflow.BoundedInvestigationWorkflow(config)
    calls = []

    async def fail_resolver(**kwargs):
        calls.append(kwargs['stage_agent'].stage)
        return StageOutcome(status=DeliveryStatus.RAW_ONLY)

    monkeypatch.setattr(runner.stage_runner, 'run', fail_resolver)
    claim = BehaviorClaim.model_validate(canonical().canonical_claims[0].model_dump())
    saved = RunEnvelope(packet_name='J00', elapsed_seconds=0, revision_performed=False, usage=UsageSummary(),
                        result=InvestigationResult(status='complete', summary='facts', claims=[claim]))
    result = asyncio.run(runner.run_global_reconciliation(
        completed_results={'J00': saved}, failures={}, artifact_dir=tmp_path,
    ))
    assert calls == ['global_reconciliation']
    assert result.delivery_status == DeliveryStatus.RAW_ONLY
    assert result.provenance_valid
    assert result.result.status == ReconciliationStatus.COMPLETE_WITH_CONFLICTS
    assert result.result.canonical_claims[0].behavior == claim.behavior
    assert result.result.gaps
    assert (tmp_path / 'harness_snapshot.json').is_file()
