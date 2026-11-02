"""SKIP IDA ``nullsub_`` empty functions."""

from __future__ import annotations

from typing import Optional

from ..model import Function, PROGRAM_SKIP, Verdict

RULE_ID = "r004_nullsub"
DECISION = PROGRAM_SKIP
DESCRIPTION = "空函数(nullsub_ 前缀)"
JUSTIFICATION = "CORRECTNESS"
JUSTIFICATION_NOTE = "空函数没有任何行为"
PREFIX = "nullsub_"


def evaluate(fn: Function, ctx) -> Optional[Verdict]:
    if not fn.name.strip().lower().startswith(PREFIX):
        return None
    return Verdict(PROGRAM_SKIP, "名字前缀 %s" % PREFIX)
