"""ANALYZE any function with a DeepExtract dangerous-API hit."""

from __future__ import annotations

from typing import Optional

from ..model import Function, PROGRAM_ANALYZE, Verdict

RULE_ID = "r102_dangerous_api"
DECISION = PROGRAM_ANALYZE
DESCRIPTION = "引用危险 API"
JUSTIFICATION = "CORRECTNESS"
JUSTIFICATION_NOTE = "引用危险 API 属于确定性高风险信号,不需要 LLM 判断"
MIN_HITS = 1


def evaluate(fn: Function, ctx) -> Optional[Verdict]:
    hits = fn.dangerous_apis
    if len(hits) < MIN_HITS:
        return None
    preview = ", ".join(hits[:4])
    if len(hits) > 4:
        preview += " 等 %d 个" % len(hits)
    return Verdict(PROGRAM_ANALYZE, preview)
