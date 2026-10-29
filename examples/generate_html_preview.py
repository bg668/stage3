"""Reproduce the synthetic preview with a fixed response and no model provider."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pydantic_ai import PartStartEvent
from pydantic_ai.messages import TextPart
import yaml

from bounded_investigator.config import AppConfig
from bounded_investigator.reporting import agent
from bounded_investigator.reporting.composition import ReportComposition
from bounded_investigator.reporting.html_report import create_html_report


def main() -> None:
    examples = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, default=examples / 'html-preview-v11')
    parser.add_argument('--context-file', type=Path, default=examples / 'html-preview-v11-input/context.json')
    args = parser.parse_args()
    source = examples / 'html-preview-v11-input'
    response = ReportComposition.model_validate_json((source / 'composition.json').read_text(encoding='utf-8'))
    settings = AppConfig.model_validate(yaml.safe_load((examples.parent / 'config.example.yaml').read_text(encoding='utf-8')))
    settings.format_repair.enabled = False
    settings.models[settings.stages['report_composition'].model].model_name = 'fixed-response/offline-preview-no-network'

    async def fixed_response(prompt, **kwargs):
        async def events():
            yield PartStartEvent(index=0, part=TextPart(content=response.model_dump_json()))
        await kwargs['event_stream_handler'](None, events())
        return SimpleNamespace(output=response)

    def build_fixed(**kwargs):
        return SimpleNamespace(stage=kwargs['stage'], schema=kwargs['schema'],
            model_config=SimpleNamespace(stream_output=False, output_mode='tool'),
            agent=SimpleNamespace(run=fixed_response))

    with patch.object(agent, 'build_stage_agent', build_fixed):
        manifest = asyncio.run(create_html_report(
            global_result=source / '_global_reconciliation.json', output_dir=args.output_dir,
            config=settings, context_file=args.context_file,
            packets_dir=source / 'packets', packet_results_dir=source,
        ))
    assert manifest['usage']['total']['requests'] == 0
    print(json.dumps({'status':manifest['status'], 'html':str(args.output_dir / '_final_report.html'),
                      'real_model_requests':0, 'test_fills':len(manifest['test_fills'])}, ensure_ascii=True))


if __name__ == '__main__':
    main()
