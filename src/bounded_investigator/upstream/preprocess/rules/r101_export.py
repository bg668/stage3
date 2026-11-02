"""ANALYZE exported functions because they are external roots."""

from __future__ import annotations

from typing import Optional

from ..model import Function, PROGRAM_ANALYZE, Verdict

RULE_ID = "r101_export"
DECISION = PROGRAM_ANALYZE
DESCRIPTION = "导出函数"
JUSTIFICATION = "CORRECTNESS"
JUSTIFICATION_NOTE = "导出函数是外部可达入口,同上"


def evaluate(fn: Function, ctx) -> Optional[Verdict]:
    if fn.is_export is None or not fn.is_export:
        return None
    return Verdict(PROGRAM_ANALYZE, "导出名 %s" % (fn.export_name or fn.name))
