"""An optional, shared token ledger enforced at model request boundaries."""

from __future__ import annotations

import json
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from pydantic_ai.models.wrapper import WrapperModel

from .usage_utils import reasoning_tokens_from_usage

BudgetPhase = Literal['analysis', 'global']


class SampleBudgetExceeded(RuntimeError):
    """A new provider request was declined; no provider call was made."""


class SampleBudget:
    def __init__(self, total_tokens: int = 200_000, reserve_tokens: int = 40_000,
                 ledger_path: Path | None = None):
        if total_tokens <= 0 or reserve_tokens < 0 or reserve_tokens > total_tokens:
            raise ValueError('require total_tokens > 0 and 0 <= reserve_tokens <= total_tokens')
        self.limit = total_tokens
        self.reserve_tokens = reserve_tokens
        self.ledger_path = Path(ledger_path) if ledger_path is not None else None
        self.records: list[dict] = []
        self.denied_requests: list[dict] = []

    def remaining(self, phase: BudgetPhase = 'analysis') -> int:
        if phase not in {'analysis', 'global'}:
            raise ValueError(f'unknown budget phase: {phase}')
        used = sum(r['input_tokens'] + r['output_tokens'] for r in self.records)
        cap = self.limit - self.reserve_tokens if phase == 'analysis' else self.limit
        return max(cap - used, 0)

    def exhausted(self, phase: BudgetPhase = 'analysis') -> bool:
        return self.remaining(phase) == 0

    def check(self, phase: BudgetPhase, packet_name: str, stage: str) -> int:
        remaining = self.remaining(phase)
        if remaining == 0:
            self.denied_requests.append({'phase': phase, 'packet_name': packet_name, 'stage': stage,
                                         'reason': 'sample_budget_exceeded'})
            self.save()
            raise SampleBudgetExceeded(f'{phase} sample budget exhausted before {packet_name}/{stage}')
        return remaining

    def begin(self, phase: BudgetPhase, packet_name: str, stage: str, settings) -> dict:
        remaining = self.check(phase, packet_name, stage)
        return {**(settings or {}), 'max_tokens': min((settings or {}).get('max_tokens') or remaining, remaining)}

    def record(self, *, phase: BudgetPhase, packet_name: str, stage: str, started: float,
               usage, status: str, error: BaseException | None = None) -> None:
        # SDK usage defaults can represent absent provider metadata. Keep that
        # uncertainty explicit, even though the known lower bound is zero.
        known = usage is not None and bool(getattr(usage, 'has_values', lambda: False)())
        value = lambda key: int(getattr(usage, key, 0) or 0) if usage is not None else 0
        self.records.append({
            'request': len(self.records) + 1, 'phase': phase, 'packet_name': packet_name, 'stage': stage,
            'status': status, 'usage_known': known,
            'input_tokens': value('input_tokens'), 'output_tokens': value('output_tokens'),
            'cache_read_tokens': value('cache_read_tokens'), 'cache_write_tokens': value('cache_write_tokens'),
            'reasoning_tokens': reasoning_tokens_from_usage(usage),
            'elapsed_seconds': round(time.monotonic() - started, 3),
            'error_type': type(error).__name__ if error is not None else None,
        })
        self.save()

    def to_dict(self) -> dict:
        keys = ('input_tokens', 'output_tokens', 'cache_read_tokens', 'cache_write_tokens', 'reasoning_tokens')
        totals = {key: sum(r[key] for r in self.records) for key in keys}
        used = totals['input_tokens'] + totals['output_tokens']
        analysis = sum(r['input_tokens'] + r['output_tokens'] for r in self.records if r['phase'] == 'analysis')
        return {
            'limit_tokens': self.limit, 'reserve_tokens': self.reserve_tokens,
            'analysis_limit_tokens': self.limit - self.reserve_tokens,
            'requests': len(self.records), **totals, 'total_tokens': used,
            'analysis_tokens': analysis, 'remaining_analysis_tokens': self.remaining('analysis'),
            'remaining_total_tokens': self.remaining('global'),
            'excess_tokens': max(used - self.limit, 0),
            'analysis_excess_tokens': max(analysis - (self.limit - self.reserve_tokens), 0),
            'unknown_usage_requests': sum(not r['usage_known'] for r in self.records),
            'elapsed_seconds': round(sum(r['elapsed_seconds'] for r in self.records), 3),
            'accounting': 'known usage lower bound; reasoning and cache tokens are included in input/output totals',
            'stop_rule': 'request boundary; the last accepted request may exceed the token limit',
            'records': self.records, 'denied_requests': self.denied_requests,
        }

    def save(self) -> None:
        if self.ledger_path is not None:
            self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.ledger_path.with_suffix(self.ledger_path.suffix + '.tmp')
            temporary.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding='utf-8')
            temporary.replace(self.ledger_path)


class BudgetedModel(WrapperModel):
    """Wrap every provider request, including automatic output-validation retries."""

    def __init__(self, wrapped, *, budget: SampleBudget, phase: BudgetPhase, packet_name: str, stage: str):
        super().__init__(wrapped)
        self.budget, self.phase, self.packet_name, self.stage = budget, phase, packet_name, stage

    async def request(self, messages, model_settings, model_request_parameters):
        settings = self.budget.begin(self.phase, self.packet_name, self.stage, model_settings)
        started = time.monotonic()
        response = None
        error = None
        try:
            response = await self.wrapped.request(messages, settings, model_request_parameters)
            return response
        except BaseException as exc:
            error = exc
            raise
        finally:
            self.budget.record(phase=self.phase, packet_name=self.packet_name, stage=self.stage, started=started,
                               usage=getattr(response, 'usage', None) or getattr(error, 'usage', None),
                               status='failed' if error is not None else 'completed', error=error)

    @asynccontextmanager
    async def request_stream(self, messages, model_settings, model_request_parameters, run_context=None):
        settings = self.budget.begin(self.phase, self.packet_name, self.stage, model_settings)
        started = time.monotonic()
        response = None
        error = None
        try:
            async with self.wrapped.request_stream(messages, settings, model_request_parameters, run_context) as response:
                yield response
        except BaseException as exc:
            error = exc
            raise
        finally:
            usage = response.usage if response is not None else getattr(error, 'usage', None)
            self.budget.record(phase=self.phase, packet_name=self.packet_name, stage=self.stage, started=started,
                               usage=usage, status='failed' if error is not None else 'completed', error=error)
