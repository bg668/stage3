import asyncio
import time

import pytest
from pydantic import ValidationError
from pydantic_ai import Agent
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel

from bounded_investigator.config import ProviderConfig


def test_interval_config_defaults_and_validation():
    assert ProviderConfig().min_request_interval_seconds == 1
    assert ProviderConfig(min_request_interval_seconds=0).min_request_interval_seconds == 0
    for value in [-1, float('inf'), float('nan')]:
        with pytest.raises(ValidationError): ProviderConfig(min_request_interval_seconds=value)


def test_different_models_share_service_cooldown():
    from bounded_investigator.execution.pacing import PacedModel
    async def run():
        times=[]
        async def response(messages, info):
            times.append(('start', time.monotonic()))
            await asyncio.sleep(.005)
            times.append(('end', time.monotonic()))
            return ModelResponse(parts=[TextPart('ok')])
        a=Agent(PacedModel(FunctionModel(response), service_key='same', interval=.03))
        b=Agent(PacedModel(FunctionModel(response), service_key='same', interval=.03))
        await asyncio.gather(a.run('one'), b.run('two'))
        assert [k for k,t in times]==['start','end','start','end']
        assert times[2][1]-times[1][1]>=.03
    asyncio.run(run())


def test_stream_is_held_until_completion_and_then_waits():
    from bounded_investigator.execution.pacing import PacedModel
    async def run():
        started=asyncio.Event(); finish=asyncio.Event(); times={}
        async def stream(messages, info):
            started.set()
            yield 'part'
            await finish.wait()
            times['stream_end']=time.monotonic()
            yield 'done'
        async def response(messages, info):
            times['next_start']=time.monotonic()
            return ModelResponse(parts=[TextPart('ok')])
        a=Agent(PacedModel(FunctionModel(stream_function=stream),service_key='stream',interval=.03))
        b=Agent(PacedModel(FunctionModel(response),service_key='stream',interval=.03))
        async def first():
            async with a.run_stream('one') as result: await result.get_output()
        task=asyncio.create_task(first()); await started.wait()
        other=asyncio.create_task(b.run('two')); await asyncio.sleep(.01)
        assert 'next_start' not in times
        finish.set(); await asyncio.gather(task,other)
        assert times['next_start']-times['stream_end']>=.03
    asyncio.run(run())


def test_failed_request_also_sets_cooldown_and_releases_gate():
    from bounded_investigator.execution.pacing import PacedModel
    async def run():
        times={}
        async def fail(messages,info):
            times['failed']=time.monotonic()
            raise RuntimeError('connection failed')
        async def response(messages,info):
            times['start']=time.monotonic()
            return ModelResponse(parts=[TextPart('ok')])
        with pytest.raises(RuntimeError):
            await Agent(PacedModel(FunctionModel(fail),service_key='failure',interval=.03)).run('one')
        await Agent(PacedModel(FunctionModel(response),service_key='failure',interval=.03)).run('two')
        assert times['start']-times['failed']>=.03
    asyncio.run(run())


def test_model_factory_applies_configured_interval(monkeypatch):
    from bounded_investigator.config import load_config
    from bounded_investigator.execution import llm
    from bounded_investigator.execution.pacing import PacedModel
    cfg=load_config('config.example.yaml')
    monkeypatch.setenv('DEEPSEEK_API_KEY','offline')
    model=llm.build_model(cfg,'investigator')
    assert isinstance(model,PacedModel)
    assert model.interval==1


def test_triage_registry_and_adapter_apply_the_same_interval(monkeypatch):
    from bounded_investigator.upstream.triage.llm_config import LLMRegistry, LLMRegistrySpec
    from bounded_investigator.upstream.triage.providers.pydantic_ai import PydanticAIProvider
    from bounded_investigator.execution.pacing import PacedModel
    spec=LLMRegistrySpec.model_validate({
        'providers':{'svc':{'kind':'openai-compatible','base_url':'http://offline.invalid/v1',
                            'api_key_env':'OFFLINE_KEY','min_request_interval_seconds':.25}},
        'models':{'m':{'provider':'svc','model':'offline'}},'tasks':{'triage.round1':'m'}})
    resolved=LLMRegistry(spec).resolve('triage.round1')
    assert resolved.min_request_interval_seconds==.25
    monkeypatch.setenv('OFFLINE_KEY','offline')
    provider=PydanticAIProvider(resolved)
    assert isinstance(provider._agent.model,PacedModel)
    assert provider._agent.model.interval==.25


def test_cancellation_does_not_block_following_request():
    from bounded_investigator.execution.pacing import PacedModel
    async def run():
        started=asyncio.Event(); times={}
        async def blocked(messages,info):
            started.set()
            try: await asyncio.Event().wait()
            finally: times['cancelled']=time.monotonic()
        async def response(messages,info):
            times['next_start']=time.monotonic()
            return ModelResponse(parts=[TextPart('ok')])
        a=Agent(PacedModel(FunctionModel(blocked),service_key='cancel',interval=.03))
        b=Agent(PacedModel(FunctionModel(response),service_key='cancel',interval=.03))
        task=asyncio.create_task(a.run('one')); await started.wait(); task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
        await asyncio.wait_for(b.run('two'),timeout=1)
        assert times['next_start']-times['cancelled']>=.03
    asyncio.run(run())
