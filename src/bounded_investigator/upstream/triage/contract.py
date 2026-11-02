"""Serialize stage-2 runtime state into ``triage.json``."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path

from .domain import AttemptTrace, FinalVerdict, TriageTask, UNCERTAIN_REASONS
from .input import TriageInput
from .metrics import build_execution_metrics, build_round2_execution_metrics
from .second_hop_domain import SecondHopPackage

SCHEMA_VERSION = 1



def build_payload(
    *,
    snapshot: TriageInput,
    tasks: list[TriageTask],
    traces: list[AttemptTrace],
    round1_root_task_count: int,
    round2_root_task_count: int,
    second_hop_packages: list[SecondHopPackage],
    final_verdicts: list[FinalVerdict],
    assertions: list[dict],
    config: dict,
    wall_ms: int,
) -> dict:
    verdict_rows = []
    for task in tasks:
        for verdict in task.verdicts:
            verdict_rows.append(
                {
                    "ref": verdict.ref,
                    "round": task.round,
                    "decision": verdict.decision,
                    "reason": verdict.reason,
                    "note": verdict.note,
                    "task_id": task.task_id,
                }
            )

    r1 = [row for row in verdict_rows if row["round"] == 1]
    r2 = [row for row in verdict_rows if row["round"] == 2]
    r1_uncertain = sum(row["decision"] == "UNCERTAIN" for row in r1)
    undecided_in = len(snapshot.undecided_refs)
    escalated = len({row["ref"] for row in r2})

    usage = {
        "input_tokens": sum(trace.usage.input_tokens for trace in traces),
        "output_tokens": sum(trace.usage.output_tokens for trace in traces),
        "requests": sum(trace.usage.requests for trace in traces),
        "wall_ms": wall_ms,
        "est_cost": 0.0,
    }
    r1_tasks = [task for task in tasks if task.round == 1]
    r2_tasks = [task for task in tasks if task.round == 2]
    r1_traces = [trace for trace in traces if trace.round == 1]
    r2_traces = [trace for trace in traces if trace.round == 2]
    execution = build_execution_metrics(
        tasks=r1_tasks, traces=r1_traces, root_task_count=round1_root_task_count
    )
    r2_execution = build_round2_execution_metrics(
        tasks=r2_tasks, traces=r2_traces, root_task_count=round2_root_task_count
    )
    second_hop_stats = {
        "second_hop_packages": len(second_hop_packages),
        "second_hop_complete": sum(p.status == "COMPLETE" for p in second_hop_packages),
        "second_hop_partial": sum(p.status == "PARTIAL" for p in second_hop_packages),
        "second_hop_failed": sum(p.status == "FAILED" for p in second_hop_packages),
        "second_hop_truncated": sum(p.truncated for p in second_hop_packages),
    }

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "sample": dict(snapshot.sample),
        "preprocess_ref": {
            "path": str(snapshot.preprocess_path),
            "sha256": _sha256_file(snapshot.preprocess_path),
        },
        "config": dict(config),
        "stats": {
            "undecided_in": undecided_in,
            "r1_analyze": sum(row["decision"] == "ANALYZE" for row in r1),
            "r1_skip": sum(row["decision"] == "SKIP" for row in r1),
            "r1_uncertain": r1_uncertain,
            "escalated": escalated,
            "r2_total": len(r2),
            "r2_analyze": sum(row["decision"] == "ANALYZE" for row in r2),
            "r2_skip": sum(row["decision"] == "SKIP" for row in r2),
            "final_analyze": sum(row.final_decision == "ANALYZE" for row in final_verdicts),
            "final_skip": sum(row.final_decision == "SKIP" for row in final_verdicts),
            "forced_analyze_on_failure": sum(
                task.forced_analyze for task in r2_tasks
            ),
            "r2_by_uncertain_reason": _round2_by_uncertain_reason(r1, r2_tasks),
            "uncertain_rate": round(r1_uncertain / undecided_in, 6) if undecided_in else 0.0,
            "escalation_rate": round(escalated / undecided_in, 6) if undecided_in else 0.0,
            **execution,
            **r2_execution,
            **second_hop_stats,
        },
        "usage": usage,
        "tasks": [_task_to_dict(task) for task in tasks],
        "verdicts": verdict_rows,
        "final_verdicts": [row.model_dump() for row in final_verdicts],
        "second_hop": [
            package.public_dict(path="second_hop/%s.txt" % package.subject_ref)
            for package in second_hop_packages
        ],
        "assertions": assertions,
    }


def _round2_by_uncertain_reason(r1_rows: list[dict], r2_tasks: list[TriageTask]) -> dict:
    reasons = {
        row["ref"]: row["reason"]
        for row in r1_rows
        if row["decision"] == "UNCERTAIN"
    }
    result = {
        reason: {"total": 0, "analyze": 0, "skip": 0, "forced": 0}
        for reason in sorted(UNCERTAIN_REASONS)
    }
    for task in r2_tasks:
        if not task.verdicts:
            continue
        verdict = task.verdicts[0]
        original_reason = reasons.get(verdict.ref)
        if original_reason is None:
            continue
        bucket = result[original_reason]
        bucket["total"] += 1
        if verdict.decision == "ANALYZE":
            bucket["analyze"] += 1
        elif verdict.decision == "SKIP":
            bucket["skip"] += 1
        if task.forced_analyze:
            bucket["forced"] += 1
    return result


def _task_to_dict(task: TriageTask) -> dict:
    return {
        "task_id": task.task_id,
        "parent_task_id": task.parent_task_id,
        "unit_id": task.unit_id,
        "round": task.round,
        "subject_refs": list(task.subject_refs),
        "input_sha256": task.input_sha256,
        "template_id": task.template_id,
        "status": task.status,
        "failure_kind": task.failure_kind,
        "forced_analyze": task.forced_analyze,
        "model_id": task.model_id,
        "usage": task.usage.model_dump(),
        "attempts": task.attempts,
        "error": task.error,
    }


def _sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()
