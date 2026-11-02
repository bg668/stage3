"""Deterministic rule registry. Order is semantic priority, evaluated first-match wins."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol

from ..model import Function, Verdict
from . import r001_thunk
from . import r002_library
from . import r003_jump_stub
from . import r004_nullsub
from . import r005_empty_code
from . import r006_trivial_leaf
from . import r100_entry
from . import r101_export
from . import r102_dangerous_api
from . import r103_decompile_failed


@dataclass
class RuleContext:
    """Only tunable facts actually consumed by rules belong here."""

    trivial_max_size: int = 64
    trivial_max_code_chars: int = 400


class Rule(Protocol):
    RULE_ID: str
    DECISION: str
    DESCRIPTION: str

    @staticmethod
    def evaluate(fn: Function, ctx: RuleContext) -> Optional[Verdict]: ...


REGISTRY: tuple = (
    r100_entry,
    r101_export,
    r103_decompile_failed,
    r001_thunk,
    r002_library,
    r003_jump_stub,
    r004_nullsub,
    r005_empty_code,
    # Dangerous API must outrank the heuristic trivial-leaf skip, but it must not
    # override deterministic library/stub classifications above.
    r102_dangerous_api,
    r006_trivial_leaf,
)


def all_rule_ids() -> list[str]:
    return [rule.RULE_ID for rule in REGISTRY]
