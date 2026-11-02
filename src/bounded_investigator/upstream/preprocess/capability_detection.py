"""Small deterministic capability detector used before LLM triage.

This is deliberately not a capa reimplementation.  It contains only the
high-specificity combinations needed by the current samples and emits a small,
stable contract that can later be populated by a real capa adapter.
"""

from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass
from typing import Iterable

from .model import Function


HIGH = "HIGH"
MEDIUM = "MEDIUM"

_CALL_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(")
_QUOTED_RE = re.compile(r'"([^"\r\n]{1,512})"')
_GLOBAL_ASSIGN_RE = re.compile(
    r"\b(?:byte|word|dword|qword|off|unk|flt|dbl)_[0-9A-Fa-f]+\b\s*(?==)"
)
_FIXED_LOOP_RE = re.compile(
    r"(?:while|for)\s*\([^\)]{0,160}?(?:!=|<|<=)\s*(\d{1,4})\b",
    re.IGNORECASE,
)
_AUTORUN_RE = re.compile(
    r"\\(?:CurrentVersion\\)?(?:Policies\\Explorer\\)?Run\b|"
    r"Installed Components|StubPath|Active Setup",
    re.IGNORECASE,
)
_MUTEX_RE = re.compile(r"_x_X_[A-Za-z0-9_]+_X_x_", re.IGNORECASE)
_OUTPUT_RE = re.compile(r"\b[A-Za-z0-9_-]+\.(?:abc|dat)\b", re.IGNORECASE)
_PE_RE = re.compile(
    r"\b(?:IMAGE_[A-Z0-9_]+|SizeOfImage|AddressOfEntryPoint|BaseOfCode)\b|"
    r"0x(?:5A4D|4550)\b",
    re.IGNORECASE,
)
_ANTI_STRING_RE = re.compile(
    r"Sandboxie|SbieDll|VBox|VMware|VirtualBox|dbghelp|SoftICE|NTICE|"
    r"IsDebuggerPresent|\\\\\.\\(?:SICE|NTICE)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class CapabilityEvidence:
    kind: str
    detail: str

    def to_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "detail": self.detail}


@dataclass(frozen=True)
class CapabilityHit:
    hit_id: str
    rule_id: str
    capability: str
    function_ref: str
    confidence: str
    owner_eligible: bool
    evidence: tuple[CapabilityEvidence, ...]
    source: str = "SIMPLE_RULE"

    def to_dict(self) -> dict:
        return {
            "hit_id": self.hit_id,
            "rule_id": self.rule_id,
            "capability": self.capability,
            "function_ref": self.function_ref,
            "confidence": self.confidence,
            "owner_eligible": self.owner_eligible,
            "source": self.source,
            "evidence": [item.to_dict() for item in self.evidence],
        }


class _Facts:
    def __init__(self, functions: Iterable[Function]) -> None:
        rows = tuple(functions)
        self.functions = {fn.ref: fn for fn in rows}
        self.succ = {fn.ref: tuple(ref for ref in fn.callees if ref in self.functions) for fn in rows}
        self.direct_names: dict[str, set[str]] = {}
        self.strings: dict[str, set[str]] = {}
        for fn in rows:
            names = set(fn.api_refs) | set(fn.dangerous_apis)
            names.update(self.functions[ref].name for ref in self.succ[fn.ref])
            names.update(_CALL_RE.findall(fn.code))
            self.direct_names[fn.ref] = {_api_key(name) for name in names if name}
            values = set(fn.string_refs)
            values.update(_QUOTED_RE.findall(fn.code))
            self.strings[fn.ref] = {value for value in values if value}

    def names_near(self, start: str, depth: int) -> set[str]:
        names: set[str] = set()
        for ref in self.reachable(start, depth):
            names.update(self.direct_names.get(ref, ()))
        return names

    def reachable(self, start: str, depth: int) -> dict[str, int]:
        distance = {start: 0}
        queue = deque([start])
        while queue:
            current = queue.popleft()
            if distance[current] >= depth:
                continue
            for target in self.succ.get(current, ()):
                if target not in distance:
                    distance[target] = distance[current] + 1
                    queue.append(target)
        return distance


def detect_capabilities(functions: list[Function], *, enabled: bool = True) -> list[CapabilityHit]:
    """Return deterministic, localized behavior hints without changing decisions."""

    for fn in functions:
        fn.capability_hits.clear()
    if not enabled:
        return []

    facts = _Facts(functions)
    pending: list[tuple[Function, str, str, str, bool, str, str]] = []

    def emit(
        fn: Function,
        rule_id: str,
        capability: str,
        confidence: str,
        kind: str,
        detail: str,
        *,
        owner_eligible: bool | None = None,
    ) -> None:
        pending.append(
            (
                fn,
                rule_id,
                capability,
                confidence,
                confidence == HIGH if owner_eligible is None else owner_eligible,
                kind,
                detail,
            )
        )

    for fn in functions:
        ref = fn.ref
        direct = facts.direct_names[ref]
        near2 = facts.names_near(ref, 2)
        near3 = facts.names_near(ref, 3)
        code = fn.code or ""
        strings = facts.strings[ref]
        joined = "\n".join(sorted(strings)) + "\n" + code
        internal_children = facts.succ.get(ref, ())

        resource = _present(direct, "FindResource", "LoadResource", "LockResource", "SizeofResource")
        if len(resource) >= 3:
            emit(fn, "simple.configuration.resource", "configuration", MEDIUM,
                 "api-combination", "资源读取组合: %s" % ", ".join(sorted(resource)))
        global_writes = set(_GLOBAL_ASSIGN_RE.findall(code))
        if len(global_writes) >= 12:
            emit(fn, "simple.configuration.global-propagation", "configuration", HIGH,
                 "global-propagation", "%d 个不同全局赋值" % len(global_writes))
        fixed_counts = [int(value) for value in _FIXED_LOOP_RE.findall(code) if int(value) > 1]
        if fixed_counts and re.search(r"\b(?:byte|word|dword|qword|off|unk)_[0-9A-Fa-f]+\b", code):
            emit(fn, "simple.configuration.fixed-copy", "configuration", MEDIUM,
                 "fixed-global-copy", "固定 %d 项的全局数据传播" % min(fixed_counts),
                 owner_eligible=False)

        mutexes = {value for value in strings if _MUTEX_RE.search(value)}
        if len(mutexes) >= 3 and _has_key(direct, "CreateMutex"):
            emit(fn, "simple.mode-dispatch.mutex-set", "mode-dispatch", HIGH,
                 "fixed-mode-objects", "%d 个固定模式互斥体" % len(mutexes))

        anti_markers = set(_ANTI_STRING_RE.findall(joined))
        anti_apis = _present(
            direct, "IsDebuggerPresent", "CheckRemoteDebuggerPresent",
            "NtQueryInformationProcess", "GetTickCount", "QueryPerformanceCounter", "ExitProcess",
        )
        if (
            _has_key(direct, "GetTickCount")
            and _has_key(direct, "ExitProcess")
            and len(internal_children) >= 3
        ) or len(anti_markers) >= 2 or len(anti_apis) >= 3:
            emit(fn, "simple.anti-analysis.cluster", "anti-analysis", HIGH,
                 "anti-analysis-cluster", "多检查分支及时间/终止证据")

        direct_reg = _present(direct, "RegCreateKey", "RegCreateKeyEx", "RegSetValue", "RegSetValueEx")
        near_reg = _present(near2, "RegCreateKey", "RegCreateKeyEx", "RegSetValue", "RegSetValueEx")
        direct_create = bool({"regcreatekey", "regcreatekeyex"} & direct_reg)
        direct_set = bool({"regsetvalue", "regsetvalueex"} & direct_reg)
        near_create = bool({"regcreatekey", "regcreatekeyex"} & near_reg)
        near_set = bool({"regsetvalue", "regsetvalueex"} & near_reg)
        if (direct_create and direct_set and _AUTORUN_RE.search(joined)) or (
            _has_key(direct, "CopyFile") and near_create and near_set
        ):
            emit(fn, "simple.persistence.file-registry", "persistence", HIGH,
                 "file-registry-chain", "复制/自启动注册表写入组合")

        if _has_key(direct, "PStoreCreateInstance"):
            emit(fn, "simple.credential.pstore", "credential-pstore", HIGH,
                 "credential-api", "PStoreCreateInstance")
        if _has_all(direct, "RasEnumEntries", "RasGetEntryDialParams") or _has_key(direct, "LsaRetrievePrivateData"):
            emit(fn, "simple.credential.ras-lsa", "credential-ras", HIGH,
                 "credential-api", "RAS/LSA 私密数据访问")
        if _has_all(direct, "CredEnumerate", "CryptUnprotectData"):
            emit(fn, "simple.credential.credman", "credential-credman", HIGH,
                 "credential-api", "CredEnumerate + DPAPI")
        if _has_key(direct, "RegEnumValue") and _has_key(direct, "CryptUnprotectData"):
            emit(fn, "simple.credential.registry-dpapi", "credential-storage2", HIGH,
                 "credential-api", "注册表枚举 + DPAPI")
        if re.search(r"nss3|NSSBase64|PK11|profiles\.ini|signons", joined, re.IGNORECASE):
            emit(fn, "simple.credential.firefox", "credential-firefox", HIGH,
                 "credential-artifact", "Firefox/NSS 静态标记")
        if re.search(r"Vitalwerks|No-?IP|\\DUC\b", joined, re.IGNORECASE):
            emit(fn, "simple.credential.noip", "credential-noip", HIGH,
                 "credential-artifact", "No-IP/Vitalwerks 静态标记")
        if re.search(r"Windows\s*Live|WindowsLive:name", joined, re.IGNORECASE):
            emit(fn, "simple.credential.windows-live", "credential-windows-live", HIGH,
                 "credential-artifact", "Windows Live 凭据标记")

        output_names = {value for value in strings if _OUTPUT_RE.search(value)}
        if len(output_names) >= 4 and (_has_key(near3, "WriteFile") or re.search(r"WriteFile\s*\(", code)):
            emit(fn, "simple.credential.fixed-output-set", "credential-output", HIGH,
                 "fixed-output-set", "%d 个固定结果文件名" % len(output_names))

        if _has_key(direct, "SetWindowsHookEx"):
            emit(fn, "simple.input-hook.install", "input-hook", HIGH,
                 "hook-installation", "安装 Windows 输入 Hook")

        injection = _present(
            direct, "VirtualAllocEx", "VirtualProtectEx", "WriteProcessMemory", "CreateRemoteThread"
        )
        if len(injection) >= 2 and bool({"writeprocessmemory", "createremotethread"} & injection):
            substantial = fn.size >= 128 or len([line for line in code.splitlines() if line.strip()]) >= 24
            confidence = HIGH if "createremotethread" in injection or substantial else MEDIUM
            emit(fn, "simple.injection.remote-memory", "process-injection", confidence,
                 "remote-memory-chain", "+".join(sorted(injection)))

        if _has_all(direct, "VirtualAlloc", "VirtualProtect") and (
            len(internal_children) >= 2 or _PE_RE.search(code)
        ):
            emit(fn, "simple.load-code.local-pe-map", "pe-mapping", HIGH,
                 "local-image-map", "本地映像分配/保护及映射分支")

        if _has_all(direct, "DeleteFile", "Sleep") and (
            (fn.loop_count or 0) > 0 or re.search(r"\b(?:while|for|do)\b", code)
        ):
            emit(fn, "simple.self-delete.retry-loop", "self-delete", HIGH,
                 "delete-retry-loop", "DeleteFile 失败后 Sleep 重试")

        network = _present(
            direct, "connect", "send", "recv", "WSAConnect", "InternetOpen",
            "InternetConnect", "HttpSendRequest", "WinHttpOpen", "WinHttpConnect",
            "WinHttpSendRequest", "URLDownloadToFile",
        )
        if len(network) >= 2:
            emit(fn, "simple.communication.api-chain", "communication", HIGH,
                 "network-api-chain", "网络 API 组合: %s" % ", ".join(sorted(network)))

    # Attribute an opaque collector only when its caller pairs the produced value
    # with a high-specificity Firefox artifact in the same bounded source window.
    by_ref = {fn.ref: fn for fn in functions}
    for caller in functions:
        for target_ref in caller.callees:
            target = by_ref.get(target_ref)
            if target and _caller_pairs_artifact(
                caller.code,
                target.name,
                r"FIREFOX\.(?:abc|dat)|nss3|NSSBase64|PK11|profiles\.ini|signons",
            ):
                emit(target, "simple.credential.firefox-caller-association", "credential-firefox", MEDIUM,
                     "caller-artifact-association", "调用者将该函数关联到 Firefox/NSS 产物",
                     owner_eligible=False)

    hits: list[CapabilityHit] = []
    for index, (
        fn,
        rule_id,
        capability,
        confidence,
        owner_eligible,
        kind,
        detail,
    ) in enumerate(pending, 1):
        hit = CapabilityHit(
            hit_id="CH-%04d" % index,
            rule_id=rule_id,
            capability=capability,
            function_ref=fn.ref,
            confidence=confidence,
            owner_eligible=owner_eligible,
            evidence=(CapabilityEvidence(kind=kind, detail=detail),),
        )
        hits.append(hit)
        fn.capability_hits.append(hit)
    return hits


def _api_key(value: str) -> str:
    name = str(value).rsplit("!", 1)[-1]
    name = re.sub(r"^(?:__imp_|j_)", "", name, flags=re.IGNORECASE)
    name = re.sub(r"(?:_w|@\d+)$", "", name, flags=re.IGNORECASE)
    key = re.sub(r"[^A-Za-z0-9]", "", name).lower()
    return re.sub(r"0+$", "", key)


def _has_key(names: set[str], expected: str) -> bool:
    key = _api_key(expected)
    return any(name in {key, key + "a", key + "w"} for name in names)


def _has_all(names: set[str], *expected: str) -> bool:
    return all(_has_key(names, item) for item in expected)


def _present(names: set[str], *expected: str) -> set[str]:
    return {_api_key(item) for item in expected if _has_key(names, item)}


def _caller_pairs_artifact(
    code: str,
    target_name: str,
    artifact_pattern: str,
    *,
    window_chars: int = 700,
) -> bool:
    if not code or not target_name:
        return False
    escaped = re.escape(target_name)
    matches = sorted(
        [
            *re.finditer(r"\b%s\s*\(\s*&\s*([A-Za-z_]\w*)" % escaped, code),
            *re.finditer(r"\b([A-Za-z_]\w*)\s*=\s*%s\s*\(" % escaped, code),
        ],
        key=lambda item: item.start(),
    )
    artifact = re.compile(artifact_pattern, re.IGNORECASE)
    for index, match in enumerate(matches):
        next_call = matches[index + 1].start() if index + 1 < len(matches) else len(code)
        artifact_match = artifact.search(
            code, match.end(), min(len(code), next_call, match.end() + window_chars)
        )
        if artifact_match is None:
            continue
        between = code[match.end():artifact_match.start()]
        if re.search(r"(?<![&A-Za-z0-9_])%s\b" % re.escape(match.group(1)), between):
            return True
    return False


__all__ = [
    "CapabilityEvidence",
    "CapabilityHit",
    "HIGH",
    "MEDIUM",
    "detect_capabilities",
]
