"""Serialize preprocessing domain data into the stable ``preprocess.json`` contract."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path

from .model import Function
from .relations import Relationships

# v4 removes the redundant derived candidate list. Function decisions remain authoritative.
SCHEMA_VERSION = 4


def function_to_dict(fn: Function) -> dict:
    return {
        "ref": fn.ref,
        "address": fn.hex_address,
        "name": fn.name,
        "size": fn.size,
        "flags": fn.flags,
        "code_chars": len(fn.code),
        "is_entry": fn.is_entry,
        "is_export": fn.is_export,
        "export_name": fn.export_name,
        "is_thunk": fn.is_thunk,
        "is_library": fn.is_library,
        "has_indirect_call": fn.has_indirect_call,
        "decompile_status": fn.decompile_status,
        "basic_block_count": fn.basic_block_count,
        "loop_count": fn.loop_count,
        "in_degree": fn.in_degree,
        "out_degree": fn.out_degree,
        "api_refs": fn.api_refs,
        "dangerous_apis": fn.dangerous_apis,
        "string_refs": fn.string_refs,
        "decision": fn.decision,
        "reason": fn.decision_reason,
        "detail": fn.decision_detail,
        "analysis_role": fn.analysis_role,
        "has_user_name": fn.has_user_name,
        "capability_hit_ids": [hit.hit_id for hit in fn.capability_hits],
    }


def build_payload(
    *,
    sqlite_path: Path,
    sample_name: str,
    sample_sha256: str | None,
    deepextract_version: str | None,
    arch: str | None,
    bitness: int | None,
    flirt_status: str,
    config: dict,
    code_view_comparison: list[dict] | None,
    stats: dict,
    diagnostics: dict,
    assertions: list[dict],
    functions: list[Function],
    capability_hits,
    relationships: Relationships,
    units: list[dict],
) -> dict:
    unit_sizes = [len(unit["subject_refs"]) for unit in units] or [0]
    unit_chars = [unit["chars"] for unit in units] or [0]
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "sample": {
            "name": sample_name,
            "sqlite_path": str(sqlite_path),
            "sqlite_sha256": _sha256_file(sqlite_path),
            "sample_sha256": sample_sha256,
            "deepextract_version": deepextract_version,
            "arch": arch,
            "bitness": bitness,
            "flirt_status": flirt_status,
        },
        "config": config,
        "code_view_comparison": code_view_comparison,
        "stats": stats,
        "units_summary": {
            "count": len(units),
            "mean_functions": round(sum(unit_sizes) / len(unit_sizes), 1),
            "mean_chars": round(sum(unit_chars) / len(unit_chars), 1),
            "total_chars": sum(unit_chars),
        },
        "diagnostics": diagnostics,
        "assertions": assertions,
        "functions": [function_to_dict(fn) for fn in functions],
        "capability_hits": [hit.to_dict() for hit in capability_hits],
        "call_edges": relationships.call_edges,
        "reference_edges": relationships.reference_edges,
        "vtable_groups": relationships.vtable_groups,
        "global_edges": [edge.to_dict() for edge in relationships.global_edges],
        "units": [{key: value for key, value in unit.items() if key != "text"} for unit in units],
    }


def _sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()
