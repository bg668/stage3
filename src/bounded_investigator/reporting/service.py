"""Save and rebuild deterministic reports without calling models."""

from __future__ import annotations

import json
from pathlib import Path

from ..evidence.scope import (
    EvidenceScope,
    PacketCoverage,
    coverage_warnings,
    load_evidence_scope,
    packet_coverage_from_directory,
)
from ..evidence.inventory import (
    build_input_inventory,
    build_automatic_scope,
    dump_inventory_json,
    merge_expert_scope,
)
from ..logging_utils import get_logger
from ..schemas import GlobalReconciliationEnvelope, RunEnvelope
from .knowledge_report import save_knowledge_outputs
from .material_suggestions import build_material_suggestions


_REPORT_OUTPUTS = (
    "_report_ready.json", "_final_report.md", "_knowledge_audit.json",
    "_evidence_reference_audit.json",
)


def clear_report_outputs(output_dir: Path) -> None:
    """Remove current report artifacts before a new attempt can leave stale results."""
    for filename in _REPORT_OUTPUTS:
        path = output_dir / filename
        if path.is_file():
            path.unlink()


def save_final_outputs(
    output_dir: Path,
    global_envelope: GlobalReconciliationEnvelope,
    *,
    evidence_scope: EvidenceScope | None = None,
    packet_coverage: list[PacketCoverage] | None = None,
    packet_source_dir: Path | None = None,
) -> dict[str, object]:
    """Project validated knowledge to three report levels without model calls."""
    clear_report_outputs(output_dir)
    summary: dict[str, object] = {"mode": "deterministic", "completed": False}
    if global_envelope.result is None or not global_envelope.provenance_valid:
        summary["reason"] = "canonical facts missing or provenance gate failed"
        return summary
    scope = evidence_scope or EvidenceScope()
    warnings = [c.model_dump(mode="json") for c in coverage_warnings(packet_coverage or [])]
    paths = save_knowledge_outputs(
        output_dir,
        global_envelope.result,
        material_suggestions=build_material_suggestions(scope),
        input_coverage_warnings=warnings,
        source_claim_count=global_envelope.source_claim_count or None,
        source_artifact_count=global_envelope.source_artifact_count or None,
        packet_source_dir=packet_source_dir,
    )
    report_path, markdown_path, audit_path, audit = paths
    report_data = json.loads(report_path.read_text(encoding="utf-8"))
    summary.update({
        "completed": bool(audit["passed"]),
        "output": report_path.name,
        "report_ready": report_path.name,
        "report_markdown": markdown_path.name,
        "knowledge_audit": audit_path.name,
        "evidence_reference_audit": "_evidence_reference_audit.json",
        "audit_passed": audit["passed"],
        "audit_issues": audit["issues"],
        "confirmed_behavior_count": len(report_data["core_behaviors"]),
        "suspected_behavior_count": len(report_data["suspected_behaviors"]),
        "investigation_lead_count": len(report_data["investigation_leads"]),
        "legacy_unclassified_count": sum(
            item["kind"] == "legacy_unclassified_claim"
            for item in report_data["investigation_leads"]
        ),
        "historical_gap_count": sum(
            item["kind"] == "historical_gap"
            for item in report_data["investigation_leads"]
        ),
        "ioc_count": len(report_data["iocs"]),
        "coverage_warning_count": len(warnings),
        "material_suggestion_count": len(report_data["material_suggestions"]),
        "model_requests": 0,
        "code_excerpt_count": sum(
            len(value)
            for value in report_data.get("code_excerpts", {}).values()
        ),
        "code_excerpt_claim_count": len(report_data.get("code_excerpts", {})),
    })
    return summary


def report_saved_results(
    *,
    global_result: Path,
    output_dir: Path,
    scope_file: Path | None = None,
    packets_dir: Path | None = None,
    evidence_dir: Path | None = None,
    packet_results_dir: Path | None = None,
) -> dict[str, object]:
    """Rebuild an evidence report from saved canonical facts, without model calls."""
    envelope = GlobalReconciliationEnvelope.model_validate_json(
        global_result.read_text(encoding="utf-8")
    )
    if envelope.result is None or not envelope.provenance_valid:
        raise ValueError("Canonical result is missing or has not passed provenance validation")
    output_dir.mkdir(parents=True, exist_ok=True)
    if evidence_dir is not None and packets_dir is None:
        raise ValueError("--evidence-dir requires --packets-dir when using assess to rebuild an index")
    source_result_dir = packet_results_dir if packet_results_dir is not None else global_result.parent
    packet_coverage = packet_coverage_from_directory(source_result_dir, canonical=envelope.result)
    saved_scope = global_result.parent / "_evidence_scope.json"
    if packets_dir is not None:
        packet_paths = sorted(packets_dir.glob("J*.md"))
        if not packet_paths:
            raise ValueError(f"No J*.md packet files found: {packets_dir}")
        inventory = build_input_inventory(packet_paths, additional_root=evidence_dir)
        saved_results = {}
        for pth in source_result_dir.glob("J*.json"):
            try:
                saved_results[pth.stem] = RunEnvelope.model_validate_json(
                    pth.read_text(encoding="utf-8")
                )
            except Exception:
                continue
        scope, requirements = build_automatic_scope(inventory, saved_results)
        (output_dir / "_evidence_inventory.json").write_text(
            dump_inventory_json(inventory), encoding="utf-8"
        )
        (output_dir / "_material_requests.json").write_text(
            json.dumps([r.model_dump(mode="json") for r in requirements], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    elif saved_scope.is_file():
        scope = load_evidence_scope(saved_scope)
    else:
        scope = EvidenceScope()  # No inventory: availability is unknown; never guess.
        get_logger("cli").warning(
            "command=assess missing_auto_inventory source=%s; pass --packets-dir to rebuild",
            global_result.parent,
        )
    scope = merge_expert_scope(scope, load_evidence_scope(scope_file))
    (output_dir / "_evidence_scope.json").write_text(
        scope.model_dump_json(indent=2), encoding="utf-8"
    )
    # No model. This command is also published as `report` and works offline.
    (output_dir / "_global_reconciliation.reported.json").write_text(
        envelope.model_dump_json(indent=2), encoding="utf-8"
    )
    summary = save_final_outputs(
        output_dir, envelope, evidence_scope=scope, packet_coverage=packet_coverage,
        packet_source_dir=packets_dir,
    )
    (output_dir / "_report_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    get_logger("cli").info(
        "command=report completed=%s confirmed=%s suspected=%s leads=%s model_requests=0",
        summary["completed"], summary.get("confirmed_behavior_count", 0),
        summary.get("suspected_behavior_count", 0), summary.get("investigation_lead_count", 0),
    )

    return summary
