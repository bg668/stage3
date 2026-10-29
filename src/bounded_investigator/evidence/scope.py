"""Generic material availability and packet-delivery coverage.

Automatically indexed input + validated Worker material requests are the normal
source; a manually attested scope is supported only as optional expert metadata.
Never infer original-sample absence from missing analyzed files or Worker failure.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, model_validator

from ..schemas import DeliveryStatus, GlobalReconciliationResult, RunEnvelope

Availability = Literal["available", "partial", "not_in_input", "unavailable", "unknown"]
Impact = Literal["critical", "major", "minor"]
PacketStatus = Literal[
    "success", "success_after_repair", "partial_validated",
    "raw_only", "execution_failed", "missing", "unknown",
]


class MaterialScopeItem(BaseModel):
    scope_id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    label: str = Field(min_length=1)
    status: Availability = "unknown"
    impact: Impact = "major"
    source: str | None = None
    details: str = ""
    kind: str | None = None
    target: str | None = None
    requested_start_line: int | None = None
    requested_end_line: int | None = None
    requested_by_packets: list[str] = Field(default_factory=list)
    request_evidence: list[str] = Field(default_factory=list)
    request_reason: str = ""  # original Worker explanation, when present
    submission_status: Literal["submitted_to_worker", "not_submitted", "unknown"] = "unknown"

    @model_validator(mode="after")
    def verify_basis(self) -> "MaterialScopeItem":
        if self.status in {"available", "partial", "not_in_input", "unavailable"}:
            if not self.source or not self.source.strip():
                raise ValueError(f"materials.{self.scope_id}: {self.status} requires an explicit source")
            if not self.details.strip():
                raise ValueError(f"materials.{self.scope_id}: {self.status} requires supporting details")
        return self


class EvidenceScope(BaseModel):
    schema_version: str = "1"
    materials: list[MaterialScopeItem] = Field(default_factory=list)
    inventory_summary: dict[str, object] = Field(default_factory=dict)

    @model_validator(mode="after")
    def unique_ids(self) -> "EvidenceScope":
        ids = [item.scope_id for item in self.materials]
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate material scope_id values are not allowed")
        return self


def load_evidence_scope(path: str | Path | None) -> EvidenceScope:
    """Expert-only read of an evidence scope; never required in automatic mode."""
    if path is None:
        return EvidenceScope()
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"Evidence scope not found: {path}")
    content = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        data = json.loads(content)
    elif path.suffix.lower() in {".yaml", ".yml"}:
        data = yaml.safe_load(content)
    else:
        raise ValueError("Evidence scope must be .json, .yaml or .yml")
    return EvidenceScope.model_validate(data or {})


class PacketCoverage(BaseModel):
    packet_id: str
    delivery_status: PacketStatus
    validated_claims: int = 0
    validated_artifacts: int = 0
    note: str = ""


def _coverage_from_envelope(packet_id: str, envelope: RunEnvelope) -> PacketCoverage:
    result = envelope.result
    partial = envelope.partial_result
    if envelope.delivery_status == DeliveryStatus.PARTIAL_VALIDATED:
        return PacketCoverage(
            packet_id=packet_id, delivery_status="partial_validated",
            validated_claims=len(partial.claims) if partial else 0,
            validated_artifacts=len(partial.artifacts) if partial else 0,
            note="Only independently validated partial items are usable; this does not imply source code is absent",
        )
    if envelope.delivery_status in {DeliveryStatus.RAW_ONLY, DeliveryStatus.EXECUTION_FAILED}:
        return PacketCoverage(
            packet_id=packet_id, delivery_status=envelope.delivery_status.value,
            note="No validated structured result; this does not mean source code or resource bytes are missing",
        )
    return PacketCoverage(
        packet_id=packet_id, delivery_status=envelope.delivery_status.value,
        validated_claims=len(result.claims) if result else 0,
        validated_artifacts=len(result.artifacts) if result else 0,
    )


def packet_coverage_from_results(
    completed_results: dict[str, RunEnvelope],
    failures: dict[str, str],
) -> list[PacketCoverage]:
    items = [_coverage_from_envelope(pid, env) for pid, env in sorted(completed_results.items())]
    items.extend(
        PacketCoverage(packet_id=pid, delivery_status="execution_failed", note=f"Worker run failed: {reason}")
        for pid, reason in sorted(failures.items()) if pid not in completed_results
    )
    return sorted(items, key=lambda entry: entry.packet_id)


def packet_coverage_from_directory(
    folder: Path,
    *,
    canonical: GlobalReconciliationResult | None = None,
) -> list[PacketCoverage]:
    """Replay path: read saved Jxx envelopes, never packet source or raw model output."""
    items: dict[str, PacketCoverage] = {}
    if folder.is_dir():
        for path in sorted(folder.glob("J*.json")):
            if not re.fullmatch(r"J\d+", path.stem):
                continue
            pid = path.stem
            try:
                env = RunEnvelope.model_validate_json(path.read_text(encoding="utf-8"))
                items[pid] = _coverage_from_envelope(pid, env)
            except Exception as exc:
                items[pid] = PacketCoverage(
                    packet_id=pid, delivery_status="unknown",
                    note=f"Saved packet envelope unreadable ({type(exc).__name__}); not evidence of missing source code",
                )
    if canonical is not None:
        for pid in canonical.unavailable_packets:
            if pid not in items:
                items[pid] = PacketCoverage(
                    packet_id=pid, delivery_status="unknown",
                    note="Canonical marked packet unavailable; no validated delivery envelope loaded",
                )
    return sorted(items.values(), key=lambda entry: entry.packet_id)


def coverage_warnings(coverage: list[PacketCoverage]) -> list[PacketCoverage]:
    """System delivery warnings; these must NOT become malware behavior gaps."""
    return [x for x in coverage if x.delivery_status not in {"success", "success_after_repair"}]
