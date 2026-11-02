"""SKIP IDA ``j_`` jump stubs."""

from __future__ import annotations

from typing import Optional

from ..model import Function, PROGRAM_SKIP, Verdict

RULE_ID = "r003_jump_stub"
DECISION = PROGRAM_SKIP
DESCRIPTION = "跳转桩(j_ 前缀)"
JUSTIFICATION = "CORRECTNESS"
JUSTIFICATION_NOTE = "同 thunk,自身无逻辑"
PREFIX = "j_"


def evaluate(fn: Function, ctx) -> Optional[Verdict]:
    if not fn.name.strip().lower().startswith(PREFIX):
        return None
    return Verdict(PROGRAM_SKIP, "名字前缀 %s" % PREFIX)
