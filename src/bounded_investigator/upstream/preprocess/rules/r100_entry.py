"""ANALYZE program entry points."""

from __future__ import annotations

from typing import Optional

from ..model import Function, PROGRAM_ANALYZE, Verdict

RULE_ID = "r100_entry"
DECISION = PROGRAM_ANALYZE
DESCRIPTION = "程序入口点"
JUSTIFICATION = "CORRECTNESS"
JUSTIFICATION_NOTE = "入口点必然被执行,不分析它是覆盖率缺口,与命中数无关"


def evaluate(fn: Function, ctx) -> Optional[Verdict]:
    if fn.is_entry is None or not fn.is_entry:
        return None
    return Verdict(PROGRAM_ANALYZE, "程序入口 %s" % fn.hex_address)
