from __future__ import annotations

import re
from importlib.resources import files

from .harness import ResolvedHarness, load_harness
from .investigation.revision import render_revision_scope
from .schemas import CoverageReview, InvestigationResult

_PROMPT_PACKAGE = "bounded_investigator"
_TOKEN_RE = re.compile(r"{{\s*([a-zA-Z0-9_]+)\s*}}")


def load_prompt(name: str) -> str:
    return (
        files(_PROMPT_PACKAGE)
        .joinpath("prompts", name)
        .read_text(encoding="utf-8")
        .strip()
    )


def render_prompt(name: str, **values: str) -> str:
    template = load_prompt(name)

    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in values:
            raise KeyError(f"Missing prompt template value: {key}")
        return values[key]

    # Validate only placeholders originating from the template. Runtime material/raw
    # output may legitimately contain strings that look like {{placeholders}}.
    return _TOKEN_RE.sub(replace, template)


def analyzer_instructions(harness: ResolvedHarness | None = None) -> str:
    return (harness or load_harness()).instructions('analyzer')


def reviewer_instructions(harness: ResolvedHarness | None = None) -> str:
    return (harness or load_harness()).instructions('reviewer')


def reviser_instructions(harness: ResolvedHarness | None = None) -> str:
    return (harness or load_harness()).instructions('reviser')


def format_repair_instructions() -> str:
    return load_prompt("format_repair.instructions.md")


def global_reconciliation_instructions() -> str:
    return load_prompt("global_reconciliation.instructions.md")


def _ev_lines(evidence) -> list[str]:
    return [f"    - [{i}] {e.location}: {e.observation}" for i, e in enumerate(evidence)]


def render_result_context(result: InvestigationResult) -> str:
    """Compact LLM-readable Markdown; JSON remains only the machine persistence format."""
    lines = [
        f"status: {result.status.value}",
        f"summary: {result.summary}",
        "revision_paths: zero-based indices in this original result; next append slots: " + "; ".join(
            f"/{name}/{len(getattr(result, name))}"
            for name in ("claims", "artifacts", "gaps", "handoffs", "material_needs")
        ),
        "claims:",
    ]
    if not result.claims:
        lines.append("  - none")
    for i, c in enumerate(result.claims, 1):
        lines.extend([
            f"  {i}. [path=/claims/{i - 1}] {c.behavior} [confidence={c.confidence.value}, evidence_level={c.evidence_level.value}]",
            f"     condition: {c.condition}",
            f"     source: {c.source}",
            f"     action: {c.action}",
            f"     sink: {c.sink}",
            "     evidence:",
            *_ev_lines(c.evidence),
        ])
        if c.limitations:
            lines.append(f"     limitations: {'; '.join(c.limitations)}")

    lines.append("artifacts:")
    if not result.artifacts:
        lines.append("  - none")
    for i, a in enumerate(result.artifacts):
        lines.extend([
            f"  - [path=/artifacts/{i}, role={a.role}, detection_specificity={a.detection_specificity.value}, confidence={a.confidence.value}] {a.kind}: {a.value}",
            f"    meaning: {a.meaning}",
            "    evidence:",
            *_ev_lines(a.evidence),
        ])

    lines.append("gaps:")
    if not result.gaps:
        lines.append("  - none")
    for i, g in enumerate(result.gaps):
        lines.append(f"  - [path=/gaps/{i}, gap_type={g.gap_type}, scope={g.scope}, impact={g.impact}] {g.question} — {g.reason}")

    lines.append("handoffs:")
    if not result.handoffs:
        lines.append("  - none")
    for i, h in enumerate(result.handoffs):
        lines.append(
            f"  - [path=/handoffs/{i}, priority={h.priority}] {h.target}: {h.reason}; expected={h.expected_value}"
        )
    lines.append("material_needs:")
    if not result.material_needs:
        lines.append("  - none")
    for i, need in enumerate(result.material_needs):
        lines.extend([
            f"  - [path=/material_needs/{i}] kind: {need.kind}",
            f"    target: {need.target}",
            "    targets:",
        ])
        if not need.targets:
            lines.append("      - none")
        for target in need.targets:
            lines.extend([
                f"      - identifier: {target.identifier}",
                f"        start_line: {target.start_line if target.start_line is not None else 'null'}",
                f"        end_line: {target.end_line if target.end_line is not None else 'null'}",
            ])
        lines.extend([
            f"    reason: {need.reason}",
            f"    impact: {need.impact}",
            "    evidence:",
            *_ev_lines(need.evidence),
        ])
    return "\n".join(lines)


def render_review_context(review: CoverageReview) -> str:
    lines = [
        f"requires_revision: {str(review.requires_revision).lower()}",
        f"summary: {review.review_summary}",
        "issues:",
    ]
    if not review.issues:
        lines.append("  - none")
    for issue in review.issues:
        lines.append(
            f"  - {issue.issue_id} [{issue.scope}/{issue.severity}/{issue.kind}] {issue.description}"
        )
        if issue.evidence:
            lines.extend(_ev_lines(issue.evidence))
        lines.append(f"    fix: {issue.required_fix}")
        lines.append("    fixes:")
        if not issue.fixes:
            lines.append("      - none")
        for fix in issue.fixes:
            lines.append(f"      - {fix.type}: {', '.join(fix.targets)}")
    return "\n".join(lines)


def analyzer_prompt(packet_name: str, packet_text: str, dependency_context: str = "none") -> str:
    return render_prompt(
        "analyzer.input.md",
        packet_name=packet_name,
        packet_text=packet_text,
        dependency_context=dependency_context,
    )


def reviewer_prompt(
    packet_name: str,
    packet_text: str,
    draft: InvestigationResult,
    dependency_context: str = "none",
    evidence_validation_context: str = "none",
) -> str:
    return render_prompt(
        "reviewer.input.md",
        packet_name=packet_name,
        packet_text=packet_text,
        dependency_context=dependency_context,
        draft_context=render_result_context(draft),
        evidence_validation_context=evidence_validation_context,
    )


def reviser_prompt(
    packet_name: str,
    packet_text: str,
    draft: InvestigationResult,
    review: CoverageReview,
    dependency_context: str = "none",
    evidence_validation_context: str = "none",
) -> str:
    return render_prompt(
        "reviser.input.md",
        packet_name=packet_name,
        packet_text=packet_text,
        dependency_context=dependency_context,
        draft_context=render_result_context(draft),
        review_context=f"{render_review_context(review)}\n\n{render_revision_scope(draft, review)}",
        evidence_validation_context=evidence_validation_context,
    )


def format_repair_prompt(
    *,
    target_stage: str,
    attempt: int,
    target_schema: str,
    validation_error: str,
    original_raw_output: str,
    current_candidate: str,
) -> str:
    return render_prompt(
        "format_repair.input.md",
        target_stage=target_stage,
        attempt=str(attempt),
        target_schema=target_schema,
        validation_error=validation_error,
        original_raw_output=original_raw_output,
        current_candidate=current_candidate,
    )


def global_reconciliation_prompt(packet_results: str) -> str:
    return render_prompt(
        "global_reconciliation.input.md",
        packet_results=packet_results,
    )
