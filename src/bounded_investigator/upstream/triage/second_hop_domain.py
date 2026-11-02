"""Internal deterministic domain objects for V4 second-hop material assembly."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

AssemblyStatus = Literal["COMPLETE", "PARTIAL", "FAILED"]
FactStatus = Literal["FOUND", "NOT_FOUND", "OMITTED_BY_RECIPE"]
CodeStatus = Literal["OK", "EMPTY", "NOT_FOUND", "READ_ERROR", "OMITTED_BY_BUDGET"]


@dataclass(frozen=True)
class RelationTrace:
    source: str
    relation: str
    detail: str


@dataclass(frozen=True)
class FactSupplementSpec:
    kind: str
    source: str
    relation: str
    status: FactStatus
    fields: tuple[tuple[str, str], ...] = ()
    detail: str = ""


@dataclass(frozen=True)
class RelatedFunctionSpec:
    ref: str
    priority: int
    traces: tuple[RelationTrace, ...]


@dataclass(frozen=True)
class RecipePlan:
    facts: tuple[FactSupplementSpec, ...] = ()
    related_functions: tuple[RelatedFunctionSpec, ...] = ()
    missing_sections: tuple[str, ...] = ()


@dataclass(frozen=True)
class RelatedFunctionMaterial:
    ref: str
    name: str
    address: str
    traces: tuple[RelationTrace, ...]
    code_status: CodeStatus
    code: str = ""
    code_truncated: bool = False
    original_code_chars: int = 0


@dataclass
class SecondHopPackage:
    subject_ref: str
    reason: str
    status: AssemblyStatus
    subject_name: str
    subject_address: str
    subject_code: str
    facts: list[FactSupplementSpec] = field(default_factory=list)
    related_functions: list[RelatedFunctionMaterial] = field(default_factory=list)
    missing_sections: list[str] = field(default_factory=list)
    truncated: bool = False
    omitted_related_refs: list[str] = field(default_factory=list)
    supplement_code_chars: int = 0
    error: str | None = None

    def public_dict(self, *, path: str) -> dict:
        return {
            "ref": self.subject_ref,
            "reason": self.reason,
            "status": self.status,
            "path": path,
            "supplement_count": len(self.facts) + len(self.related_functions),
            "related_refs": [item.ref for item in self.related_functions],
            "missing_sections": list(self.missing_sections),
            "truncated": self.truncated,
            "omitted_related_refs": list(self.omitted_related_refs),
            "supplement_code_chars": self.supplement_code_chars,
            "error": self.error,
        }
