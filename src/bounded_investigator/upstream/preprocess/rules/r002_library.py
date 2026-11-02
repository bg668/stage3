"""SKIP functions already identified by IDA/FLIRT as library code."""

from __future__ import annotations

from typing import Optional

from ..model import Function, PROGRAM_SKIP, Verdict

RULE_ID = "r002_library"
DECISION = PROGRAM_SKIP
DESCRIPTION = "FLIRT 库函数(FUNC_LIB)"
JUSTIFICATION = "CORRECTNESS"
JUSTIFICATION_NOTE = (
    "库代码不是作者写的代码,在其中找恶意能力会产生干扰性结果。边际贡献为 0 只说明本样本没有静态库代码,与规则存废无关"
)


def evaluate(fn: Function, ctx) -> Optional[Verdict]:
    if fn.is_library is None or not fn.is_library:
        return None
    return Verdict(PROGRAM_SKIP, "FUNC_LIB,识别为 %s" % fn.name)
