"""SKIP functions confirmed to have no analyzable code."""

from __future__ import annotations

from typing import Optional

from ..model import Function, PROGRAM_SKIP, Verdict

RULE_ID = "r005_empty_code"
DECISION = PROGRAM_SKIP
DESCRIPTION = "无可分析代码"
JUSTIFICATION = "CORRECTNESS"
JUSTIFICATION_NOTE = "确认为空(非失败)的函数没有可分析内容"
MAX_ASM_CHARS_WHEN_UNKNOWN = 120


def evaluate(fn: Function, ctx) -> Optional[Verdict]:
    if fn.has_code:
        return None
    if fn.decompile_status is not None:
        status = str(fn.decompile_status).strip().upper()
        if status in ("FAILED", "TIMEOUT"):
            return None
        return Verdict(PROGRAM_SKIP, "decompile_status=%s" % status)
    if len(fn.assembly.strip()) <= MAX_ASM_CHARS_WHEN_UNKNOWN:
        return Verdict(
            PROGRAM_SKIP,
            "无伪代码且汇编 <= %d 字符" % MAX_ASM_CHARS_WHEN_UNKNOWN,
        )
    return None
