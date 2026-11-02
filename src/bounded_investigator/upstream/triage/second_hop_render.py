"""Stable plain-text projection for V4 second-hop packages."""

from __future__ import annotations

from pathlib import Path

from .second_hop_domain import FactSupplementSpec, RelatedFunctionMaterial, SecondHopPackage


def write_second_hop_packages(directory: Path, packages: list[SecondHopPackage]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for package in packages:
        (directory / (package.subject_ref + ".txt")).write_text(
            render_second_hop_package(package), encoding="utf-8"
        )


def render_second_hop_package(package: SecondHopPackage) -> str:
    lines = [
        "# second-hop package v4",
        "# subject: %s" % package.subject_ref,
        "# reason: %s" % package.reason,
        "# status: %s" % package.status,
        "# supplement_truncated: %s" % str(package.truncated).lower(),
        "# supplement_code_chars: %d" % package.supplement_code_chars,
        "# omitted_related_refs: %s"
        % (", ".join(package.omitted_related_refs) if package.omitted_related_refs else "none"),
        "# missing_sections: %s"
        % (", ".join(package.missing_sections) if package.missing_sections else "none"),
    ]
    if package.error:
        lines.append("# error: %s" % _one_line(package.error))
    lines.extend(
        [
            "",
            "[SUBJECT]",
            "ref: %s" % package.subject_ref,
            "name: %s" % package.subject_name,
            "address: %s" % package.subject_address,
            "source: code/%s.txt" % package.subject_ref,
            "code_complete: %s" % str(package.status != "FAILED").lower(),
            "code:",
            "----- BEGIN SUBJECT CODE -----",
            package.subject_code.rstrip(),
            "----- END SUBJECT CODE -----",
        ]
    )

    for fact in package.facts:
        lines.extend(["", *_render_fact(fact)])
    for material in package.related_functions:
        lines.extend(["", *_render_related(material)])

    if package.omitted_related_refs:
        lines.extend(
            [
                "",
                "[SUPPLEMENT]",
                "kind: budget",
                "source: second_hop_budget",
                "relation: related_function_limit",
                "status: OMITTED_BY_BUDGET",
                "refs: %s" % ", ".join(package.omitted_related_refs),
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def _render_fact(fact: FactSupplementSpec) -> list[str]:
    lines = [
        "[SUPPLEMENT]",
        "kind: %s" % fact.kind,
        "source: %s" % fact.source,
        "relation: %s" % fact.relation,
        "status: %s" % fact.status,
    ]
    lines.extend("%s: %s" % (key, _one_line(value)) for key, value in fact.fields)
    if fact.detail:
        lines.append("detail: %s" % _one_line(fact.detail))
    return lines


def _render_related(item: RelatedFunctionMaterial) -> list[str]:
    lines = [
        "[SUPPLEMENT]",
        "kind: related_function",
        "ref: %s" % item.ref,
        "name: %s" % item.name,
        "address: %s" % item.address,
        "status: FOUND",
    ]
    for index, trace in enumerate(item.traces, 1):
        lines.extend(
            [
                "edge_%d_source: %s" % (index, trace.source),
                "edge_%d_relation: %s" % (index, trace.relation),
                "edge_%d_detail: %s" % (index, _one_line(trace.detail)),
            ]
        )
    lines.extend(
        [
            "code_source: code/%s.txt" % item.ref,
            "code_status: %s" % item.code_status,
            "code_truncated: %s" % str(item.code_truncated).lower(),
            "original_code_chars: %d" % item.original_code_chars,
        ]
    )
    if item.code:
        lines.extend(
            [
                "code:",
                "----- BEGIN SUPPLEMENT CODE -----",
                item.code.rstrip(),
                "----- END SUPPLEMENT CODE -----",
            ]
        )
    return lines


def _one_line(value: object) -> str:
    return " ".join(str(value).replace("\r", " ").replace("\n", " ").split())
