"""SKIP small leaf functions only when structural evidence is complete and quiet."""

from __future__ import annotations

from typing import Optional

from ..model import Function, PROGRAM_SKIP, Verdict

RULE_ID = "r006_trivial_leaf"
DECISION = PROGRAM_SKIP
DESCRIPTION = "结构平凡的叶子函数"
JUSTIFICATION = "EFFICIENCY"
JUSTIFICATION_NOTE = "'小的叶子函数不重要'是统计判断而非原理。边际贡献持续为 0 时应当重新考虑"
MISSING_CODE_STATUSES = {"FAILED", "TIMEOUT", "UNAVAILABLE"}


def evaluate(fn: Function, ctx) -> Optional[Verdict]:
    if fn.has_indirect_call is None or fn.has_indirect_call:
        return None
    if not fn.has_code:
        return None
    if (
        fn.decompile_status
        and str(fn.decompile_status).strip().upper() in MISSING_CODE_STATUSES
    ):
        return None
    if not fn.is_leaf:
        return None
    if fn.dangerous_apis or fn.string_refs or fn.api_refs:
        return None
    if fn.loop_count:
        return None
    if fn.size > ctx.trivial_max_size or len(fn.code) > ctx.trivial_max_code_chars:
        return None
    return Verdict(
        PROGRAM_SKIP,
        "叶子/无 API/无字符串/无循环,size=%d code=%d" % (fn.size, len(fn.code)),
    )
