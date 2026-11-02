"""ANALYZE functions whose decompilation explicitly failed or timed out."""

from __future__ import annotations

from typing import Optional

from ..model import Function, PROGRAM_ANALYZE, Verdict

RULE_ID = "r103_decompile_failed"
DECISION = PROGRAM_ANALYZE
DESCRIPTION = "反编译失败,需要基于汇编分析"
JUSTIFICATION = "CORRECTNESS"
JUSTIFICATION_NOTE = "信息缺失不等于简单,必须升级而非跳过"
FAILED_STATUSES = {"FAILED", "TIMEOUT"}


def evaluate(fn: Function, ctx) -> Optional[Verdict]:
    if fn.decompile_status is None:
        return None
    status = str(fn.decompile_status).strip().upper()
    if status not in FAILED_STATUSES:
        return None
    return Verdict(PROGRAM_ANALYZE, "状态 %s" % status)
