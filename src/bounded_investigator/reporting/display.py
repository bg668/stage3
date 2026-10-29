"""Resolve display fields consistently for validation and HTML compilation."""

import re
from typing import Any


_GENERIC_HTML_LIMITATION = re.compile(
    r'(?:本报告|本次分析|当前分析|本次|当前)?(?:'
    r'(?:仅|只)(?:基于|进行|进行了|完成|完成了)?静态分析|'
    r'基于静态分析|'
    r'(?:未|尚未|没有)(?:实际)?(?:执行|运行)(?:该)?样本|'
    r'(?:未|尚未)(?:进行|完成)?(?:动态分析|动态验证|运行时验证)'
    r'|静态分析(?:不能|无法)证明(?:运行时|实际运行)(?:一定|必然)?成功'
    r'|(?:无法确认|不能断言|不代表|不证明|调用存在不证明)(?:运行时|实际运行)(?:一定|必然)?成功'
    r'|不能断言每次运行都会创建全部文件'
    r'|(?:does not prove runtime success|runtime success cannot be guaranteed|may fail at runtime)'
    r')', re.IGNORECASE,
)


def field_text(item: Any, field: str) -> str:
    if field == 'evidence':
        return '；'.join(f'{ref.location}：{ref.observation}' for ref in item.evidence)
    if field == 'limitations':
        displayed = []
        for limitation in item.limitations:
            clauses = [part.strip() for part in re.split(r'[，,；;。.!！\n]+', limitation) if part.strip()]
            if clauses and all(_GENERIC_HTML_LIMITATION.fullmatch(part) for part in clauses):
                continue
            displayed.append(limitation)
        return '；'.join(displayed)
    return getattr(item, field)
