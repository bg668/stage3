from __future__ import annotations

"""Cheap guard against revisions caused solely by already-known upstream facts.

Reviewer remains responsible for semantic assessment. A missing-claim/artifact
issue can trigger Reviser only if the Reviewer supplied LOCAL source evidence.
Other kinds, such as wrong_claim/evidence_mismatch, are unchanged.
"""

from pathlib import Path

from .material_inventory import _parse_code_sections, normal_func_name, _FUNCTION_TARGET
from .schemas import CoverageReview


def constrain_reviewer_scope(review: CoverageReview, packet_text: str) -> CoverageReview:
    local: set[str] = set()
    for section in _parse_code_sections(Path("current_packet.md"), packet_text):
        local.add(section.function_id)
        local.add(section.symbol.lower())

    if not local:
        # Other clients can analyze plain code without our Jxx section format;
        # do not demote valid findings when there is no index to check against.
        return review

    changed = False
    modified = []
    for issue in review.issues:
        # Only omitted items require packet-local positive evidence; other
        # semantic errors are not automatically demoted.
        should_check = (
            issue.scope == "packet_internal"
            and issue.severity in {"major", "critical"}
            and issue.kind in {"omitted_claim", "omitted_artifact"}
        )
        if not should_check:
            modified.append(issue)
            continue
        local_supported = any(
            (normal_func_name(match.group("identifier")) or "") in local
            for ref in issue.evidence
            for match in _FUNCTION_TARGET.finditer(ref.location)
        )
        if not local_supported:
            changed = True
            modified.append(issue.model_copy(update={
                "severity": "minor",
                "scope": "outside_packet",
                "description": (
                    issue.description +
                    " [Scope guard: no packet-local code evidence; upstream-only repetition does not require revision]"
                ),
            }))
        else:
            modified.append(issue)
    if not changed:
        return review
    # CoverageReview's validator requires the flag to track remaining major
    # internal issues. Revalidate rather than bypassing the model validator.
    return CoverageReview.model_validate({
        "requires_revision": any(i.scope == "packet_internal" and i.severity in {"major", "critical"} for i in modified),
        "issues": [i.model_dump() for i in modified],
        "review_summary": review.review_summary,
    })
