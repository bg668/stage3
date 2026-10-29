"""Input-scoped, provenance-preserving material inventory.

This module indexes *what the run actually received*, rather than describing
what a malware sample is presumed to contain. Names mentioned in decompiled
code do not constitute corresponding payload/resource bytes.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from ..evidence.scope import EvidenceScope, MaterialScopeItem
from ..schemas import DeliveryStatus, MaterialTarget, RunEnvelope

MaterialKind = Literal["function_code", "resource_bytes", "data_bytes", "runtime_trace", "other"]

# Match actual code-block headings and declared line coverage, NOT mere mentions
# of a function in a call graph or analyst task description.
_FUNCTION_HEADER = re.compile(
    r"^##\s+(F[-_]?\d{1,5})\s+([a-zA-Z_][a-zA-Z0-9_]*).*?L(\d{1,6})\s*[-–—]\s*L(\d{1,6})\s*/\s*共\s*(\d+)\s*行",
    re.I | re.M,
)
_FUNC_TOKEN = re.compile(r"(?<![a-z0-9])F[-_]?0*(\d{1,5})(?![a-z0-9])", re.I)
_SUB_TOKEN = re.compile(r"\bsub_[0-9a-f]{5,16}\b", re.I)
_HEX_FUNC = re.compile(r"^\s*(F[-_]?\d{1,5}|sub_[0-9a-f]{5,16})\s*$", re.I)
_SKIP_PARTS = {".git", ".venv", "__pycache__", "node_modules", ".pytest_cache"}
_MAX_FILE_ENTRIES = 20000
# Extract explicit function references from multi-target strings, never
# from a free-form reason or a decompiled code body. Supports L0275-L0522.
_FUNCTION_TARGET = re.compile(
    r"(?<![A-Za-z0-9_])(?P<identifier>F[-_]?\d{1,5}|sub_[0-9a-f]{5,16})"
    r"(?:\s+(?:original\s+)?L(?P<start>\d{1,6})\s*[-–—]\s*L(?P<end>\d{1,6}))?"
    r"(?![A-Za-z0-9_])", re.I,
)


def normal_func_name(value: str) -> str | None:
    match = _FUNC_TOKEN.fullmatch(value.strip())
    if match:
        return f"F-{int(match.group(1)):04d}"
    if _SUB_TOKEN.fullmatch(value.strip()):
        return value.strip().lower()
    return None


class CodeSection(BaseModel):
    function_id: str
    symbol: str
    packet_id: str
    start_line: int
    end_line: int
    total_lines: int
    source_path: str


class IndexedFile(BaseModel):
    path: str
    relative_path: str
    size_bytes: int
    origin: Literal["packet", "additional_input"]
    format_hint: str


class InputInventory(BaseModel):
    schema_version: str = "1"
    packet_files: list[IndexedFile] = Field(default_factory=list)
    additional_files: list[IndexedFile] = Field(default_factory=list)
    code_sections: list[CodeSection] = Field(default_factory=list)
    scanned_roots: list[str] = Field(default_factory=list)
    scan_complete: bool = True
    warnings: list[str] = Field(default_factory=list)


class RequiredMaterial(BaseModel):
    kind: MaterialKind
    target: str
    reason: str
    source_packet: str
    requested_start_line: int | None = None
    requested_end_line: int | None = None
    impact: Literal["critical", "major", "minor"] = "major"
    evidence_locations: list[str] = Field(default_factory=list)
    source_field: Literal["material_needs", "handoff"] = "material_needs"


def _format_hint(path: Path) -> str:
    suffix = path.suffix.lower()
    return {
        ".exe": "executable_file", ".dll": "executable_file", ".sys": "executable_file",
        ".bin": "binary_file", ".dat": "binary_file", ".blob": "binary_file",
        ".rc": "resource_file", ".rsrc": "resource_file",
        ".log": "log_file", ".etl": "trace_file", ".pcap": "capture_file",
        ".jsonl": "json_lines", ".json": "json_file", ".md": "markdown_file",
        ".sqlite": "database_file", ".db": "database_file",
    }.get(suffix, "unknown_file_type")


def _index_file(path: Path, root: Path, origin: Literal["packet", "additional_input"]) -> IndexedFile:
    path = path.resolve()
    stat = path.stat()
    return IndexedFile(
        path=str(path.resolve()), relative_path=path.relative_to(root).as_posix(),
        size_bytes=stat.st_size, origin=origin, format_hint=_format_hint(path),
    )


def _parse_code_sections(packet_path: Path, text: str) -> list[CodeSection]:
    packet_id = packet_path.stem
    sections: list[CodeSection] = []
    for match in _FUNCTION_HEADER.finditer(text):
        func, symbol, start, end, total = match.groups()
        a, b, t = int(start), int(end), int(total)
        # Only accept ranges that actually contain a fenced body. Truncation and
        # mistaken markdown headings should not prove a function is available.
        next_heading = text.find("\n## ", match.end())
        section_text = text[match.end():next_heading if next_heading != -1 else len(text)]
        if not (1 <= a <= b <= t and re.search(r"```(?:text|c|cpp|asm)?\s*\n", section_text)):
            continue
        sections.append(CodeSection(
            function_id=normal_func_name(func) or func, symbol=symbol,
            packet_id=packet_id, start_line=a, end_line=b, total_lines=t,
            source_path=str(packet_path.resolve()),
        ))
    return sections


def build_input_inventory(packet_paths: list[Path], additional_root: Path | None = None) -> InputInventory:
    """Index actual supplied files. No inferential malware classification is done."""
    inventory = InputInventory()
    if packet_paths:
        try:
            common = Path(__import__("os").path.commonpath([str(p.resolve().parent) for p in packet_paths]))
        except ValueError:
            common = packet_paths[0].parent.resolve()
        inventory.scanned_roots.append(str(common))
        for packet in sorted(packet_paths):
            if not packet.is_file() or packet.is_symlink():
                inventory.warnings.append(f"Packet unreadable: {packet}")
                inventory.scan_complete = False
                continue
            try:
                inventory.packet_files.append(_index_file(packet, common, "packet"))
                inventory.code_sections.extend(_parse_code_sections(packet, packet.read_text(encoding="utf-8")))
            except (OSError, UnicodeError) as exc:
                inventory.warnings.append(f"Could not scan packet {packet}: {type(exc).__name__}")
                inventory.scan_complete = False
    # Also inspect sidecar files placed alongside packets, without requiring
    # the user to name a special evidence directory. An optional additional
    # root can expand the inspection scope (not override the packet directory).
    packet_paths_resolved = {p.resolve() for p in packet_paths}
    roots: list[Path] = [common] if packet_paths else []
    if additional_root is not None:
        additional_root = additional_root.resolve()
        if not additional_root.is_dir():
            raise ValueError(f"Input material directory is not a directory: {additional_root}")
        if additional_root not in roots:
            roots.append(additional_root)
    for root in roots:
        if str(root) not in inventory.scanned_roots:
            inventory.scanned_roots.append(str(root))
        count = 0
        for path in sorted(root.rglob("*")):
            if (path.is_symlink() or not path.is_file() or
                    any(part in _SKIP_PARTS for part in path.relative_to(root).parts)):
                continue
            if path.name.startswith(".env") or path.resolve() in packet_paths_resolved:
                continue
            count += 1
            if count > _MAX_FILE_ENTRIES:
                inventory.warnings.append(f"Additional input scan limit reached at {root}")
                inventory.scan_complete = False
                break
            try:
                inventory.additional_files.append(_index_file(path, root, "additional_input"))
            except OSError:
                inventory.scan_complete = False
                inventory.warnings.append(f"Could not stat {path}")
    return inventory


def _split_function_targets(raw: str) -> list[MaterialTarget]:
    """Normalize scalar requests such as 'sub_409EF8 L0275-L0522'.

    Never consider arbitrary words or function mentions in a *reason* to be
    material targets. Unparseable scalar inputs remain unresolved rather than
    being silently invented or discarded.
    """
    found: list[MaterialTarget] = []
    seen: set[tuple[str, int | None, int | None]] = set()
    for match in _FUNCTION_TARGET.finditer(raw):
        identifier = normal_func_name(match.group("identifier"))
        if identifier is None:
            continue
        a = int(match.group("start")) if match.group("start") else None
        b = int(match.group("end")) if match.group("end") else None
        if a is not None and a > b:
            continue
        key = (identifier, a, b)
        if key not in seen:
            seen.add(key)
            found.append(MaterialTarget(identifier=identifier, start_line=a, end_line=b))
    return found


def collect_material_needs(completed_results: dict[str, RunEnvelope]) -> list[RequiredMaterial]:
    """Explode each validated function request to ONE function/range.

    Structured targets are preferred; scalar function requests are parsed
    without granting their free-form remainder authority.
    """
    needs: list[RequiredMaterial] = []
    for packet_id, envelope in sorted(completed_results.items()):
        result = envelope.result
        if result is None or envelope.delivery_status not in {
            DeliveryStatus.SUCCESS, DeliveryStatus.SUCCESS_AFTER_REPAIR
        }:
            continue
        for need in result.material_needs:
            if need.kind == "function_code":
                targets = need.targets or _split_function_targets(need.target)
                if not targets:
                    # Preserve an unparseable request as unknown; don't guess.
                    needs.append(RequiredMaterial(
                        kind=need.kind, target=need.target,
                        reason=need.reason, source_packet=packet_id,
                        impact=need.impact,
                        evidence_locations=[e.location for e in need.evidence],
                    ))
                    continue
                for function in targets:
                    needs.append(RequiredMaterial(
                        kind="function_code", target=function.identifier,
                        requested_start_line=function.start_line,
                        requested_end_line=function.end_line,
                        reason=need.reason, source_packet=packet_id,
                        impact=need.impact,
                        evidence_locations=[e.location for e in need.evidence],
                    ))
            else:
                needs.append(RequiredMaterial(
                    kind=need.kind, target=need.target, reason=need.reason,
                    source_packet=packet_id, impact=need.impact,
                    evidence_locations=[e.location for e in need.evidence],
                ))
        for handoff in result.handoffs:
            if handoff.priority != "high":
                continue
            # Handoff fallback remains conservative: a single exact identifier;
            # multi-function handoffs are already covered by material_needs.
            target = normal_func_name(handoff.target)
            if target is not None:
                needs.append(RequiredMaterial(
                    kind="function_code", target=target,
                    reason=handoff.reason, source_packet=packet_id,
                    impact="major", source_field="handoff",
                ))
    return needs


def _merge_ranges(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for start, end in sorted(ranges):
        if out and start <= out[-1][1] + 1:
            out[-1] = out[-1][0], max(end, out[-1][1])
        else:
            out.append((start, end))
    return out


def _function_lookup(
    target: str, inventory: InputInventory,
    start_line: int | None = None, end_line: int | None = None,
) -> tuple[str, str, list[str], list[str]]:
    canonical = normal_func_name(target)
    if not canonical:
        return "unknown", "Function target is not an exact identifier", [], []
    matches = [s for s in inventory.code_sections if s.function_id == canonical or s.symbol.lower() == canonical]
    # A full exported function may be supplied as a separate source file,
    # rather than through a bounded packet. Filename must match exactly;
    # source contents are not executed or semantically interpreted here.
    code_suffixes = {".txt", ".c", ".cpp", ".asm", ".i", ".json"}
    exported = [f for f in inventory.additional_files
                if Path(f.relative_path).suffix.lower() in code_suffixes
                and normal_func_name(Path(f.relative_path).stem) == canonical
                and f.size_bytes > 0]
    if exported:
        if start_line is not None:
            # An unparsed sidecar file proves the function exists, but not that
            # the requested original source range can be verified from it.
            return (
                "unknown",
                "Separate exported function file exists, but requested line coverage was not indexed",
                sorted({f.path for f in exported}), [],
            )
        return (
            "available",
            "Separate nonempty exported function source file exists (source range/completeness not independently verified)",
            sorted({f.path for f in exported}), [],
        )
    if not matches:
        if not inventory.scan_complete:
            return "unknown", "Input index incomplete; absence cannot be determined", [], []
        return "not_in_input", "No matching function code section or named source file in this run's scoped input", [], []
    ranges = _merge_ranges([(s.start_line, s.end_line) for s in matches])
    total = max(s.total_lines for s in matches)
    sources = sorted({s.source_path for s in matches})
    packet_ids = sorted({s.packet_id for s in matches})
    if start_line is not None and end_line is not None:
        if end_line > total:
            return (
                "partial",
                f"Requested original lines L{start_line}-L{end_line} exceed declared function length {total}; "
                f"observed ranges: {ranges}", sources, packet_ids,
            )
        covered = sum(max(0, min(b, end_line) - max(a, start_line) + 1) for a,b in ranges)
        requested = end_line - start_line + 1
        detail = (f"Requested L{start_line}-L{end_line}: {covered}/{requested} lines "
                  f"supplied across {packet_ids}; observed ranges {ranges}")
        return ("available" if covered == requested else "partial"), detail, sources, packet_ids
    observed = sum(b-a+1 for a,b in ranges)
    detail = f"Observed line ranges {ranges}; {observed}/{total} declared lines supplied in {packet_ids}"
    return ("available" if ranges == [(1, total)] else "partial"), detail, sources, packet_ids


def _file_lookup(kind: MaterialKind, target: str, inventory: InputInventory) -> tuple[str, str, list[str], list[str]]:
    # File match is by path or exact basename, *never* by incidental strings in
    # source code. A C string containing a resource name is not its raw bytes.
    cleaned = target.strip().replace("\\", "/")
    if (not cleaned or cleaned in {"*", ".", "unknown"} or
            any(ch in cleaned for ch in "*?[]") or " " in cleaned):
        return "unknown", "No unambiguous file/blob identifier to check", [], []
    matches = [f for f in inventory.additional_files if (
        f.relative_path.casefold() == cleaned.casefold() or
        Path(f.relative_path).name.casefold() == cleaned.casefold()
    ) and f.size_bytes > 0]
    if matches:
        return "available", "Matching nonempty file bytes are present in the provided additional input directory", [f.path for f in matches], []
    if len(inventory.scanned_roots) < 2:
        return "unknown", "Only analysis packets were indexed; no independent data/trace input directory was scanned", [], []
    if not inventory.scan_complete:
        return "unknown", "Input index incomplete; absence cannot be determined", [], []
    return "not_in_input", "No matching material bytes in the supplied additional input files; this says nothing about the original sample", [], []


def build_automatic_scope(
    inventory: InputInventory,
    completed_results: dict[str, RunEnvelope],
) -> tuple[EvidenceScope, list[RequiredMaterial]]:
    requests = collect_material_needs(completed_results)
    grouped: dict[tuple[MaterialKind, str, int | None, int | None], list[RequiredMaterial]] = defaultdict(list)
    for request in requests:
        identifier = normal_func_name(request.target) if request.kind == "function_code" else None
        grouped[(request.kind, (identifier or request.target.strip()).casefold(),
                 request.requested_start_line, request.requested_end_line)].append(request)
    materials: list[MaterialScopeItem] = []
    for (kind, normalized_target, start_line, end_line), entries in sorted(
        grouped.items(), key=lambda kv: (kv[0][0], kv[0][1], kv[0][2] or 0, kv[0][3] or 0)
    ):
        if kind == "function_code":
            state, details, sources, matching_packets = _function_lookup(
                normalized_target, inventory, start_line, end_line
            )
        elif kind in {"resource_bytes", "data_bytes", "runtime_trace"}:
            state, details, sources, matching_packets = _file_lookup(kind, normalized_target, inventory)
        else:
            state, details, sources, matching_packets = "unknown", "Unclassified material request; no deterministic availability check", [], []
        source_packets = sorted({x.source_packet for x in entries})
        submitted = [pid for pid in matching_packets if pid in completed_results]
        submission = ("submitted_to_worker" if submitted else "not_submitted") if state in {"available", "partial"} else "unknown"
        specificity = hashlib.sha256(
            f"{kind}\0{normalized_target}\0{start_line}\0{end_line}".encode()
        ).hexdigest()[:12]
        impact = min((e.impact for e in entries), key={"critical": 0, "major": 1, "minor": 2}.get)
        display_target = (f"{entries[0].target} L{start_line}-L{end_line}"
                          if start_line is not None else entries[0].target)
        materials.append(MaterialScopeItem(
            scope_id=f"auto_{specificity}", label=f"{kind}: {display_target}",
            status=state, impact=impact,
            source="; ".join(sources) if sources else f"auto_input_inventory:{','.join(inventory.scanned_roots)}",
            details=details,
            kind=kind, target=entries[0].target,
            requested_start_line=start_line, requested_end_line=end_line,
            requested_by_packets=source_packets,
            request_evidence=sorted({loc for e in entries for loc in e.evidence_locations}),
            request_reason="；".join(dict.fromkeys(e.reason.strip() for e in entries if e.reason.strip()))[:1500],
            submission_status=submission,
        ))
    per_function: dict[str, list[CodeSection]] = defaultdict(list)
    for section in inventory.code_sections:
        per_function[section.function_id].append(section)
    incomplete = []
    for fid, parts in sorted(per_function.items()):
        covered = _merge_ranges([(p.start_line, p.end_line) for p in parts])
        full_length = max(p.total_lines for p in parts)
        if covered != [(1, full_length)]:
            incomplete.append(fid)
    summary = {
        "packet_count": len(inventory.packet_files),
        "indexed_function_count": len(per_function),
        "indexed_code_sections": len(inventory.code_sections),
        "partial_function_count": len(incomplete),
        "partial_function_ids": incomplete[:30],
        "additional_file_count": len(inventory.additional_files),
        "additional_file_format_hints": sorted(set(f.format_hint for f in inventory.additional_files)),
        "inventory_scan_complete": inventory.scan_complete,
        "material_requests_count": len(requests),
        "scanned_roots": inventory.scanned_roots,
    }
    return EvidenceScope(materials=materials, inventory_summary=summary), requests


def dump_inventory_json(inventory: InputInventory) -> str:
    return json.dumps(inventory.model_dump(mode="json"), ensure_ascii=False, indent=2)


def merge_expert_scope(automatic: EvidenceScope, expert: EvidenceScope) -> EvidenceScope:
    """Optional expert supplement. Automatic matches cannot be contradicted silently."""
    merged = {m.scope_id: m for m in automatic.materials}
    for item in expert.materials:
        if item.scope_id in merged and merged[item.scope_id].status != item.status:
            raise ValueError(f"Expert scope contradicts automatically observed material {item.scope_id}")
        merged[item.scope_id] = item
    return EvidenceScope(materials=list(merged.values()), inventory_summary=automatic.inventory_summary)
