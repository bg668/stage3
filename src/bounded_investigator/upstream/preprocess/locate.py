"""关键函数定位器 —— 把行为级标准答案落到函数地址。

    python -m preprocess.locate --sqlite 07b8.db --api CreateRemoteThread
    python -m preprocess.locate --sqlite 07b8.db --string "\\Run"
    python -m preprocess.locate --sqlite 07b8.db --name sub_401A20
    python -m preprocess.locate --sqlite 07b8.db --behavior injection

标准答案通常是行为级的("通过 CreateRemoteThread 注入"),
而 key_functions 清单需要函数地址。所需信息其实已经在导出库里
(dangerous_api_calls / global_var_accesses / string_literals),
不必回 IDA 里逐个找交叉引用。

输出直接就是 key_functions 的格式,可以复制粘贴。

**这只是提速工具,不是自动生成器。** 它给候选,你判断哪个才是
标准答案说的那个函数 —— 引用同一个 API 的可能有好几处,
只有一处是核心逻辑,其余可能是错误处理或封装层。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .model import PROGRAM_ANALYZE, PROGRAM_SKIP, UNDECIDED
from .pipeline import run as run_pipeline
from .rules import RuleContext
from .source import SourceError, SqliteSource

# 常见恶意行为 -> 相关 API 关键词。用于 --behavior 快捷查询。
# 刻意宽松:目的是不漏候选,精确判断由人做。
BEHAVIOR_APIS = {
    "injection": ["OpenProcess", "VirtualAllocEx", "WriteProcessMemory",
                  "CreateRemoteThread", "NtWriteVirtualMemory", "QueueUserAPC",
                  "SetThreadContext", "ResumeThread", "NtMapViewOfSection"],
    "persistence": ["RegSetValue", "RegCreateKey", "CreateService",
                    "StartService", "CreateFileMapping", "CopyFile",
                    "SHGetFolderPath", "GetTempPath", "CreateProcess"],
    "c2": ["WSAConnect", "connect", "send", "recv", "InternetOpen",
           "InternetConnect", "HttpSendRequest", "WinHttp", "socket",
           "gethostbyname", "getaddrinfo"],
    "crypto": ["CryptEncrypt", "CryptDecrypt", "CryptGenKey", "CryptAcquireContext",
               "CryptHashData", "BCryptEncrypt"],
    "discovery": ["GetComputerName", "GetUserName", "GetSystemInfo",
                  "CreateToolhelp32Snapshot", "Process32", "GetVolumeInformation",
                  "GetAdaptersInfo", "RegQueryValue"],
    "evasion": ["IsDebuggerPresent", "CheckRemoteDebuggerPresent", "NtQueryInformationProcess",
                "GetTickCount", "QueryPerformanceCounter", "Sleep", "OutputDebugString"],
    "dynamic": ["LoadLibrary", "GetProcAddress", "GetModuleHandle", "LdrLoadDll"],
}

DECISION_MARK = {
    PROGRAM_ANALYZE: "已判 ANALYZE",
    PROGRAM_SKIP: "!! 已判 SKIP",
    UNDECIDED: "待分诊",
}


def _matches(fn, apis: list[str], strings: list[str], names: list[str]) -> list[str]:
    """返回命中原因列表。空列表 = 不匹配。"""
    reasons: list[str] = []
    haystack_api = [t.lower() for t in (fn.dangerous_apis + fn.api_refs)]
    for needle in apis:
        low = needle.lower()
        hit = [t for t in haystack_api if low in t]
        if hit:
            reasons.append("api:%s" % hit[0])
    for needle in strings:
        low = needle.lower()
        hit = [s for s in fn.string_refs if low in s.lower()]
        if hit:
            reasons.append('str:"%s"' % hit[0][:40])
    for needle in names:
        if needle.lower() in fn.name.lower():
            reasons.append("name:%s" % fn.name)
    return reasons


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="preprocess.locate",
        description="按 API / 字符串 / 名字定位候选关键函数,输出 key_functions 格式",
    )
    ap.add_argument("--sqlite", required=True, type=Path)
    ap.add_argument("--api", action="append", default=[],
                    help="API 名(部分匹配),可重复")
    ap.add_argument("--string", action="append", default=[],
                    help="字符串内容(部分匹配),可重复")
    ap.add_argument("--name", action="append", default=[],
                    help="函数名(部分匹配),可重复")
    ap.add_argument("--behavior", action="append", default=[],
                    choices=sorted(BEHAVIOR_APIS),
                    help="按行为类别批量查询,可重复")
    ap.add_argument("--skipped-only", action="store_true",
                    help="只列出已被 PROGRAM_SKIP 的 —— 这些是最危险的漏判")
    ap.add_argument("--limit", type=int, default=40)
    args = ap.parse_args(argv)

    apis = list(args.api)
    for behavior in args.behavior:
        apis.extend(BEHAVIOR_APIS[behavior])
    if not (apis or args.string or args.name):
        ap.error("至少需要一个 --api / --string / --name / --behavior")

    try:
        source = SqliteSource(args.sqlite)
        meta = source.read_meta()
        functions, _ = source.read_functions()
    except SourceError as exc:
        print("读取失败: %s" % exc, file=sys.stderr)
        return 2

    # 跑一遍流水线,这样能标出每个候选当前的决策
    run_pipeline(functions, meta, RuleContext())

    hits = []
    for fn in functions:
        reasons = _matches(fn, apis, args.string, args.name)
        if not reasons:
            continue
        if args.skipped_only and fn.decision != PROGRAM_SKIP:
            continue
        hits.append((fn, reasons))

    # 已被 SKIP 的排最前 —— 它们最需要你的注意
    hits.sort(key=lambda pair: (pair[0].decision != PROGRAM_SKIP, pair[0].address))

    if not hits:
        print("# 无匹配。检查 API 名拼写,或该样本确实不含此行为。")
        return 0

    print("# 候选 %d 个(%s)" % (len(hits), args.sqlite.name))
    print("# 复制需要的行到 key_functions/<样本>.txt,并把 # 后的说明改成你的判断")
    print("#")
    for fn, reasons in hits[:args.limit]:
        print("%-12s # %-14s | %-28s | %s" % (
            fn.hex_address,
            DECISION_MARK.get(fn.decision, fn.decision),
            fn.name + (" sz=%d" % fn.size),
            ", ".join(reasons[:3]),
        ))
    if len(hits) > args.limit:
        print("# ... 另有 %d 个,用 --limit 调整" % (len(hits) - args.limit))

    skipped = [f for f, _ in hits if f.decision == PROGRAM_SKIP]
    if skipped:
        print()
        print("# ⚠ 上面有 %d 个候选**已被程序判为 SKIP**。" % len(skipped))
        print("#   如果其中任何一个是标准答案里的核心函数,说明规则误杀了,")
        print("#   把它写进 key_functions 后断言会立刻 FAIL,这正是要发现的问题。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
