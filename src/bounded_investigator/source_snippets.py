from __future__ import annotations

"""Deterministic, source-backed code excerpts for the human report.

Only fenced, explicitly numbered lines in supplied material packets are eligible.
No model is permitted to rewrite a snippet. Missing sources produce no excerpt.
"""

import re
from collections import defaultdict
from pathlib import Path
from typing import Any

_HEADER = re.compile(r"^##\s+(F[-_]?0*\d{1,5})\b[^\n]*", re.I | re.M)
_CODE_LINE = re.compile(r"^L(?P<line>\d{1,6}):\s?(?P<code>.*)$", re.M)
_REF = re.compile(r"(?<![\w])F[-_]?0*(?P<fid>\d{1,5})\s*L(?P<start>\d{1,6})(?:\s*[-–—]\s*L(?P<end>\d{1,6}))?", re.I)
_FENCE = re.compile(r"^```(?:text|c|cpp|asm|python)?\s*$", re.I | re.M)


def normalize_function_id(text: str) -> str:
    number = re.search(r"\d+", text)
    if not number:
        raise ValueError(f"unrecognized function ID {text!r}")
    return f"F-{int(number.group()):04d}"


def build_code_index_from_text(text: str, source_name: str) -> dict[str, dict[int, tuple[str, str]]]:
    """Build a function/line index from one already supplied packet string."""
    index: dict[str, dict[int, tuple[str, str]]] = defaultdict(dict)
    matches = list(_HEADER.finditer(text))
    for i, header in enumerate(matches):
        section = text[header.end(): matches[i + 1].start() if i + 1 < len(matches) else len(text)]
        fences = list(_FENCE.finditer(section))
        if len(fences) < 2:
            continue
        body = section[fences[0].end():fences[1].start()]
        fid = normalize_function_id(header.group(1))
        for match in _CODE_LINE.finditer(body):
            line = int(match.group("line"))
            if line < 1:
                continue
            index[fid].setdefault(line, (match.group("code"), source_name))
    return dict(index)


def build_code_index(packet_paths: list[Path]) -> dict[str, dict[int, tuple[str, str]]]:
    """function -> original line -> (unaltered source text, source filename)."""
    index: dict[str, dict[int, tuple[str, str]]] = defaultdict(dict)
    for path in sorted(packet_paths):
        local = build_code_index_from_text(path.read_text(encoding="utf-8"), path.name)
        for fid, lines in local.items():
            for line, value in lines.items():
                # Do not silently overwrite disagreeing originals.
                index[fid].setdefault(line, value)
    return dict(index)


def parse_evidence_location(location: str) -> list[tuple[str, int, int]]:
    """Parse every F-xxxx Lx[-Ly] segment from a model-produced location string."""
    parsed: list[tuple[str, int, int]] = []
    for match in _REF.finditer(location):
        fid = normalize_function_id(match.group("fid"))
        start = int(match.group("start"))
        end = int(match.group("end") or start)
        parsed.append((fid, start, end))
    return parsed


def _pick_window(
    lines: dict[int, tuple[str, str]],
    start: int,
    end: int,
    hints: str,
    max_lines: int,
) -> list[int]:
    available = sorted(n for n in lines if start <= n <= end)
    if not available:
        return []
    if len(available) <= max_lines:
        return available
    # Find the best *real* line in the cited range, using names already present
    # in the claim; no sample-specific key strings are hard-coded.
    identifiers = set(re.findall(r"[A-Za-z_][A-Za-z_0-9]{4,}", hints.lower()))
    identifiers = {x for x in identifiers if x not in {"source", "sink", "location", "behavior"}}
    scores: list[tuple[int, int]] = []
    for n in available:
        code = lines[n][0].lower()
        matches = sum(1 for token in identifiers if token in code)
        # Prefer semantic operation lines over braces/local variable declarations.
        operation = 2 if "(" in code and not code.strip().startswith(("int ", "char ", "void ")) else 0
        scores.append((matches * 3 + operation, n))
    max_score = max(score for score, _ in scores)
    anchor = min(n for score, n in scores if score == max_score)
    position = available.index(anchor)
    left = max(0, min(position - max_lines // 2, len(available) - max_lines))
    return available[left:left + max_lines]


def _excerpt_relevance_score(
    code: str,
    *,
    context: str,
) -> int:
    """Score already-selected source lines without inventing sample-specific rules."""
    identifiers = {
        token.lower()
        for token in re.findall(r"[A-Za-z_][A-Za-z_0-9]{4,}", context)
    }
    identifiers -= {"source", "sink", "location", "behavior", "condition", "action"}
    lowered = code.lower()
    identifier_hits = sum(1 for token in identifiers if token in lowered)
    operation_lines = sum(
        1
        for line in code.splitlines()
        if "(" in line and not line.strip().startswith((
            "if ", "for ", "while ", "switch ", "return ", "int ", "char ", "void ",
        ))
    )
    return identifier_hits * 3 + operation_lines


def extract_evidence_excerpts(
    item: dict[str, Any],
    index: dict[str, dict[int, tuple[str, str]]],
    *,
    max_lines: int = 7,
    max_excerpts: int = 3,
) -> list[dict[str, Any]]:
    """Return up to ``max_excerpts`` verbatim excerpts from cited source ranges.

    The source is always taken from the supplied material index. The function
    never asks a model to rewrite or reconstruct code.
    """
    if max_excerpts <= 0:
        return []
    context = " ".join(
        str(item.get(k, "")) for k in ("behavior", "condition", "source", "action", "sink")
    )
    candidates: list[tuple[int, int, dict[str, Any]]] = []
    seen_ranges: set[tuple[str, int, int]] = set()

    evidence_items = item.get("evidence", [])
    for evidence_order, evidence in enumerate(evidence_items):
        location = str(evidence.get("location", ""))
        observation = str(evidence.get("observation", ""))
        for segment_order, match in enumerate(_REF.finditer(location)):
            fid = normalize_function_id(match.group("fid"))
            lines = index.get(fid)
            if not lines:
                continue
            a = int(match.group("start"))
            b = int(match.group("end") or a)
            if not (0 < a <= b and b - a <= 10000):
                continue
            if not all(line in lines for line in range(a, b + 1)):
                # Reporting must use the same strict source boundary as the
                # full-material evidence validator.
                continue
            key = (fid, a, b)
            if key in seen_ranges:
                continue
            chosen = _pick_window(
                lines, a, b, context + " " + observation, max_lines
            )
            if not chosen:
                continue
            code = "\n".join(f"L{n:04d}: {lines[n][0]}" for n in chosen)
            excerpt = {
                "function_id": fid,
                "evidence_location": location,
                "start_line": chosen[0],
                "end_line": chosen[-1],
                "source_packets": sorted({lines[n][1] for n in chosen}),
                "code": code,
            }
            score = _excerpt_relevance_score(code, context=context + " " + observation)
            # Prefer a stronger excerpt, then preserve the original evidence
            # order so deterministic output remains stable across runs.
            candidates.append((score, -(evidence_order * 1000 + segment_order), excerpt))
            seen_ranges.add(key)

    candidates.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return [item for _, _, item in candidates[:max_excerpts]]


def extract_evidence_excerpt(
    item: dict[str, Any], index: dict[str, dict[int, tuple[str, str]]],
    *, max_lines: int = 7,
) -> dict[str, Any] | None:
    """Backward-compatible single-excerpt helper."""
    excerpts = extract_evidence_excerpts(item, index, max_lines=max_lines, max_excerpts=1)
    return excerpts[0] if excerpts else None


def report_code_excerpts(
    report: dict[str, Any],
    packets_dir: Path | None,
    *,
    max_total: int = 24,
    max_per_claim: int = 3,
) -> dict[str, list[dict[str, Any]]]:
    """Select deterministic source excerpts for confirmed and suspected behaviors.

    One excerpt is reserved for each selected claim. When there are more
    eligible claims than the global limit, confirmed and suspected claims both
    receive budget, with roughly a 2:1 preference for confirmed behavior.
    Remaining capacity is used for additional evidence ranges, up to three
    excerpts per claim.
    """
    if packets_dir is None or not packets_dir.is_dir():
        return {}
    paths = sorted(p for p in packets_dir.glob("J*.md") if p.is_file())
    if not paths or max_total <= 0 or max_per_claim <= 0:
        return {}

    index = build_code_index(paths)
    core_items = list(report.get("core_behaviors", []))
    suspected_items = list(report.get("suspected_behaviors", []))
    prepared: dict[str, list[dict[str, Any]]] = {}

    for item in [*core_items, *suspected_items]:
        claim_id = str(item["canonical_id"])
        selected = extract_evidence_excerpts(
            item, index, max_lines=7, max_excerpts=max_per_claim
        )
        if selected:
            prepared[claim_id] = selected

    core_ids = [str(item["canonical_id"]) for item in core_items if str(item["canonical_id"]) in prepared]
    suspected_ids = [
        str(item["canonical_id"]) for item in suspected_items
        if str(item["canonical_id"]) in prepared
    ]

    def _allocate(total: int, first_count: int, second_count: int) -> tuple[int, int]:
        """Allocate a 2:1 budget while using all available eligible claims."""
        if first_count == 0:
            return 0, min(total, second_count)
        if second_count == 0:
            return min(total, first_count), 0
        first_budget = min(first_count, max(1, (total * 2) // 3))
        second_budget = min(second_count, total - first_budget)
        remaining = total - first_budget - second_budget
        if remaining:
            extra = min(remaining, first_count - first_budget)
            first_budget += extra
            remaining -= extra
        if remaining:
            second_budget += min(remaining, second_count - second_budget)
        return first_budget, second_budget

    core_budget, suspected_budget = _allocate(
        max_total, len(core_ids), len(suspected_ids)
    )
    selected_ids = [*core_ids[:core_budget], *suspected_ids[:suspected_budget]]

    excerpts: dict[str, list[dict[str, Any]]] = {
        claim_id: prepared[claim_id][:1] for claim_id in selected_ids
    }
    used_ranges: set[tuple[str, str, int, int]] = set()
    for claim_id, values in excerpts.items():
        for excerpt in values:
            used_ranges.add((
                claim_id,
                excerpt["function_id"],
                int(excerpt["start_line"]),
                int(excerpt["end_line"]),
            ))

    used = sum(len(value) for value in excerpts.values())
    # Additional excerpts are deliberately considered in core-first order.
    # Exact same function/range for another claim is still allowed because it
    # may support a different behavior, but never duplicates the same range
    # within one claim.
    extras = [
        (claim_id, rank, excerpt)
        for claim_id in selected_ids
        for rank, excerpt in enumerate(prepared[claim_id][1:], start=1)
    ]
    for claim_id, _, excerpt in extras:
        if used >= max_total:
            break
        current = excerpts[claim_id]
        if len(current) >= max_per_claim:
            continue
        key = (
            claim_id,
            excerpt["function_id"],
            int(excerpt["start_line"]),
            int(excerpt["end_line"]),
        )
        if key in used_ranges:
            continue
        current.append(excerpt)
        used_ranges.add(key)
        used += 1

    return excerpts
