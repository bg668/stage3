"""Failure-path regressions using the real stage runner and fixed local agents."""

import asyncio
import json

import pytest
from pydantic_ai import PartStartEvent
from pydantic_ai.messages import TextPart

from test_html_report import input_result, install_model, settings


@pytest.mark.parametrize('interruption', ['timeout', 'cancel'])
def test_interrupted_composition_preserves_received_stream(monkeypatch, tmp_path, interruption):
    from bounded_investigator.reporting import html_report

    source = input_result(tmp_path)
    output = tmp_path / 'html'
    config = settings()
    config.html_report.wall_clock_seconds = 0.05 if interruption == 'timeout' else 30
    install_model(monkeypatch)
    from bounded_investigator.reporting import agent
    builder = agent.build_stage_agent
    partial = '{"summary": [{"text": "已经收到但尚未完成的输出'

    async def exercise():
        received = asyncio.Event()

        async def interrupted_agent(prompt, **kwargs):
            kwargs['usage'].requests += 1

            async def events():
                yield PartStartEvent(index=0, part=TextPart(content=partial))

            await kwargs['event_stream_handler'](None, events())
            received.set()
            await asyncio.Event().wait()

        def build(**kwargs):
            stage = builder(**kwargs)
            stage.agent.run = interrupted_agent
            return stage

        monkeypatch.setattr(agent, 'build_stage_agent', build)
        task = asyncio.create_task(html_report.create_html_report(
            global_result=source, output_dir=output, config=config,
        ))
        await asyncio.wait_for(received.wait(), timeout=5)
        if interruption == 'cancel':
            task.cancel()
        expected_error = TimeoutError if interruption == 'timeout' else asyncio.CancelledError
        with pytest.raises(expected_error):
            await task

    asyncio.run(exercise())
    manifest = json.loads((output / '_html_report_manifest.json').read_text(encoding='utf-8'))
    assert manifest['status'] == 'failed'
    assert manifest['error']['type'] == ('TimeoutError' if interruption == 'timeout' else 'CancelledError')
    assert manifest['usage']['composition']['requests'] == 1
    assert not (output / '_final_report.html').exists()
    assert not (output / '_html_report.json').exists()
    raw_paths = list((output / '_html_report_artifacts').glob('*/report_composition/raw_output.txt'))
    assert len(raw_paths) == 1
    assert raw_paths[0].read_text(encoding='utf-8') == partial


def test_output_equal_to_worker_input_directory_is_rejected_without_mutation(monkeypatch, tmp_path):
    from bounded_investigator.reporting import html_report

    source = input_result(tmp_path)
    worker_results = tmp_path / 'worker_results'
    worker_results.mkdir()
    for name in ('_final_report.html', '_html_report.json', 'J00.json'):
        (worker_results / name).write_text('preserve input ' + name, encoding='utf-8')
    before = {p.name: p.read_bytes() for p in worker_results.iterdir()}
    calls = install_model(monkeypatch)

    with pytest.raises(ValueError, match='directory|目录'):
        asyncio.run(html_report.create_html_report(
            global_result=source, output_dir=worker_results, packet_results_dir=worker_results,
            config=settings(),
        ))

    assert calls == []
    assert {p.name: p.read_bytes() for p in worker_results.iterdir()} == before


def test_failed_completed_manifest_write_unpublishes_report(monkeypatch, tmp_path):
    from bounded_investigator.reporting import html_report

    source = input_result(tmp_path)
    output = tmp_path / 'html'
    install_model(monkeypatch)
    original_save_json = html_report._save_json
    failed_writes = []

    def save_json(path, value):
        if (path.name == '_html_report_manifest.json' and value.get('status') == 'completed'
                and not failed_writes):
            failed_writes.append(path)
            raise OSError('completed manifest write failed once')
        return original_save_json(path, value)

    monkeypatch.setattr(html_report, '_save_json', save_json)
    with pytest.raises(OSError, match='completed manifest write failed once'):
        asyncio.run(html_report.create_html_report(
            global_result=source, output_dir=output, config=settings(),
        ))

    assert len(failed_writes) == 1
    assert not (output / '_final_report.html').exists()
    assert not (output / '_html_report.json').exists()
    manifest = json.loads((output / '_html_report_manifest.json').read_text(encoding='utf-8'))
    assert manifest['status'] == 'failed'
    assert manifest['error']['type'] == 'OSError'
    assert manifest['usage']['composition']['requests'] == 1
    failures = list((output / '_html_report_artifacts').glob('*/failure.json'))
    assert len(failures) == 1
    assert json.loads(failures[0].read_text(encoding='utf-8'))['type'] == 'OSError'
