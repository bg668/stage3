"""Decision pipeline: rules -> diagnostics -> reachability -> stable stats contract."""

from __future__ import annotations

from typing import Optional

from .metrics import compute_skip_reachability
from .model import Function, SampleMeta, PROGRAM_ANALYZE, PROGRAM_SKIP, UNDECIDED
from .rule_engine import RuleEngine
from .rules import REGISTRY, RuleContext


def run(
    functions: list[Function],
    meta: SampleMeta,
    ctx: Optional[RuleContext] = None,
    vtable_groups: Optional[dict] = None,
) -> dict:
    """Apply deterministic rules in place and return the existing stats contract."""
    del meta  # Kept in the public signature for compatibility; no rule guesses from metadata.
    context = ctx or RuleContext()
    engine = RuleEngine(context)

    hits = engine.apply(functions)
    diagnostics_rules = engine.diagnostic_rule_stats(functions)
    marginal = engine.marginal_contributions(functions)
    reachability = compute_skip_reachability(functions, vtable_groups)

    counts = {PROGRAM_ANALYZE: 0, PROGRAM_SKIP: 0, UNDECIDED: 0}
    for fn in functions:
        counts[fn.decision] += 1
    scope = len(functions)
    program_total = counts[PROGRAM_ANALYZE] + counts[PROGRAM_SKIP]

    return {
        "scope": scope,
        "program_analyze": counts[PROGRAM_ANALYZE],
        "program_skip": counts[PROGRAM_SKIP],
        "undecided": counts[UNDECIDED],
        "program_decision_ratio": round(program_total / scope, 4) if scope else 0.0,
        "diagnostic_rules": diagnostics_rules,
        "rules": [
            {
                "id": rule.RULE_ID,
                "diagnostic_only": getattr(rule, "DIAGNOSTIC_ONLY", False),
                "decision": rule.DECISION,
                "description": rule.DESCRIPTION,
                "justification": getattr(rule, "JUSTIFICATION", "EFFICIENCY"),
                "justification_note": getattr(rule, "JUSTIFICATION_NOTE", ""),
                "hits": hits[rule.RULE_ID],
                "marginal": marginal.get(rule.RULE_ID, 0),
            }
            for rule in REGISTRY
        ],
        "functions_without_flags": sum(1 for fn in functions if fn.flags is None),
        "skip_reachability": reachability,
    }


__all__ = ["run", "compute_skip_reachability"]
