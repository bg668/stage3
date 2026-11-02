"""Stage-2 application service."""

from __future__ import annotations

import asyncio
import json
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from . import assertions as assertion_rules
from .reporting import AgentEventSink, build_reporter
from .contract import build_payload
from .domain import ROUND1_TEMPLATE_ID, TriageConfig, TriageTask
from .input import TriageInputPaths, load_triage_input
from .final_verdict import build_final_verdicts
from .llm_audit import LLMCallRecorder, attach_call_recorder
from .provider import TriageProvider
from .provider_health import ProviderHealthRecorder, preflight_providers
from .runner import run_round1, run_round2
from .second_hop import SecondHopBudget, assemble_second_hop_packages
from .second_hop_render import write_second_hop_packages
from .summary import write_summary
from .task import build_round1_requests, build_round2_plan
from .trace import TraceRecorder


@dataclass(frozen=True)
class TriageOptions:
    preprocess: Path
    out: Path
    units_dir: Path | None = None
    code_dir: Path | None = None
    config: TriageConfig | None = None
    actor_name: str = "员工1号"
    activity_stream: TextIO | None = None


@dataclass
class TriageRun:
    payload: dict
    summary: str

    @property
    def exit_code(self) -> int:
        return 0 if all(item["passed"] for item in self.payload["assertions"]) else 1


def run_triage(
    options: TriageOptions,
    round1_provider: TriageProvider,
    round2_provider: TriageProvider | None = None,
) -> TriageRun:
    """Synchronous entry point for the standalone CLI and simple callers."""
    return asyncio.run(run_triage_async(options, round1_provider, round2_provider))


async def run_triage_async(
    options: TriageOptions,
    round1_provider: TriageProvider,
    round2_provider: TriageProvider | None = None,
) -> TriageRun:
    started = time.perf_counter()
    config = options.config or TriageConfig(template_id=ROUND1_TEMPLATE_ID)
    if config.round2_enabled and round2_provider is None:
        raise ValueError("round2_enabled=true 时必须提供 Round2 provider")

    snapshot = load_triage_input(
        TriageInputPaths(
            preprocess=options.preprocess,
            units_dir=options.units_dir,
            code_dir=options.code_dir,
        )
    )
    requests = build_round1_requests(snapshot, template_id=config.template_id)

    options.out.mkdir(parents=True, exist_ok=True)
    second_hop_dir = options.out / "second_hop"
    if second_hop_dir.exists():
        shutil.rmtree(second_hop_dir)
    second_hop_dir.mkdir()

    activity = build_reporter(
        options.out / "activity.jsonl",
        actor=options.actor_name,
        stream=options.activity_stream,
    )
    activity.reset()
    trace_recorder = TraceRecorder(options.out / "trace.jsonl")
    trace_recorder.reset()
    llm_call_recorder = LLMCallRecorder(options.out / "llm_calls.jsonl")
    llm_call_recorder.reset()
    provider_health_recorder = ProviderHealthRecorder(options.out / "provider_health.jsonl")
    provider_health_recorder.reset()
    _attach_llm_recorders(
        round1_provider,
        round2_provider,
        recorder=llm_call_recorder,
    )
    _attach_provider_activity(round1_provider, round2_provider, activity=activity)

    if config.provider_preflight_enabled:
        try:
            await preflight_providers(
                (round1_provider, round2_provider if config.round2_enabled else None),
                timeout_seconds=config.provider_probe_timeout_seconds,
                recorder=provider_health_recorder,
                activity=activity,
            )
        except Exception:
            activity.close()
            raise

    activity.report(
        "stage_started",
        stage="stage2",
        undecided=len(snapshot.undecided_refs),
    )
    activity.report(
        "round_started",
        stage="round1",
        round_number=1,
        task_total=len(requests),
        function_total=sum(len(request.task.subject_refs) for request in requests),
        max_concurrency=config.max_concurrency,
    )

    round1 = await run_round1(
        requests,
        round1_provider,
        max_attempts_per_task=config.max_attempts_per_task,
        max_concurrency=config.max_concurrency,
        model_profile=config.model_profile,
        model_id=config.model_id,
        activity=activity,
        trace_recorder=trace_recorder,
    )
    r1_counts = _decision_counts(round1.tasks, round_number=1)
    activity.report(
        "round_completed",
        stage="round1",
        round_number=1,
        analyze=r1_counts["ANALYZE"],
        skip=r1_counts["SKIP"],
        uncertain=r1_counts["UNCERTAIN"],
    )

    for task in round1.tasks:
        if task.round != 1 or task.status != "OK":
            continue
        for verdict in task.verdicts:
            if verdict.decision != "UNCERTAIN":
                continue
            activity.report(
                "context_requested",
                stage="second_hop",
                round_number=1,
                task_id=task.task_id,
                refs=[verdict.ref],
                reason=verdict.reason,
                note=verdict.note,
            )

    activity.report(
        "context_assembly_started",
        stage="second_hop",
        subject_total=sum(
            1
            for task in round1.tasks
            if task.round == 1 and task.status == "OK"
            for verdict in task.verdicts
            if verdict.decision == "UNCERTAIN"
        ),
    )
    second_hop_packages = assemble_second_hop_packages(
        snapshot,
        round1.tasks,
        SecondHopBudget(
            max_chars=config.second_hop_max_chars,
            max_related_functions=config.second_hop_max_related_functions,
            max_code_chars_per_related_function=(
                config.second_hop_max_code_chars_per_related_function
            ),
        ),
    )
    write_second_hop_packages(second_hop_dir, second_hop_packages)
    for package in second_hop_packages:
        activity.report(
            "context_assembled",
            stage="second_hop",
            refs=[package.subject_ref],
            status=package.status,
            reason=package.reason,
            related_count=len(package.related_functions),
            fact_count=len(package.facts),
            missing_sections=list(package.missing_sections),
            truncated=package.truncated,
            error=package.error,
        )
    activity.report(
        "context_assembly_completed",
        stage="second_hop",
        total=len(second_hop_packages),
        complete=sum(1 for package in second_hop_packages if package.status == "COMPLETE"),
        partial=sum(1 for package in second_hop_packages if package.status == "PARTIAL"),
        failed=sum(1 for package in second_hop_packages if package.status == "FAILED"),
        truncated=sum(1 for package in second_hop_packages if package.truncated),
    )

    round2_tasks: list[TriageTask] = []
    round2_traces = []
    round2_root_task_count = 0
    if config.round2_enabled:
        assert round2_provider is not None
        plan = build_round2_plan(
            second_hop_packages,
            round1.tasks,
            template_id=config.round2_template_id,
        )
        activity.report(
            "round_started",
            stage="round2",
            round_number=2,
            task_total=plan.total,
            function_total=plan.total,
            max_concurrency=config.max_concurrency,
            forced_without_model=len(plan.forced),
        )
        round2 = await run_round2(
            plan,
            round2_provider,
            max_attempts_per_task=config.max_attempts_per_task,
            max_concurrency=config.max_concurrency,
            model_profile=config.round2_model_profile,
            model_id=config.round2_model_id,
            activity=activity,
            trace_recorder=trace_recorder,
        )
        round2_tasks = round2.tasks
        round2_traces = round2.traces
        round2_root_task_count = round2.root_task_count
        r2_counts = _decision_counts(round2_tasks, round_number=2)
        activity.report(
            "round_completed",
            stage="round2",
            round_number=2,
            analyze=r2_counts["ANALYZE"],
            skip=r2_counts["SKIP"],
            forced=sum(1 for task in round2_tasks if task.forced_analyze),
        )

    tasks = list(round1.tasks) + list(round2_tasks)
    traces = list(round1.traces) + list(round2_traces)
    final_verdicts = build_final_verdicts(snapshot.undecided_refs, tasks)

    assertions = assertion_rules.run_all(
        undecided_refs=snapshot.undecided_refs,
        tasks=tasks,
        final_verdicts=final_verdicts,
        second_hop_dir=second_hop_dir,
        second_hop_packages=second_hop_packages,
        round2_enabled=config.round2_enabled,
    )
    wall_ms = max(0, round((time.perf_counter() - started) * 1000))
    payload = build_payload(
        snapshot=snapshot,
        tasks=tasks,
        traces=traces,
        round1_root_task_count=round1.root_task_count,
        round2_root_task_count=round2_root_task_count,
        second_hop_packages=second_hop_packages,
        final_verdicts=final_verdicts,
        assertions=assertions,
        config=config.model_dump(),
        wall_ms=wall_ms,
    )
    (options.out / "triage.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    summary = write_summary(options.out / "summary.txt", payload)
    unresolved_refs = [
        ref
        for task in round1.tasks
        if task.status != "OK"
        for ref in task.subject_refs
    ]
    activity.report(
        "stage_completed",
        stage="stage2",
        undecided=len(snapshot.undecided_refs),
        stage2_analyze=sum(row.final_decision == "ANALYZE" for row in final_verdicts),
        stage2_skip=sum(row.final_decision == "SKIP" for row in final_verdicts),
        complete=all(item["passed"] for item in assertions),
        unresolved=len(_stable_union((), unresolved_refs)),
    )
    activity.close()
    return TriageRun(payload=payload, summary=summary)


def _attach_llm_recorders(
    round1_provider: TriageProvider,
    round2_provider: TriageProvider | None,
    *,
    recorder: LLMCallRecorder,
) -> None:
    seen: set[int] = set()
    for provider in (round1_provider, round2_provider):
        if provider is None or id(provider) in seen:
            continue
        seen.add(id(provider))
        attach_call_recorder(provider, recorder)


def _attach_provider_activity(
    round1_provider: TriageProvider,
    round2_provider: TriageProvider | None,
    *,
    activity: AgentEventSink,
) -> None:
    seen: set[int] = set()
    for provider in (round1_provider, round2_provider):
        if provider is None or id(provider) in seen:
            continue
        seen.add(id(provider))
        setter = getattr(provider, "set_activity_sink", None)
        if callable(setter):
            setter(activity)


def _decision_counts(tasks: list[TriageTask], *, round_number: int) -> dict[str, int]:
    counts = {"ANALYZE": 0, "SKIP": 0, "UNCERTAIN": 0}
    for task in tasks:
        if task.round != round_number or task.status != "OK":
            continue
        for verdict in task.verdicts:
            if verdict.decision in counts:
                counts[verdict.decision] += 1
    return counts


def _stable_union(initial: tuple[str, ...] | list[str], added: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for ref in list(initial) + added:
        if ref not in seen:
            seen.add(ref)
            result.append(ref)
    return result
