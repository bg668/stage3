"""Core data models for deterministic preprocessing."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from .capability_detection import CapabilityHit

PROGRAM_ANALYZE = "PROGRAM_ANALYZE"
PROGRAM_SKIP = "PROGRAM_SKIP"
UNDECIDED = "UNDECIDED"
ALL_DECISIONS = (PROGRAM_ANALYZE, PROGRAM_SKIP, UNDECIDED)

ANALYSIS_PRIMARY = "PRIMARY"
ANALYSIS_SUPPORT_FORWARDER = "SUPPORT_FORWARDER"
ALL_ANALYSIS_ROLES = (ANALYSIS_PRIMARY, ANALYSIS_SUPPORT_FORWARDER)


@dataclass(frozen=True)
class Verdict:
    """A rule decision. ``None`` means the rule does not apply."""

    decision: str
    detail: str = ""

    def __post_init__(self) -> None:
        if self.decision not in (PROGRAM_ANALYZE, PROGRAM_SKIP):
            raise ValueError(
                "规则只能产出 PROGRAM_ANALYZE 或 PROGRAM_SKIP,收到 %r" % self.decision
            )


@dataclass
class Function:
    """Normalized function facts plus deterministic preprocessing state."""

    # Stable source facts.
    ref: str
    source_id: str
    address: int
    name: str
    size: int
    flags: Optional[int]
    code: str
    assembly: str = ""

    # Optional DeepExtract facts. ``None`` means the source did not provide them.
    is_entry: Optional[bool] = None
    is_export: Optional[bool] = None
    export_name: Optional[str] = None
    decompile_status: Optional[str] = None
    has_indirect_call: Optional[bool] = None
    basic_block_count: Optional[int] = None
    has_user_name: Optional[bool] = None

    # Export-side fields used only for cross checks, never as rule inputs.
    analysis_candidate: Optional[bool] = None
    export_skip_reason: Optional[str] = None

    # Derived relationship/features.
    callers: list[str] = field(default_factory=list)
    callees: list[str] = field(default_factory=list)
    referenced: list[str] = field(default_factory=list)
    api_refs: list[str] = field(default_factory=list)
    dangerous_apis: list[str] = field(default_factory=list)
    string_refs: list[str] = field(default_factory=list)
    loop_count: Optional[int] = None

    # Additive navigation facts. They never change the Stage-1 decision directly.
    capability_hits: list["CapabilityHit"] = field(default_factory=list)

    # Decision state populated by pipeline.
    decision: str = UNDECIDED
    decision_reason: Optional[str] = None
    decision_detail: str = ""
    analysis_role: Optional[str] = ANALYSIS_PRIMARY

    FUNC_LIB = 0x04
    FUNC_THUNK = 0x80

    @property
    def hex_address(self) -> str:
        return "0x%X" % self.address

    @property
    def is_thunk(self) -> Optional[bool]:
        return None if self.flags is None else bool(self.flags & self.FUNC_THUNK)

    @property
    def is_library(self) -> Optional[bool]:
        return None if self.flags is None else bool(self.flags & self.FUNC_LIB)

    @property
    def in_degree(self) -> int:
        return len(self.callers)

    @property
    def out_degree(self) -> int:
        return len(self.callees)

    @property
    def is_leaf(self) -> bool:
        return not self.callees

    @property
    def has_code(self) -> bool:
        return bool(self.code.strip())

    @property
    def code_lines(self) -> list[str]:
        if not self.code:
            return []
        return self.code.replace("\r\n", "\n").replace("\r", "\n").split("\n")

    @property
    def is_stub(self) -> bool:
        """Whether an unreachable function has no substantive behavior."""
        if self.decision_reason in ("r001_thunk", "r003_jump_stub", "r004_nullsub"):
            return True
        if self.dangerous_apis or self.string_refs or self.api_refs or self.callees:
            return False
        if not self.has_code:
            return True
        body = [
            line.strip()
            for line in self.code_lines
            if line.strip() and not line.strip().startswith("//")
        ]
        meaningful = [line for line in body if line not in ("{", "}")]
        return len(meaningful) <= 2 and any(
            line.startswith("return") for line in meaningful
        )


@dataclass
class SampleMeta:
    """Sample-level facts from ``analysis_metadata`` when available."""

    sample_name: Optional[str] = None
    sample_sha256: Optional[str] = None
    file_type: Optional[str] = None
    arch: Optional[str] = None
    bitness: Optional[int] = None
    ida_version: Optional[str] = None
    deepextract_version: Optional[str] = None
    flirt_signatures_loaded: Optional[list] = None
    decompiler_available: Optional[bool] = None
    total_functions: Optional[int] = None

    @property
    def flirt_match_total(self) -> Optional[int]:
        if not self.flirt_signatures_loaded:
            return None
        total, seen = 0, False
        for signature in self.flirt_signatures_loaded:
            if isinstance(signature, dict) and isinstance(signature.get("matches"), int):
                total += signature["matches"]
                seen = True
        return total if seen else None

    def flirt_report(self, func_lib_count: int) -> str:
        """Report FLIRT facts without guessing whether coverage is sufficient."""
        if self.flirt_signatures_loaded is None:
            return "未知(导出中无 flirt_signatures_loaded)"
        if not self.flirt_signatures_loaded:
            return "未应用任何签名 —— FUNC_LIB 计数不可据以判断是否存在库代码"

        parts = []
        for signature in self.flirt_signatures_loaded:
            if isinstance(signature, dict):
                parts.append(
                    "%s[%s]=%s"
                    % (
                        signature.get("name", "?"),
                        signature.get("state", "?"),
                        signature.get("matches", "?"),
                    )
                )
            else:
                parts.append(str(signature))
        text = "已应用 %d 个签名:%s" % (len(self.flirt_signatures_loaded), ", ".join(parts))
        if func_lib_count == 0:
            text += "  |  FUNC_LIB 命中 0"
            if self.flirt_match_total == 0:
                text += ",且全部签名匹配为 0 —— 需人工确认签名是否覆盖本样本的运行库"
        return text
