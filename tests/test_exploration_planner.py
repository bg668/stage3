import asyncio
import logging
from types import SimpleNamespace

import pytest
from pydantic_ai import Agent
from pydantic_ai.models.function import DeltaToolCall, FunctionModel

from bounded_investigator.config import load_config
from bounded_investigator.execution.sample_budget import SampleBudget


def config():
    from bounded_investigator.config import StageConfig
    cfg = load_config('config.example.yaml')
    cfg.stages['planner'] = StageConfig(model=cfg.stages['analyzer'].model, max_output_tokens=1024)
    return cfg


def install_planner(monkeypatch, decision, calls):
    from bounded_investigator.orchestration import exploration
    def build(**kw):
        assert kw['output_retries'] == 0 and kw['stage'] == 'planner'
        async def stream(messages, info):
            calls.append(messages)
            yield {0: DeltaToolCall(name=info.output_tools[0].name, json_args=decision)}
        return SimpleNamespace(stage='planner', schema=kw['schema'], agent=Agent(FunctionModel(stream_function=stream),
            output_type=kw['schema'], retries=0), model_config=SimpleNamespace(stream_output=False, output_mode='tool'))
    monkeypatch.setattr(exploration, 'build_stage_agent', build)


@pytest.mark.parametrize('decision', ['{"action":"analyze","candidate_id":"C-one","reason":"source gap","expected_value":"bind source"}',
                                       '{"action":"stop","reason":"budget should be preserved"}'])
def test_planner_single_decision_and_shared_budget(monkeypatch, tmp_path, decision):
    from bounded_investigator.orchestration.exploration import choose_next
    from bounded_investigator.orchestration.exploration_models import Candidate
    calls = []
    install_planner(monkeypatch, decision, calls)
    budget = SampleBudget(total_tokens=10000, reserve_tokens=1000)
    candidate = Candidate(candidate_id='C-one', primary_refs=['F-0001'], question='source?', issue_key='source')
    result = asyncio.run(choose_next(candidates=[candidate], reviewed_facts=[], drafts=[], config=config(),
        budget=budget, artifact_dir=tmp_path, round_index=1))
    assert result.reason and len(calls) == 1
    assert budget.to_dict()['records'][0]['stage'] == 'planner'
    assert budget.to_dict()['records'][0]['phase'] == 'analysis'


@pytest.mark.parametrize('decision', ['{"action":"analyze","candidate_id":"../../escape","reason":"x","expected_value":"x"}',
                                       '{"action":"analyze","candidate_id":"C-one","reason":"x"}', 'not JSON'])
def test_invalid_planner_stops_without_repair_or_retry(monkeypatch, tmp_path, decision):
    from bounded_investigator.orchestration.exploration import PlanningFailed, choose_next
    from bounded_investigator.orchestration.exploration_models import Candidate
    calls = []
    install_planner(monkeypatch, decision, calls)
    with pytest.raises(PlanningFailed):
        asyncio.run(choose_next(candidates=[Candidate(candidate_id='C-one', primary_refs=['F-0001'], question='q', issue_key='q')],
            reviewed_facts=[], drafts=[], config=config(), budget=SampleBudget(), artifact_dir=tmp_path, round_index=1))
    assert len(calls) == 1
    assert not (tmp_path / 'format_repair').exists()


def test_planner_budget_refusal_makes_no_call(monkeypatch, tmp_path):
    from bounded_investigator.orchestration.exploration import PlanningFailed, choose_next
    from bounded_investigator.orchestration.exploration_models import Candidate
    calls = []
    install_planner(monkeypatch, '{}', calls)
    with pytest.raises(PlanningFailed):
        asyncio.run(choose_next(candidates=[Candidate(candidate_id='C-one', primary_refs=['F-0001'], question='q', issue_key='q')],
            reviewed_facts=[], drafts=[], config=config(), budget=SampleBudget(total_tokens=1, reserve_tokens=1),
            artifact_dir=tmp_path, round_index=1))
    assert calls == []
