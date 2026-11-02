"""Hard assertions over the merged stage-2 triage result."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from .domain import FinalVerdict, TriageTask, reason_matches_decision
from .second_hop_domain import SecondHopPackage


def run_all(
    *,
    undecided_refs: tuple[str, ...],
    tasks: list[TriageTask],
    final_verdicts: list[FinalVerdict],
    second_hop_dir: Path,
    second_hop_packages: list[SecondHopPackage],
    round2_enabled: bool,
) -> list[dict]:
    verdicts = [verdict for task in tasks for verdict in task.verdicts]
    checks = [
        _round1_complete(undecided_refs, tasks),
        _task_ref_integrity(tasks),
        _reason_decision_compatibility(verdicts),
        _round2_has_no_uncertain(tasks),
        _verdict_field_scope(verdicts),
        _final_verdicts_complete(undecided_refs, final_verdicts),
        _final_verdicts_match_audit(final_verdicts, tasks),
        _second_hop_complete(tasks, second_hop_dir, second_hop_packages),
        _second_hop_assembly_health(second_hop_packages, tasks, round2_enabled),
    ]
    if round2_enabled:
        checks.insert(1, _round2_complete(tasks))
        checks.insert(2, _forced_analyze_integrity(tasks))
    return checks


def _assertion(name: str, violations: list[str], ok_detail: str) -> dict:
    return {
        "name": name,
        "passed": not violations,
        "detail": ok_detail if not violations else "; ".join(violations[:5]),
        "violations": violations[:50],
    }


def _round1_complete(undecided_refs, tasks: list[TriageTask]) -> dict:
    verdicts = [
        verdict
        for task in tasks
        if task.round == 1 and task.status == "OK"
        for verdict in task.verdicts
    ]
    counts = Counter(v.ref for v in verdicts)
    missing = [ref for ref in undecided_refs if counts[ref] == 0]
    duplicate = [ref for ref in undecided_refs if counts[ref] > 1]
    unexpected = sorted(set(counts) - set(undecided_refs))
    violations = []
    if missing:
        violations.append("缺少 Round1 verdict: %s" % missing)
    if duplicate:
        violations.append("Round1 verdict 重复: %s" % duplicate)
    if unexpected:
        violations.append("出现非 UNDECIDED ref: %s" % unexpected)
    return _assertion(
        "round1_complete",
        violations,
        "%d 个 UNDECIDED 全部得到唯一 Round1 verdict" % len(undecided_refs),
    )


def _round2_complete(tasks: list[TriageTask]) -> dict:
    uncertain_refs = [
        verdict.ref
        for task in tasks
        if task.round == 1 and task.status == "OK"
        for verdict in task.verdicts
        if verdict.decision == "UNCERTAIN"
    ]
    r2 = [
        verdict.ref
        for task in tasks
        if task.round == 2 and task.status == "OK"
        for verdict in task.verdicts
    ]
    counts = Counter(r2)
    missing = [ref for ref in uncertain_refs if counts[ref] == 0]
    duplicate = [ref for ref in uncertain_refs if counts[ref] > 1]
    extra = sorted(set(r2) - set(uncertain_refs))
    violations: list[str] = []
    if missing:
        violations.append("Round2 缺少 ref: %s" % missing)
    if duplicate:
        violations.append("Round2 ref 重复: %s" % duplicate)
    if extra:
        violations.append("Round2 出现非 UNCERTAIN ref: %s" % extra)
    return _assertion(
        "round2_complete",
        violations,
        "%d 个 Round1 UNCERTAIN 均得到唯一 Round2 终态" % len(uncertain_refs),
    )


def _forced_analyze_integrity(tasks: list[TriageTask]) -> dict:
    violations: list[str] = []
    forced = [task for task in tasks if task.round == 2 and task.forced_analyze]
    for task in forced:
        if task.status != "OK" or len(task.verdicts) != 1:
            violations.append("%s forced task 非 OK/单 verdict" % task.task_id)
            continue
        verdict = task.verdicts[0]
        if verdict.decision != "ANALYZE":
            violations.append("%s forced verdict=%s" % (task.task_id, verdict.decision))
        if task.failure_kind is None:
            violations.append("%s forced task 缺 failure_kind" % task.task_id)
    return _assertion(
        "forced_analyze_integrity",
        violations,
        "%d 个 forced fallback 全部保守收敛为 ANALYZE" % len(forced),
    )


def _task_ref_integrity(tasks: list[TriageTask]) -> dict:
    violations: list[str] = []
    for task in tasks:
        if task.status != "OK":
            violations.append("%s status=%s (%s)" % (task.task_id, task.status, task.error or ""))
            continue
        actual = [verdict.ref for verdict in task.verdicts]
        if actual != task.subject_refs:
            violations.append(
                "%s refs=%s subjects=%s" % (task.task_id, actual, task.subject_refs)
            )
    return _assertion(
        "task_ref_integrity", violations, "%d 个 task 的 ref 与 subject_refs 完全一致" % len(tasks)
    )


def _reason_decision_compatibility(verdicts) -> dict:
    violations = [
        "%s %s/%s" % (v.ref, v.decision, v.reason)
        for v in verdicts
        if not reason_matches_decision(v.decision, v.reason)
    ]
    return _assertion(
        "reason_decision_compatibility",
        violations,
        "%d 条 verdict 的 decision/reason 组配合法" % len(verdicts),
    )


def _round2_has_no_uncertain(tasks: list[TriageTask]) -> dict:
    violations = [
        verdict.ref
        for task in tasks
        if task.round == 2
        for verdict in task.verdicts
        if verdict.decision == "UNCERTAIN"
    ]
    return _assertion(
        "round2_no_uncertain", violations, "round 2 未出现 UNCERTAIN"
    )


def _verdict_field_scope(verdicts) -> dict:
    allowed = {"ref", "decision", "reason", "note"}
    violations = []
    for verdict in verdicts:
        extra = set(verdict.model_dump()) - allowed
        if extra:
            violations.append("%s 多余字段: %s" % (verdict.ref, sorted(extra)))
    return _assertion(
        "verdict_field_scope",
        violations,
        "verdict 仅包含 ref/decision/reason/note",
    )



def _final_verdicts_complete(
    undecided_refs: tuple[str, ...], final_verdicts: list[FinalVerdict]
) -> dict:
    refs = [row.ref for row in final_verdicts]
    counts = Counter(refs)
    expected = set(undecided_refs)
    actual = set(refs)
    missing = [ref for ref in undecided_refs if counts[ref] == 0]
    duplicate = [ref for ref in undecided_refs if counts[ref] > 1]
    extra = sorted(actual - expected)
    order_ok = refs == [ref for ref in undecided_refs if ref in actual]
    violations: list[str] = []
    if missing:
        violations.append("缺少 final verdict: %s" % missing)
    if duplicate:
        violations.append("final verdict 重复: %s" % duplicate)
    if extra:
        violations.append("出现非 UNDECIDED ref: %s" % extra)
    if not order_ok:
        violations.append("final_verdicts 未保持 preprocess UNDECIDED 顺序")
    return _assertion(
        "final_verdicts_complete",
        violations,
        "%d 个 UNDECIDED 均有唯一、有序的最终 ANALYZE/SKIP" % len(undecided_refs),
    )


def _final_verdicts_match_audit(
    final_verdicts: list[FinalVerdict], tasks: list[TriageTask]
) -> dict:
    by_round: dict[tuple[str, int], list[tuple[TriageTask, object]]] = {}
    for task in tasks:
        for verdict in task.verdicts:
            by_round.setdefault((verdict.ref, task.round), []).append((task, verdict))

    violations: list[str] = []
    for row in final_verdicts:
        r1 = by_round.get((row.ref, 1), [])
        if len(r1) != 1:
            violations.append("%s Round1 audit=%d" % (row.ref, len(r1)))
            continue
        r1_task, r1_verdict = r1[0]
        if r1_task.status != "OK":
            violations.append("%s Round1 status=%s" % (row.ref, r1_task.status))
            continue

        if row.source_round == 1:
            if r1_verdict.decision not in ("ANALYZE", "SKIP"):
                violations.append("%s source_round=1 但 Round1=%s" % (row.ref, r1_verdict.decision))
                continue
            if not _final_matches_verdict(row, r1_verdict):
                violations.append("%s final 与 Round1 verdict 不一致" % row.ref)
            if row.forced_analyze or row.failure_kind is not None:
                violations.append("%s Round1 final 带 forced/failure" % row.ref)
            continue

        if r1_verdict.decision != "UNCERTAIN":
            violations.append("%s source_round=2 但 Round1=%s" % (row.ref, r1_verdict.decision))
            continue
        r2 = by_round.get((row.ref, 2), [])
        if len(r2) != 1:
            violations.append("%s Round2 audit=%d" % (row.ref, len(r2)))
            continue
        r2_task, r2_verdict = r2[0]
        if r2_task.status != "OK" or r2_verdict.decision not in ("ANALYZE", "SKIP"):
            violations.append("%s Round2 非有效终态" % row.ref)
            continue
        if not _final_matches_verdict(row, r2_verdict):
            violations.append("%s final 与 Round2 verdict 不一致" % row.ref)
        if row.forced_analyze != r2_task.forced_analyze:
            violations.append("%s forced_analyze 与 task 不一致" % row.ref)
        if row.failure_kind != r2_task.failure_kind:
            violations.append("%s failure_kind 与 task 不一致" % row.ref)

    return _assertion(
        "final_verdicts_match_audit",
        violations,
        "%d 条 final verdict 均可唯一回溯到 task/verdict 审计记录" % len(final_verdicts),
    )


def _final_matches_verdict(final: FinalVerdict, verdict) -> bool:
    return (
        final.final_decision == verdict.decision
        and final.reason == verdict.reason
        and final.note == verdict.note
    )

def _second_hop_complete(
    tasks: list[TriageTask],
    second_hop_dir: Path,
    packages: list[SecondHopPackage],
) -> dict:
    uncertain_refs = [
        verdict.ref
        for task in tasks
        if task.round == 1 and task.status == "OK"
        for verdict in task.verdicts
        if verdict.decision == "UNCERTAIN"
    ]
    package_refs = [package.subject_ref for package in packages]
    counts = Counter(package_refs)
    missing_packages = [ref for ref in uncertain_refs if counts[ref] == 0]
    duplicate_packages = [ref for ref, count in counts.items() if count > 1]
    extra_packages = sorted(set(package_refs) - set(uncertain_refs))
    missing_files = [
        ref for ref in uncertain_refs if not (second_hop_dir / (ref + ".txt")).is_file()
    ]
    violations: list[str] = []
    if missing_packages:
        violations.append("缺少 package: %s" % missing_packages)
    if duplicate_packages:
        violations.append("package 重复: %s" % duplicate_packages)
    if extra_packages:
        violations.append("出现非 UNCERTAIN package: %s" % extra_packages)
    if missing_files:
        violations.append("缺少 second_hop 文件: %s" % missing_files)
    return _assertion(
        "second_hop_complete",
        violations,
        "%d 个 UNCERTAIN 均有唯一 package 和 second_hop 文件" % len(uncertain_refs),
    )


def _second_hop_assembly_health(
    packages: list[SecondHopPackage], tasks: list[TriageTask], round2_enabled: bool
) -> dict:
    failed = [package for package in packages if package.status == "FAILED"]
    if not round2_enabled:
        violations = [
            "%s: %s" % (package.subject_ref, package.error or "assembly failed")
            for package in failed
        ]
        return _assertion(
            "second_hop_assembly_health",
            violations,
            "%d 个 second-hop package 无程序级装配失败" % len(packages),
        )

    forced_refs = {
        task.subject_refs[0]
        for task in tasks
        if task.round == 2 and task.forced_analyze and task.subject_refs
    }
    unsafe = [package.subject_ref for package in failed if package.subject_ref not in forced_refs]
    return _assertion(
        "second_hop_assembly_health",
        ["装配失败但未 forced ANALYZE: %s" % unsafe] if unsafe else [],
        "%d 个装配失败 package 均通过 forced ANALYZE 安全收敛" % len(failed),
    )
