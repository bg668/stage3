"""Reconcile saved packet results without rerunning packet investigations."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from ..config import AppConfig
from ..evidence.scope import (
    EvidenceScope, load_evidence_scope, packet_coverage_from_results,
)
from ..evidence.inventory import (
    build_input_inventory,
    build_automatic_scope,
    dump_inventory_json,
    merge_expert_scope,
)
from ..io import write_error
from ..logging_utils import get_logger
from ..reporting.service import clear_report_outputs, save_final_outputs
from ..schemas import RunEnvelope


def load_saved_packet_results(folder: Path) -> tuple[dict[str, RunEnvelope], dict[str, str]]:
    """Load saved validated envelopes, never raw streams or packet source."""
    completed: dict[str, RunEnvelope] = {}
    failures: dict[str, str] = {}
    for path in sorted(folder.glob("J*.json")):
        if not re.fullmatch(r"J\d+", path.stem):
            continue
        try:
            completed[path.stem] = RunEnvelope.model_validate_json(path.read_text(encoding="utf-8"))
        except Exception as exc:
            failures[path.stem] = f"Saved envelope unreadable: {type(exc).__name__}: {exc}"
    for path in sorted(folder.glob("J*.error.json")):
        job_id = path.name.split(".")[0]
        if re.fullmatch(r"J\d+", job_id) and job_id not in completed:
            failures[job_id] = f"Worker error saved in {path.name}"
    if not completed:
        raise ValueError(f"No usable Jxx.json packet envelopes found in {folder}")
    return completed, failures


async def reconcile_saved_results(
    *,
    packet_results_dir: Path,
    output_dir: Path,
    config: AppConfig,
    scope_file: Path | None = None,
    packets_dir: Path | None = None,
    evidence_dir: Path | None = None,
) -> dict[str, object]:
    """Reconcile saved packet facts and regenerate deterministic reports."""
    from ..workflow import BoundedInvestigationWorkflow
    if not config.global_reconciliation.enabled:
        raise ValueError("Set global_reconciliation.enabled=true in config")
    if evidence_dir is not None and packets_dir is None:
        raise ValueError("--evidence-dir requires --packets-dir")
    if packet_results_dir.resolve() == output_dir.resolve():
        raise ValueError("Replay output directory must differ from saved Worker results directory")
    log = get_logger("reconcile")
    completed, failures = load_saved_packet_results(packet_results_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    # A failed new replay must never leave an older successful report looking current.
    clear_report_outputs(output_dir)
    for stale in (
        "_global_reconciliation.json", "_global_reconciliation.error.json", "_reconcile_summary.json",
    ):
        old_file = output_dir / stale
        if old_file.is_file():
            old_file.unlink()
    if packets_dir is not None:
        packet_paths = sorted(packets_dir.glob("J*.md"))
        if not packet_paths:
            raise ValueError(f"No J*.md packets found in {packets_dir}")
        inventory = build_input_inventory(packet_paths, additional_root=evidence_dir)
        scope, material_requests = build_automatic_scope(inventory, completed)
        (output_dir / "_evidence_inventory.json").write_text(
            dump_inventory_json(inventory), encoding="utf-8"
        )
        (output_dir / "_material_requests.json").write_text(
            json.dumps([x.model_dump(mode="json") for x in material_requests], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    else:
        saved_scope = packet_results_dir / "_evidence_scope.json"
        scope = load_evidence_scope(saved_scope) if saved_scope.is_file() else EvidenceScope()
        if not saved_scope.is_file():
            log.warning("command=reconcile missing_saved_scope; supply --packets-dir for inventory")
    scope = merge_expert_scope(scope, load_evidence_scope(scope_file))
    (output_dir / "_evidence_scope.json").write_text(scope.model_dump_json(indent=2), encoding="utf-8")
    log.info(
        "command=reconcile start completed_packets=%s failed_packets=%s output=%s",
        len(completed), len(failures), output_dir,
    )
    started = time.monotonic()
    workflow = BoundedInvestigationWorkflow(config)
    try:
        global_result = await workflow.run_global_reconciliation(
            completed_results=completed,
            failures=failures,
            artifact_dir=output_dir / "_global_artifacts",
        )
        path = output_dir / "_global_reconciliation.json"
        path.write_text(global_result.model_dump_json(indent=2), encoding="utf-8")
        final_summary = save_final_outputs(
            output_dir, global_result, evidence_scope=scope,
            packet_coverage=packet_coverage_from_results(completed, failures),
            packet_source_dir=packets_dir,
        )
        summary = {
            "source_results_dir": str(packet_results_dir),
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "completed_packets": sorted(completed),
            "failed_packets": failures,
            "delivery_status": global_result.delivery_status.value,
            "provenance_valid": global_result.provenance_valid,
            "output": path.name,
            "knowledge_report": final_summary,
            "usage": global_result.usage.model_dump(),
        }
        (output_dir / "_reconcile_summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        log.info("command=reconcile complete status=%s elapsed=%.1fs", summary["delivery_status"], summary["elapsed_seconds"])
        return summary
    except Exception as exc:
        clear_report_outputs(output_dir)
        write_error(output_dir / "_global_reconciliation.error.json", "GLOBAL", exc)
        log.error("command=reconcile failed error=%s: %s", type(exc).__name__, exc)
        raise
