"""Explicit business retry policy for Stage-2 Round1 and Round2."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from .reporting.events import AgentEventSink
from .domain import AttemptTrace, FailureKind, FunctionVerdict, TriageTask, Usage
from .provider import (
    ProviderBudgetError,
    ProviderError,
    ProviderSchemaError,
    ProviderTimeoutError,
    TriageProvider,
)
from .task import (
    Round2Plan,
    TriageRequest,
    build_retry_request,
    build_split_requests,
    sha256_text,
)
from .trace import TraceRecorder
from .validation import (
    VerdictIdMismatch,
    VerdictSemanticMismatch,
    validate_and_order_verdicts,
    validate_round2_and_order_verdicts,
)


@dataclass(frozen=True)
class Round1RunResult:
    tasks: list[TriageTask]
    traces: list[AttemptTrace]
    root_task_count: int


@dataclass(frozen=True)
class Round2RunResult:
    tasks: list[TriageTask]
    traces: list[AttemptTrace]
    root_task_count: int


@dataclass(frozen=True)
class _AttemptOutcome:
    status: str
    verdicts: list[FunctionVerdict] | None
    model_id: str | None
    usage: Usage
    error: str | None
    retryable: bool = True


@dataclass(frozen=True)
class _ExecutionResult:
    tasks: list[TriageTask]
    traces: list[AttemptTrace]


class _RoundProgress:
    def __init__(self, total: int) -> None:
        self.total = total
        self.completed = 0
        self._lock = asyncio.Lock()

    async def advance(self) -> int:
        async with self._lock:
            self.completed += 1
            return self.completed


async def run_round1(
    requests: list[TriageRequest],
    provider: TriageProvider,
    *,
    max_attempts_per_task: int = 2,
    max_concurrency: int = 3,
    model_profile: str | None = None,
    model_id: str | None = None,
    activity: AgentEventSink | None = None,
    trace_recorder: TraceRecorder | None = None,
) -> Round1RunResult:
    """Execute Round1 with explicit retry and deterministic split fallback."""
    _validate_limits(max_attempts_per_task, max_concurrency)
    semaphore = asyncio.Semaphore(max_concurrency)
    progress = _RoundProgress(len(requests))

    async def execute_root(request: TriageRequest) -> _ExecutionResult:
        execution = await _execute_task(
            request,
            provider,
            semaphore=semaphore,
            max_attempts=max_attempts_per_task,
            allow_split=True,
            model_profile=model_profile,
            expected_model_id=model_id,
            activity=activity,
            trace_recorder=trace_recorder,
        )
        completed = await progress.advance()
        _report_task_completed(
            activity,
            request,
            execution.tasks,
            completed=completed,
            total=progress.total,
        )
        return execution

    executions = await asyncio.gather(*(execute_root(request) for request in requests))
    return Round1RunResult(
        tasks=[task for execution in executions for task in execution.tasks],
        traces=[trace for execution in executions for trace in execution.traces],
        root_task_count=len(requests),
    )


async def run_round2(
    plan: Round2Plan,
    provider: TriageProvider,
    *,
    max_attempts_per_task: int = 2,
    max_concurrency: int = 3,
    model_profile: str | None = None,
    model_id: str | None = None,
    activity: AgentEventSink | None = None,
    trace_recorder: TraceRecorder | None = None,
) -> Round2RunResult:
    """Execute one-subject Round2 tasks and conservatively resolve technical failure.

    Round2 never splits: one second-hop package already represents exactly one subject.
    Any terminal technical failure becomes ANALYZE so Stage2 never silently SKIPs an
    unresolved function. Assembly failures are also resolved without an LLM call.
    """
    _validate_limits(max_attempts_per_task, max_concurrency)
    semaphore = asyncio.Semaphore(max_concurrency)
    progress = _RoundProgress(plan.total)

    async def execute_root(request: TriageRequest) -> _ExecutionResult:
        execution = await _execute_task(
            request,
            provider,
            semaphore=semaphore,
            max_attempts=max_attempts_per_task,
            allow_split=False,
            model_profile=model_profile,
            expected_model_id=model_id,
            activity=activity,
            trace_recorder=trace_recorder,
        )
        terminal_tasks = [
            _force_round2_analyze(task) if task.status == "FAILED" else task
            for task in execution.tasks
        ]
        completed = await progress.advance()
        _report_task_completed(
            activity,
            request,
            terminal_tasks,
            completed=completed,
            total=progress.total,
        )
        return _ExecutionResult(tasks=terminal_tasks, traces=execution.traces)

    executions = await asyncio.gather(*(execute_root(request) for request in plan.requests))

    tasks: list[TriageTask] = [
        task for execution in executions for task in execution.tasks
    ]
    traces: list[AttemptTrace] = [
        trace for execution in executions for trace in execution.traces
    ]

    for seed in plan.forced:
        task = seed.task.model_copy(deep=True)
        task.status = "OK"
        task.failure_kind = seed.failure_kind  # type: ignore[assignment]
        task.verdicts = [
            FunctionVerdict(
                ref=task.subject_refs[0],
                decision="ANALYZE",
                reason="other",
                note="second-hop 装配失败，保守进入精读",
            )
        ]
        task.model_id = model_id
        task.usage = Usage()
        task.attempts = 0
        task.error = seed.error
        task.forced_analyze = True
        tasks.append(task)
        completed = await progress.advance()
        _report_task_completed(
            activity,
            TriageRequest(task=seed.task, input_text=""),
            [task],
            completed=completed,
            total=progress.total,
        )

    order = {ref: index for index, ref in enumerate(plan.subject_order)}
    tasks.sort(key=lambda task: order.get(task.subject_refs[0], 10**9))

    return Round2RunResult(tasks=tasks, traces=traces, root_task_count=plan.total)


async def _execute_task(
    request: TriageRequest,
    provider: TriageProvider,
    *,
    semaphore: asyncio.Semaphore,
    max_attempts: int,
    allow_split: bool,
    model_profile: str | None,
    expected_model_id: str | None,
    activity: AgentEventSink | None,
    trace_recorder: TraceRecorder | None,
) -> _ExecutionResult:
    task = request.task.model_copy(deep=True)
    traces: list[AttemptTrace] = []
    total_usage = Usage()
    retry_request = request
    last_outcome: _AttemptOutcome | None = None

    for attempt in range(1, max_attempts + 1):
        async with semaphore:
            _report(
                activity,
                "attempt_started",
                stage="round%d" % task.round,
                round_number=task.round,
                task_id=task.task_id,
                refs=task.subject_refs,
                unit_id=task.unit_id,
                attempt=attempt,
                max_attempts=max_attempts,
                ref_count=len(task.subject_refs),
                model_profile=model_profile,
            )
            outcome = await _classify_once(retry_request, provider)
            _report(
                activity,
                "attempt_finished",
                stage="round%d" % task.round,
                round_number=task.round,
                task_id=task.task_id,
                refs=task.subject_refs,
                unit_id=task.unit_id,
                attempt=attempt,
                status=outcome.status,
                latency_ms=outcome.usage.latency_ms,
            )
        last_outcome = outcome
        total_usage = total_usage.add(outcome.usage)
        trace = AttemptTrace(
            task_id=task.task_id,
            parent_task_id=task.parent_task_id,
            unit_id=task.unit_id,
            attempt=attempt,
            round=task.round,
            refs=list(task.subject_refs),
            status=outcome.status,
            model_profile=model_profile,
            model_id=outcome.model_id or expected_model_id,
            input_sha256=sha256_text(retry_request.input_text),
            usage=outcome.usage,
            error=outcome.error,
        )
        traces.append(trace)
        if trace_recorder is not None:
            trace_recorder.append(trace)

        if outcome.status == "OK":
            task.status = "OK"
            task.failure_kind = None
            task.verdicts = list(outcome.verdicts or [])
            task.model_id = outcome.model_id or expected_model_id
            task.usage = total_usage
            task.attempts = attempt
            task.error = None
            task.forced_analyze = False
            return _ExecutionResult(tasks=[task], traces=traces)

        if outcome.status == "BUDGET_EXCEEDED" or not outcome.retryable:
            next_action = "terminal"
        elif attempt < max_attempts:
            next_action = "retry"
        elif (
            allow_split
            and len(task.subject_refs) > 1
            and outcome.status in ("SCHEMA_FAILED", "ID_MISMATCH")
        ):
            next_action = "split"
        else:
            next_action = "terminal"
        _report(
            activity,
            "attempt_failed",
            stage="round%d" % task.round,
            round_number=task.round,
            task_id=task.task_id,
            refs=task.subject_refs,
            status=outcome.status,
            error=outcome.error,
            next_action=next_action,
        )

        if outcome.status == "BUDGET_EXCEEDED" or not outcome.retryable:
            break

        if attempt < max_attempts:
            if outcome.status in ("SCHEMA_FAILED", "ID_MISMATCH"):
                retry_request = build_retry_request(
                    request,
                    failure_kind=outcome.status,
                    error=outcome.error or "",
                )
            else:
                retry_request = request
            continue

    assert last_outcome is not None
    if (
        allow_split
        and len(task.subject_refs) > 1
        and last_outcome.status in ("SCHEMA_FAILED", "ID_MISMATCH")
    ):
        children = build_split_requests(request)
        _report(
            activity,
            "task_split",
            stage="round%d" % task.round,
            round_number=task.round,
            task_id=task.task_id,
            refs=task.subject_refs,
            unit_id=task.unit_id,
            child_count=len(children),
        )
        child_results = await asyncio.gather(
            *(
                _execute_task(
                    child,
                    provider,
                    semaphore=semaphore,
                    max_attempts=max_attempts,
                    allow_split=False,
                    model_profile=model_profile,
                    expected_model_id=expected_model_id,
                    activity=activity,
                    trace_recorder=trace_recorder,
                )
                for child in children
            )
        )
        return _ExecutionResult(
            tasks=[child for result in child_results for child in result.tasks],
            traces=traces + [trace for result in child_results for trace in result.traces],
        )

    task.status = "FAILED"
    task.failure_kind = _as_failure_kind(last_outcome.status)
    task.verdicts = []
    task.model_id = last_outcome.model_id or expected_model_id
    task.usage = total_usage
    task.attempts = len([trace for trace in traces if trace.task_id == task.task_id])
    task.error = last_outcome.error
    task.forced_analyze = False
    return _ExecutionResult(tasks=[task], traces=traces)


async def _classify_once(
    request: TriageRequest,
    provider: TriageProvider,
) -> _AttemptOutcome:
    started = time.perf_counter()
    try:
        response = await provider.classify(request)
    except ProviderBudgetError as exc:
        return _failed_outcome("BUDGET_EXCEEDED", exc, started)
    except ProviderSchemaError as exc:
        return _failed_outcome("SCHEMA_FAILED", exc, started)
    except ProviderTimeoutError as exc:
        return _failed_outcome("PROVIDER_TIMEOUT", exc, started)
    except ProviderError as exc:
        return _failed_outcome("PROVIDER_ERROR", exc, started)
    except Exception as exc:
        return _failed_outcome(
            "PROVIDER_ERROR", "%s: %s" % (type(exc).__name__, exc), started
        )

    elapsed_ms = max(0, round((time.perf_counter() - started) * 1000))
    usage = response.usage.model_copy(update={"latency_ms": elapsed_ms})
    try:
        if request.task.round == 2:
            verdicts = validate_round2_and_order_verdicts(
                request.task.subject_refs, response.verdicts
            )
        else:
            verdicts = validate_and_order_verdicts(
                request.task.subject_refs, response.verdicts
            )
    except VerdictIdMismatch as exc:
        return _AttemptOutcome(
            status="ID_MISMATCH",
            verdicts=None,
            model_id=response.model_id,
            usage=usage,
            error=str(exc),
        )
    except VerdictSemanticMismatch as exc:
        return _AttemptOutcome(
            status="SCHEMA_FAILED",
            verdicts=None,
            model_id=response.model_id,
            usage=usage,
            error=str(exc),
        )

    return _AttemptOutcome(
        status="OK",
        verdicts=verdicts,
        model_id=response.model_id,
        usage=usage,
        error=None,
    )


def _force_round2_analyze(task: TriageTask) -> TriageTask:
    forced = task.model_copy(deep=True)
    forced.status = "OK"
    forced.verdicts = [
        FunctionVerdict(
            ref=forced.subject_refs[0],
            decision="ANALYZE",
            reason="other",
            note="Round2 技术失败，保守进入精读",
        )
    ]
    forced.forced_analyze = True
    return forced


def _report_task_completed(
    activity: AgentEventSink | None,
    request: TriageRequest,
    tasks: list[TriageTask],
    *,
    completed: int,
    total: int,
) -> None:
    if activity is None:
        return
    verdicts = [
        verdict.model_dump()
        for task in tasks
        if task.status == "OK"
        for verdict in task.verdicts
    ]
    failed = next((task for task in tasks if task.status != "OK"), None)
    status = "FAILED" if failed is not None else "OK"
    activity.report(
        "task_completed",
        stage="round%d" % request.task.round,
        round_number=request.task.round,
        task_id=request.task.task_id,
        refs=request.task.subject_refs,
        unit_id=request.task.unit_id,
        completed=completed,
        total=total,
        status=status,
        failure_kind=failed.failure_kind if failed is not None else None,
        error=failed.error if failed is not None else None,
        verdicts=verdicts,
        forced_analyze=any(task.forced_analyze for task in tasks),
    )


def _report(
    activity: AgentEventSink | None,
    event: str,
    *,
    stage: str,
    round_number: int | None = None,
    task_id: str | None = None,
    refs: tuple[str, ...] | list[str] = (),
    **data: object,
) -> None:
    if activity is None:
        return
    activity.report(
        event,
        stage=stage,
        round_number=round_number,
        task_id=task_id,
        refs=refs,
        **data,
    )


def _failed_outcome(status: str, error: Exception | str, started: float) -> _AttemptOutcome:
    elapsed_ms = max(0, round((time.perf_counter() - started) * 1000))
    retryable = bool(getattr(error, "retryable", True)) if isinstance(error, Exception) else True
    return _AttemptOutcome(
        status=status,
        verdicts=None,
        model_id=None,
        usage=Usage(latency_ms=elapsed_ms),
        error=str(error),
        retryable=retryable,
    )


def _as_failure_kind(status: str) -> FailureKind:
    if status not in {
        "SCHEMA_FAILED",
        "ID_MISMATCH",
        "BUDGET_EXCEEDED",
        "PROVIDER_ERROR",
        "PROVIDER_TIMEOUT",
    }:
        raise ValueError("不能作为 failure_kind 的状态: %s" % status)
    return status  # type: ignore[return-value]


def _validate_limits(max_attempts_per_task: int, max_concurrency: int) -> None:
    if max_attempts_per_task < 1:
        raise ValueError("max_attempts_per_task 必须 >= 1")
    if max_concurrency < 1:
        raise ValueError("max_concurrency 必须 >= 1")
