"""Derive Stage-2 authoritative terminal verdicts from immutable task audit state."""

from __future__ import annotations

from collections import defaultdict

from .domain import FinalVerdict, FunctionVerdict, TriageTask


def build_final_verdicts(
    undecided_refs: tuple[str, ...],
    tasks: list[TriageTask],
) -> list[FinalVerdict]:
    """Build terminal verdicts in Stage-1 preprocess order.

    The function is deliberately conservative: only refs with a unique, auditable
    terminal path are emitted. Hard assertions own completeness reporting, so a
    partially failed Stage-2 run can still persist its audit facts without inventing
    a final decision.
    """

    by_round = _index_task_verdicts(tasks)
    result: list[FinalVerdict] = []

    for ref in undecided_refs:
        r1 = by_round.get((ref, 1), [])
        if len(r1) != 1:
            continue
        r1_task, r1_verdict = r1[0]
        if r1_task.status != "OK":
            continue

        if r1_verdict.decision in ("ANALYZE", "SKIP"):
            result.append(
                FinalVerdict(
                    ref=ref,
                    final_decision=r1_verdict.decision,
                    reason=r1_verdict.reason,
                    note=r1_verdict.note,
                    source_round=1,
                    forced_analyze=False,
                    failure_kind=None,
                )
            )
            continue

        if r1_verdict.decision != "UNCERTAIN":
            continue

        r2 = by_round.get((ref, 2), [])
        if len(r2) != 1:
            continue
        r2_task, r2_verdict = r2[0]
        if r2_task.status != "OK" or r2_verdict.decision not in ("ANALYZE", "SKIP"):
            continue
        result.append(
            FinalVerdict(
                ref=ref,
                final_decision=r2_verdict.decision,
                reason=r2_verdict.reason,
                note=r2_verdict.note,
                source_round=2,
                forced_analyze=r2_task.forced_analyze,
                failure_kind=r2_task.failure_kind,
            )
        )

    return result


def _index_task_verdicts(
    tasks: list[TriageTask],
) -> dict[tuple[str, int], list[tuple[TriageTask, FunctionVerdict]]]:
    index: dict[tuple[str, int], list[tuple[TriageTask, FunctionVerdict]]] = defaultdict(list)
    for task in tasks:
        for verdict in task.verdicts:
            index[(verdict.ref, task.round)].append((task, verdict))
    return dict(index)
