"""SKIP IDA ``FUNC_THUNK`` functions: the thunk has no behavior of its own."""

from __future__ import annotations

from typing import Optional

from ..model import Function, PROGRAM_SKIP, Verdict

RULE_ID = "r001_thunk"
DECISION = PROGRAM_SKIP
DESCRIPTION = "导入跳板(FUNC_THUNK)"
JUSTIFICATION = "CORRECTNESS"
JUSTIFICATION_NOTE = "跳板自身无逻辑,语义在调用方,不属于粗筛范围"


def evaluate(fn: Function, ctx) -> Optional[Verdict]:
    if fn.is_thunk is None or not fn.is_thunk:
        return None
    return Verdict(PROGRAM_SKIP, "FUNC_THUNK,目标 %s" % fn.name)
