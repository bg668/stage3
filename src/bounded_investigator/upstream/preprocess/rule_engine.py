"""Rule evaluation engine. It knows rule ordering, not preprocessing orchestration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .model import Function, Verdict, UNDECIDED
from .rules import REGISTRY, RuleContext


@dataclass(frozen=True)
class Evaluation:
    decision: str
    rule_id: Optional[str]
    verdict: Optional[Verdict]


class RuleEngine:
    def __init__(self, context: RuleContext):
        self.context = context

    def evaluate(
        self,
        fn: Function,
        *,
        skip_rule_id: Optional[str] = None,
    ) -> Evaluation:
        for rule in REGISTRY:
            rule_id = rule.RULE_ID
            if getattr(rule, "DIAGNOSTIC_ONLY", False):
                continue
            if rule_id == skip_rule_id:
                continue
            verdict = rule.evaluate(fn, self.context)
            if verdict is not None:
                return Evaluation(verdict.decision, rule_id, verdict)
        return Evaluation(UNDECIDED, None, None)

    def apply(self, functions: list[Function]) -> dict[str, int]:
        hits = {rule.RULE_ID: 0 for rule in REGISTRY}
        for fn in functions:
            result = self.evaluate(fn)
            fn.decision = result.decision
            if result.verdict is None or result.rule_id is None:
                fn.decision_reason = None
                fn.decision_detail = ""
                continue
            fn.decision_reason = result.rule_id
            fn.decision_detail = result.verdict.detail
            hits[result.rule_id] += 1
        return hits

    def diagnostic_rule_stats(self, functions: list[Function]) -> dict[str, dict]:
        diagnostics: dict[str, dict] = {}
        for rule in REGISTRY:
            if not getattr(rule, "DIAGNOSTIC_ONLY", False):
                continue
            would_hit, would_flip, samples = 0, 0, []
            for fn in functions:
                verdict = rule.evaluate(fn, self.context)
                if verdict is None:
                    continue
                would_hit += 1
                if fn.decision != verdict.decision:
                    would_flip += 1
                    if len(samples) < 5:
                        samples.append("%s %s (%s)" % (fn.ref, fn.name, verdict.detail))
            diagnostics[rule.RULE_ID] = {
                "would_hit": would_hit,
                "would_flip": would_flip,
                "samples": samples,
            }
        return diagnostics

    def marginal_contributions(self, functions: list[Function]) -> dict[str, int]:
        marginal: dict[str, int] = {}
        for rule in REGISTRY:
            rule_id = rule.RULE_ID
            if getattr(rule, "DIAGNOSTIC_ONLY", False):
                marginal[rule_id] = 0
                continue
            lost = 0
            for fn in functions:
                if fn.decision == UNDECIDED:
                    continue
                if self.evaluate(fn, skip_rule_id=rule_id).decision == UNDECIDED:
                    lost += 1
            marginal[rule_id] = lost
        return marginal
