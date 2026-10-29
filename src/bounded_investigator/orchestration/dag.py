from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

from pydantic import BaseModel, Field, model_validator

from ..schemas import RunEnvelope


class ManifestJob(BaseModel):
    job_id: str
    title: str = ""
    depends_on: list[str] = Field(default_factory=list)
    source_chars: int | None = None
    source_count: int | None = None


class DagManifest(BaseModel):
    purpose: str | None = None
    packet_count: int | None = None
    jobs: list[ManifestJob]

    @model_validator(mode="after")
    def validate_graph(self) -> "DagManifest":
        ids = [job.job_id for job in self.jobs]
        if len(ids) != len(set(ids)):
            raise ValueError("manifest contains duplicate job_id values")
        known = set(ids)
        for job in self.jobs:
            unknown = [dep for dep in job.depends_on if dep not in known]
            if unknown:
                raise ValueError(f"{job.job_id} depends on unknown jobs: {unknown}")
            if job.job_id in job.depends_on:
                raise ValueError(f"{job.job_id} cannot depend on itself")
        _topological_order(self.jobs)  # cycle check
        if self.packet_count is not None and self.packet_count != len(self.jobs):
            raise ValueError(
                f"packet_count={self.packet_count} does not match jobs={len(self.jobs)}"
            )
        return self

    def by_id(self) -> dict[str, ManifestJob]:
        return {job.job_id: job for job in self.jobs}


def _topological_order(jobs: Iterable[ManifestJob]) -> list[str]:
    jobs = list(jobs)
    deps = {job.job_id: set(job.depends_on) for job in jobs}
    remaining = set(deps)
    order: list[str] = []
    while remaining:
        ready = sorted(job_id for job_id in remaining if deps[job_id].isdisjoint(remaining))
        if not ready:
            cycle_nodes = sorted(remaining)
            raise ValueError(f"manifest DAG contains a cycle involving: {cycle_nodes}")
        order.extend(ready)
        remaining.difference_update(ready)
    return order


def load_manifest(path: str | Path) -> DagManifest:
    path = Path(path)
    return DagManifest.model_validate(json.loads(path.read_text(encoding="utf-8")))


def discover_manifest(input_dir: str | Path, explicit: str | Path | None = None) -> Path | None:
    if explicit is not None:
        path = Path(explicit)
        if not path.exists():
            raise FileNotFoundError(f"DAG manifest not found: {path}")
        return path

    input_dir = Path(input_dir)
    candidates = [input_dir / "manifest.json", input_dir.parent / "manifest.json"]
    for path in candidates:
        if path.exists():
            return path
    return None


def render_dependency_handoffs(
    dependency_ids: list[str],
    completed: dict[str, RunEnvelope],
    failures: dict[str, str],
) -> str:
    """Project only validated upstream facts into compact downstream context."""
    if not dependency_ids:
        return "none"

    blocks: list[str] = []
    for dep_id in dependency_ids:
        if dep_id in failures:
            blocks.extend([
                f"[dependency {dep_id}]",
                "status: unavailable",
                f"reason: {failures[dep_id]}",
                "[/dependency]",
            ])
            continue

        envelope = completed.get(dep_id)
        if envelope is None:
            blocks.extend([
                f"[dependency {dep_id}]",
                "status: unavailable",
                "reason: dependency result missing",
                "[/dependency]",
            ])
            continue

        if envelope.result is not None:
            result = envelope.result
            lines = [
                f"[dependency {dep_id}]",
                f"status: {envelope.delivery_status.value}",
                f"summary: {result.summary}",
                "claims:",
            ]
            if not result.claims:
                lines.append("- none")
            for claim in result.claims:
                evidence = ", ".join(ev.location for ev in claim.evidence)
                lines.append(
                    "- "
                    f"{claim.behavior} [confidence={claim.confidence.value}, evidence_level={claim.evidence_level.value}] | condition={claim.condition} | "
                    f"{claim.source} -> {claim.action} -> {claim.sink} | evidence={evidence}"
                )

            lines.append("artifacts:")
            if not result.artifacts:
                lines.append("- none")
            for artifact in result.artifacts:
                evidence = ", ".join(ev.location for ev in artifact.evidence)
                lines.append(
                    "- "
                    f"[{artifact.role}/{artifact.detection_specificity.value}] "
                    f"{artifact.kind}: {artifact.value} | {artifact.meaning} | evidence={evidence}"
                )

            lines.append("unresolved:")
            unresolved = False
            for gap in result.gaps:
                unresolved = True
                lines.append(
                    f"- gap [{gap.scope}/{gap.impact}] {gap.question} | {gap.reason}"
                )
            for handoff in result.handoffs:
                unresolved = True
                lines.append(
                    f"- handoff [{handoff.priority}] {handoff.target} | "
                    f"{handoff.reason} | expected={handoff.expected_value}"
                )
            for need in result.material_needs:
                unresolved = True
                evidence = ",".join(e.location for e in need.evidence)
                lines.append(
                    f"- needed material [{need.kind}/{need.impact}] {need.display_targets()} | "
                    f"{need.reason} | evidence={evidence}"
                )
            if not unresolved:
                lines.append("- none")
            lines.append("[/dependency]")
            blocks.extend(lines)
            continue

        if envelope.partial_result is not None:
            partial = envelope.partial_result
            lines = [
                f"[dependency {dep_id}]",
                "status: partial_validated",
                "note: only independently Pydantic-validated claims/artifacts are passed downstream",
                "claims:",
            ]
            if not partial.claims:
                lines.append("- none")
            for claim in partial.claims:
                evidence = ", ".join(ev.location for ev in claim.evidence)
                lines.append(
                    "- "
                    f"{claim.behavior} [confidence={claim.confidence.value}, evidence_level={claim.evidence_level.value}] | condition={claim.condition} | "
                    f"{claim.source} -> {claim.action} -> {claim.sink} | evidence={evidence}"
                )
            lines.append("artifacts:")
            if not partial.artifacts:
                lines.append("- none")
            for artifact in partial.artifacts:
                evidence = ", ".join(ev.location for ev in artifact.evidence)
                lines.append(
                    "- "
                    f"[{artifact.role}/{artifact.detection_specificity.value}] "
                    f"{artifact.kind}: {artifact.value} | {artifact.meaning} | evidence={evidence}"
                )
            lines.append("[/dependency]")
            blocks.extend(lines)
            continue

        blocks.extend([
            f"[dependency {dep_id}]",
            "status: unavailable",
            f"reason: upstream delivery_status={envelope.delivery_status.value}; no validated facts available",
            "[/dependency]",
        ])

    return "\n".join(blocks)
