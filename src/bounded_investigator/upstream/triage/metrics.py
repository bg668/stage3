"""Deterministic execution metrics derived from tasks and attempt traces."""

from __future__ import annotations

from collections import Counter, defaultdict

from .domain import AttemptTrace, TriageTask


def build_execution_metrics(
    *, tasks: list[TriageTask], traces: list[AttemptTrace], root_task_count: int
) -> dict[str, int]:
    """Round1/V3-compatible execution metrics."""
    failure_counts = Counter(trace.status for trace in traces if trace.status != "OK")
    first_attempt_ok = sum(
        trace.parent_task_id is None and trace.attempt == 1 and trace.status == "OK"
        for trace in traces
    )
    retry_recovered = sum(task.status == "OK" and task.attempts > 1 for task in tasks)

    children_by_parent: dict[str, list[TriageTask]] = defaultdict(list)
    for task in tasks:
        if task.parent_task_id is not None:
            children_by_parent[task.parent_task_id].append(task)

    split_tasks = len(children_by_parent)
    split_recovered = sum(
        bool(children) and all(child.status == "OK" for child in children)
        for children in children_by_parent.values()
    )
    terminal_failed = sum(task.status == "FAILED" for task in tasks)

    return {
        "tasks_total": root_task_count,
        "first_attempt_ok": first_attempt_ok,
        "retry_recovered": retry_recovered,
        "split_tasks": split_tasks,
        "split_recovered": split_recovered,
        "terminal_failed": terminal_failed,
        "schema_failed": failure_counts["SCHEMA_FAILED"],
        "id_mismatch": failure_counts["ID_MISMATCH"],
        "provider_error": failure_counts["PROVIDER_ERROR"],
        "provider_timeout": failure_counts["PROVIDER_TIMEOUT"],
        "budget_exceeded": failure_counts["BUDGET_EXCEEDED"],
    }


def build_round2_execution_metrics(
    *, tasks: list[TriageTask], traces: list[AttemptTrace], root_task_count: int
) -> dict[str, int]:
    """Round2 metrics; each root is already a single-subject task so there is no split."""
    failure_counts = Counter(trace.status for trace in traces if trace.status != "OK")
    first_attempt_ok = sum(trace.attempt == 1 and trace.status == "OK" for trace in traces)
    retry_recovered = sum(
        task.status == "OK" and not task.forced_analyze and task.attempts > 1
        for task in tasks
    )
    forced = sum(task.forced_analyze for task in tasks)
    return {
        "r2_tasks_total": root_task_count,
        "r2_first_attempt_ok": first_attempt_ok,
        "r2_retry_recovered": retry_recovered,
        "r2_forced_analyze": forced,
        "r2_schema_failed": failure_counts["SCHEMA_FAILED"],
        "r2_id_mismatch": failure_counts["ID_MISMATCH"],
        "r2_provider_error": failure_counts["PROVIDER_ERROR"],
        "r2_provider_timeout": failure_counts["PROVIDER_TIMEOUT"],
        "r2_budget_exceeded": failure_counts["BUDGET_EXCEEDED"],
    }
