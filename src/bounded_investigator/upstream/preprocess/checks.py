"""Deterministic acceptance checks for preprocessing output."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from .model import Function, PROGRAM_ANALYZE, PROGRAM_SKIP, UNDECIDED

_ADDR_RE = re.compile(r"^(0[xX][0-9a-fA-F]+|[0-9]+)$")
IDENTITY_RULES = {"r001_thunk", "r002_library", "r003_jump_stub", "r004_nullsub"}
REASON_MAP = {
    "r001_thunk": "FUNC_THUNK",
    "r002_library": "FUNC_LIB",
    "r003_jump_stub": "NAME_J_PREFIX",
    "r004_nullsub": "NAME_NULLSUB",
}


def _read_text_tolerant(path: Path) -> str:
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "gbk"):
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            pass
    return raw.decode("utf-8", errors="replace")


def load_key_functions(path: Optional[Path]) -> set[int]:
    if path is None or not Path(path).exists():
        return set()
    addresses: set[int] = set()
    for raw in _read_text_tolerant(Path(path)).splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        token = line.split()[0]
        if _ADDR_RE.match(token):
            addresses.add(int(token, 16) if token.lower().startswith("0x") else int(token))
    return addresses


def check_partition_complete(functions: list[Function]) -> dict:
    counts = {PROGRAM_ANALYZE: 0, PROGRAM_SKIP: 0, UNDECIDED: 0}
    bad: list[str] = []
    for fn in functions:
        if fn.decision not in counts:
            bad.append("%s 决策值非法: %r" % (fn.ref, fn.decision))
            continue
        counts[fn.decision] += 1
        if fn.decision in (PROGRAM_ANALYZE, PROGRAM_SKIP):
            if not fn.decision_reason:
                bad.append("%s 已决策但缺少 reason" % fn.ref)
        elif fn.decision_reason is not None:
            bad.append("%s 为 UNDECIDED 却带 reason" % fn.ref)

    total = sum(counts.values())
    passed = total == len(functions) and not bad
    return {
        "name": "partition_complete",
        "passed": passed,
        "detail": (
            "三分之和 %d == scope %d" % (total, len(functions))
            if passed
            else "; ".join(bad[:5]) or "三分之和 %d != scope %d" % (total, len(functions))
        ),
        "violations": bad[:50],
    }


def check_key_functions(functions: list[Function], key_addresses: set[int]) -> dict:
    if not key_addresses:
        return {
            "name": "key_functions_retained",
            "passed": True,
            "detail": "未提供关键函数清单,断言跳过(强烈建议补齐)",
            "skipped": True,
            "violations": [],
        }

    by_address = {fn.address: fn for fn in functions}
    missing, killed = [], []
    for address in sorted(key_addresses):
        fn = by_address.get(address)
        if fn is None:
            missing.append("0x%X 不在导出中" % address)
        elif fn.decision == PROGRAM_SKIP:
            killed.append(
                "0x%X %s 被 %s 误杀 (%s)"
                % (address, fn.name, fn.decision_reason, fn.decision_detail)
            )

    passed = not killed and not missing
    return {
        "name": "key_functions_retained",
        "passed": passed,
        "detail": (
            "%d 个关键函数全部保留" % len(key_addresses)
            if passed
            else "; ".join((killed + missing)[:5])
        ),
        "violations": killed + missing,
    }


def check_candidate_cross_check(functions: list[Function]) -> dict:
    """Cross-check identity SKIP rules against export-side ``analysis_candidate``."""
    have = [fn for fn in functions if fn.analysis_candidate is not None]
    if not have:
        return {
            "name": "candidate_cross_check",
            "passed": True,
            "detail": "导出中无 analysis_candidate 列,交叉校验跳过",
            "skipped": True,
            "violations": [],
        }

    over_kill, under_kill, null_mishandled = [], [], []
    for fn in functions:
        identity_skip = fn.decision == PROGRAM_SKIP and fn.decision_reason in IDENTITY_RULES
        if fn.analysis_candidate is None:
            if fn.decision != UNDECIDED:
                null_mishandled.append(
                    "%s candidate 为 NULL 却被判 %s" % (fn.ref, fn.decision)
                )
            continue
        if identity_skip and fn.analysis_candidate:
            over_kill.append(
                "%s %s 被 %s 判 SKIP,但导出侧 candidate=1"
                % (fn.ref, fn.name, fn.decision_reason)
            )
        if not identity_skip and not fn.analysis_candidate:
            if fn.decision == PROGRAM_ANALYZE:
                continue
            under_kill.append(
                "%s %s 导出侧 candidate=0,但身份层未判 SKIP" % (fn.ref, fn.name)
            )

    violations = over_kill + under_kill + null_mishandled
    passed = not violations
    return {
        "name": "candidate_cross_check",
        "passed": passed,
        "detail": (
            "与导出侧一致,共比对 %d 个函数" % len(have)
            if passed
            else "多杀 %d / 漏判 %d / NULL 处理错 %d"
            % (len(over_kill), len(under_kill), len(null_mishandled))
        ),
        "violations": violations[:50],
    }


def check_no_truncation(functions: list[Function], total_functions) -> dict:
    if total_functions is None:
        return {
            "name": "no_truncation",
            "passed": True,
            "detail": "导出中无 total_functions,断言跳过",
            "skipped": True,
            "violations": [],
        }
    passed = len(functions) == total_functions
    return {
        "name": "no_truncation",
        "passed": passed,
        "detail": (
            "导出 %d == IDA 函数总数 %d" % (len(functions), total_functions)
            if passed
            else "导出 %d != IDA 函数总数 %d,导出可能被截断"
            % (len(functions), total_functions)
        ),
        "violations": (
            [] if passed else ["缺失 %d 个函数" % (total_functions - len(functions))]
        ),
    }


def check_skip_reason_agreement(functions: list[Function]) -> dict:
    comparable = [
        fn
        for fn in functions
        if fn.export_skip_reason and fn.decision_reason in REASON_MAP
    ]
    if not comparable:
        return {
            "name": "skip_reason_agreement",
            "passed": True,
            "detail": "导出中无 skip_reason 或无可比对项,断言跳过",
            "skipped": True,
            "violations": [],
        }

    bad = []
    for fn in comparable:
        expected = REASON_MAP[fn.decision_reason]
        actual = str(fn.export_skip_reason).strip().upper()
        if actual != expected:
            bad.append(
                "%s %s: 我判 %s(期望 %s),导出侧记 %s"
                % (fn.ref, fn.name, fn.decision_reason, expected, actual)
            )
    return {
        "name": "skip_reason_agreement",
        "passed": not bad,
        "detail": (
            "%d 项理由全部一致" % len(comparable)
            if not bad
            else "%d / %d 项理由不一致" % (len(bad), len(comparable))
        ),
        "violations": bad[:50],
    }


def run_all(
    functions: list[Function],
    key_addresses: set[int],
    total_functions=None,
) -> list[dict]:
    return [
        check_partition_complete(functions),
        check_no_truncation(functions, total_functions),
        check_key_functions(functions, key_addresses),
        check_candidate_cross_check(functions),
        check_skip_reason_agreement(functions),
    ]
