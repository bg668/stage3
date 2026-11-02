"""Deterministic validation of provider outputs."""

from __future__ import annotations

from collections import Counter

from .domain import FunctionVerdict, reason_matches_decision


class VerdictIdMismatch(ValueError):
    pass


class VerdictSemanticMismatch(ValueError):
    pass


def validate_and_order_verdicts(
    subject_refs: list[str], verdicts: list[FunctionVerdict]
) -> list[FunctionVerdict]:
    return _validate_and_order(subject_refs, verdicts, round2=False)


def validate_round2_and_order_verdicts(
    subject_refs: list[str], verdicts: list[FunctionVerdict]
) -> list[FunctionVerdict]:
    return _validate_and_order(subject_refs, verdicts, round2=True)


def _validate_and_order(
    subject_refs: list[str], verdicts: list[FunctionVerdict], *, round2: bool
) -> list[FunctionVerdict]:
    counts = Counter(verdict.ref for verdict in verdicts)
    expected = set(subject_refs)
    actual = set(counts)
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    duplicate = sorted(ref for ref, count in counts.items() if count != 1)
    if missing or extra or duplicate:
        raise VerdictIdMismatch(
            "missing=%s extra=%s duplicate=%s" % (missing, extra, duplicate)
        )

    if round2:
        uncertain = [v.ref for v in verdicts if v.decision == "UNCERTAIN"]
        if uncertain:
            raise VerdictSemanticMismatch("Round2 禁止 UNCERTAIN: %s" % uncertain)

    bad = [
        "%s:%s/%s" % (verdict.ref, verdict.decision, verdict.reason)
        for verdict in verdicts
        if not reason_matches_decision(verdict.decision, verdict.reason)
    ]
    if bad:
        raise VerdictSemanticMismatch("非法 decision/reason: %s" % ", ".join(bad))

    by_ref = {verdict.ref: verdict for verdict in verdicts}
    return [by_ref[ref] for ref in subject_refs]
