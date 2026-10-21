from types import SimpleNamespace

from bounded_investigator.usage_utils import reasoning_tokens_from_usage, usage_delta, usage_summary


def test_reasoning_tokens_are_recorded_as_output_subset():
    usage = SimpleNamespace(
        requests=1,
        input_tokens=100,
        output_tokens=80,
        total_tokens=180,
        cache_read_tokens=0,
        cache_write_tokens=0,
        cost=None,
        details={"reasoning_tokens": 60},
    )
    summary = usage_summary(usage)
    assert summary.reasoning_tokens == 60
    assert summary.output_tokens == 80
    assert summary.visible_output_tokens == 20


def test_reasoning_aliases_can_accumulate_across_providers():
    usage = SimpleNamespace(
        requests=2,
        input_tokens=100,
        output_tokens=70,
        total_tokens=170,
        cache_read_tokens=0,
        cache_write_tokens=0,
        cost=None,
        details={"reasoning_tokens": 30, "thoughts_tokens": 20},
    )
    assert reasoning_tokens_from_usage(usage) == 50


def test_stage_usage_delta():
    before = usage_summary(SimpleNamespace(
        requests=1, input_tokens=100, output_tokens=40, total_tokens=140,
        cache_read_tokens=0, cache_write_tokens=0, cost=None,
        details={"reasoning_tokens": 30},
    ))
    after = usage_summary(SimpleNamespace(
        requests=2, input_tokens=250, output_tokens=100, total_tokens=350,
        cache_read_tokens=0, cache_write_tokens=0, cost=None,
        details={"reasoning_tokens": 70},
    ))
    delta = usage_delta(before, after)
    assert delta.requests == 1
    assert delta.input_tokens == 150
    assert delta.output_tokens == 60
    assert delta.reasoning_tokens == 40
    assert delta.visible_output_tokens == 20


def test_merge_usage_summaries_keeps_reasoning_as_output_subset():
    from bounded_investigator.schemas import UsageSummary
    from bounded_investigator.usage_utils import merge_usage_summaries

    merged = merge_usage_summaries(
        UsageSummary(requests=1, input_tokens=10, output_tokens=20, reasoning_tokens=15, visible_output_tokens=5, total_tokens=30),
        UsageSummary(requests=2, input_tokens=30, output_tokens=40, reasoning_tokens=10, visible_output_tokens=30, total_tokens=70),
    )
    assert merged.requests == 3
    assert merged.output_tokens == 60
    assert merged.reasoning_tokens == 25
    assert merged.visible_output_tokens == 35
    assert merged.total_tokens == 100
