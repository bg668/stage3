"""Shared deterministic display labels for triage verdict reasons."""

from __future__ import annotations

from typing import Any


ANALYZE_REASON_LABELS: dict[str, str] = {
    "anti-analysis": "反分析",
    "collection": "信息收集",
    "communication": "网络通信",
    "data-manipulation": "数据处理",
    "host-interaction": "主机交互",
    "impact": "系统/数据影响",
    "linking": "关键连接",
    "load-code": "代码加载/执行",
    "persistence": "持久化",
    "other": "其他需精读",
}

_REASON_TEXT = {
    "S_WRAPPER": "主要是薄包装或转发",
    "S_PURE_COMPUTE": "主要是通用计算或数据处理",
    "S_ERROR_PATH": "主要落在错误处理或清理路径",
    "S_RUNTIME_HELPER": "主要是编译器或运行时辅助逻辑",
    "anti-analysis": "存在反分析相关行为",
    "collection": "涉及信息收集",
    "communication": "涉及网络通信或远程交互",
    "data-manipulation": "存在值得继续追的数据变换",
    "host-interaction": "涉及进程、文件、注册表等主机交互",
    "impact": "存在可能影响系统或数据的行为",
    "linking": "连接了关键对象、数据或执行路径",
    "load-code": "涉及代码加载、映射、注入或执行",
    "persistence": "涉及持久化行为",
    "other": "当前行为仍值得继续精读",
    "U_TRUNCATED": "主体代码可能不完整",
    "U_CALLEE_OPAQUE": "关键语义依赖当前不可见的被调函数",
    "U_CALLER_CONTEXT": "需要调用者上下文才能确认用途",
    "U_INDIRECT": "间接调用目标还不明确",
    "U_DATA_OPAQUE": "关键数据来源或用途还不明确",
}


def display_verdict_reason(verdict: dict[str, Any], *, limit: int = 120) -> str:
    note = strip_terminal_punctuation(compact(str(verdict.get("note") or ""), limit))
    if note and contains_cjk(note):
        return note
    reason = str(verdict.get("reason") or "")
    return _REASON_TEXT.get(reason, note or reason)


def compact(text: str, limit: int) -> str:
    value = " ".join(text.split())
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 1)].rstrip() + "…"


def contains_cjk(text: str) -> bool:
    return any("\u4e00" <= char <= "\u9fff" for char in text)


def strip_terminal_punctuation(text: str) -> str:
    return text.rstrip("。.!！?？；;，,")
