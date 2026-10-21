from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path

import typer

from .config import load_config
from .dag import discover_manifest, load_manifest, render_dependency_handoffs
from .io import read_packet, write_error, write_result
from .logging_utils import configure_logging, get_logger
from .schemas import GlobalReconciliationEnvelope, RunEnvelope
from .final_assessment import build_material_suggestions
from .knowledge_report import save_knowledge_outputs
from .evidence_scope import (
    EvidenceScope, load_evidence_scope, packet_coverage_from_results,
    packet_coverage_from_directory, PacketCoverage, coverage_warnings,
)
from .material_inventory import (
    build_input_inventory, build_automatic_scope,
    dump_inventory_json, merge_expert_scope,
)

app = typer.Typer(add_completion=False, no_args_is_help=True)

@app.command("version")
def version() -> None:
    """Print the loaded code, schema and entrypoint paths to detect stale installs."""
    import sys
    from . import __version__
    from .schemas import SCHEMA_VERSION
    typer.echo(f"bounded-investigator version: {__version__}")
    typer.echo(f"schema_version: {SCHEMA_VERSION}")
    typer.echo(f"loaded_module: {Path(__file__).resolve()}")
    typer.echo(f"python: {sys.executable}")



async def _run_one(
    packet: Path,
    output: Path,
    config_path: Path,
    env_file: Path | None,
) -> None:
    from .workflow import BoundedInvestigationWorkflow
    config = load_config(config_path, env_file)
    configure_logging(config.logging)
    log = get_logger("cli")
    name, text = read_packet(packet)
    log.info("command=run packet=%s input=%s output=%s", name, packet, output)
    workflow = BoundedInvestigationWorkflow(config)
    result = await workflow.run(
        packet_name=name,
        packet_text=text,
        artifact_dir=output.parent / f"{output.stem}.artifacts",
    )
    write_result(output, result)
    log.info("command=run wrote=%s", output)


@app.command()
def run(
    packet: Path = typer.Argument(..., exists=True, dir_okay=False),
    output: Path = typer.Option(Path("result.json"), "--output", "-o"),
    config: Path = typer.Option(Path("config.yaml"), "--config", "-c", exists=True),
    env_file: Path | None = typer.Option(None, "--env-file"),
) -> None:
    """Analyze one bounded material packet without DAG dependencies."""
    asyncio.run(_run_one(packet, output, config, env_file))


async def _run_batch_independent(
    *,
    packets: list[Path],
    output_dir: Path,
    config,
    log,
) -> tuple[dict[str, RunEnvelope], dict[str, str]]:
    from .workflow import BoundedInvestigationWorkflow
    semaphore = asyncio.Semaphore(config.batch.concurrency)
    completed_results: dict[str, RunEnvelope] = {}
    failures: dict[str, str] = {}
    progress_lock = asyncio.Lock()
    completed = 0
    failed = 0
    total = len(packets)
    batch_started = time.monotonic()

    async def worker(packet: Path) -> None:
        nonlocal completed, failed
        async with semaphore:
            name, text = read_packet(packet)
            out = output_dir / f"{packet.stem}.json"
            log.info("batch=worker_start packet=%s file=%s chars=%s mode=independent", name, packet.name, len(text))
            ok = False
            try:
                workflow = BoundedInvestigationWorkflow(config)
                result = await workflow.run(
                    packet_name=name,
                    packet_text=text,
                    artifact_dir=output_dir / "_artifacts" / packet.stem,
                )
                write_result(out, result)
                completed_results[name] = result
                ok = True
                log.info("batch=worker_done packet=%s output=%s", name, out.name)
            except Exception as exc:
                failures[name] = f"{type(exc).__name__}: {exc}"
                err = output_dir / f"{packet.stem}.error.json"
                write_error(err, name, exc)
                log.error(
                    "batch=worker_failed packet=%s error=%s message=%s error_file=%s",
                    name, type(exc).__name__, exc, err.name,
                )
            finally:
                async with progress_lock:
                    completed += 1
                    if not ok:
                        failed += 1
                    log.info(
                        "batch=progress completed=%s/%s succeeded=%s failed=%s elapsed=%.1fs",
                        completed, total, completed - failed, failed, time.monotonic() - batch_started,
                    )

    await asyncio.gather(*(worker(p) for p in packets))
    return completed_results, failures


def _save_final_outputs(
    output_dir: Path,
    global_envelope: GlobalReconciliationEnvelope,
    *,
    evidence_scope: EvidenceScope | None = None,
    packet_coverage: list[PacketCoverage] | None = None,
    packet_source_dir: Path | None = None,
) -> dict[str, object]:
    """v1.7: project validated knowledge to three report levels, without an LLM."""
    for stale in ("_report_ready.json", "_final_report.md", "_final_assessment.json", "_knowledge_audit.json", "_evidence_reference_audit.json"):
        file = output_dir / stale
        if file.exists():
            file.unlink()
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
            len(value) if isinstance(value, list) else int(bool(value))
            for value in report_data.get("code_excerpts", {}).values()
        ),
        "code_excerpt_claim_count": len(report_data.get("code_excerpts", {})),
    })
    return summary


async def _run_batch_dag(
    *,
    packets: list[Path],
    output_dir: Path,
    manifest_path: Path,
    config,
    log,
    evidence_scope: EvidenceScope | None = None,
    additional_input_dir: Path | None = None,
) -> tuple[dict[str, RunEnvelope], dict[str, str]]:
    from .workflow import BoundedInvestigationWorkflow
    manifest = load_manifest(manifest_path)
    jobs = manifest.by_id()
    packet_map = {packet.stem: packet for packet in packets}
    missing_packets = sorted(job_id for job_id in jobs if job_id not in packet_map)
    if missing_packets:
        raise RuntimeError(f"manifest jobs missing packet files: {missing_packets}")
    # Record what was actually supplied; this contains no sample-specific
    # missing-material assumptions. File names/code ranges are indexed without
    # executing, decompressing or analyzing binaries.
    inventory = build_input_inventory(
        [packet_map[j] for j in sorted(jobs)], additional_root=additional_input_dir
    )
    (output_dir / "_evidence_inventory.json").write_text(
        dump_inventory_json(inventory), encoding="utf-8"
    )
    extras = sorted(set(packet_map) - set(jobs))
    if extras:
        log.warning("batch=dag ignoring_packets_not_in_manifest packets=%s", extras)

    semaphore = asyncio.Semaphore(config.batch.concurrency)
    completed_results: dict[str, RunEnvelope] = {}
    failures: dict[str, str] = {}
    pending = set(jobs)
    running: dict[asyncio.Task, str] = {}
    total = len(jobs)
    done_count = 0
    batch_started = time.monotonic()

    log.info(
        "batch=dag_start jobs=%s concurrency=%s manifest=%s purpose=%s",
        total,
        config.batch.concurrency,
        manifest_path,
        manifest.purpose,
    )

    async def run_job(job_id: str) -> RunEnvelope:
        job = jobs[job_id]
        packet = packet_map[job_id]
        async with semaphore:
            name, text = read_packet(packet)
            dependency_context = render_dependency_handoffs(
                job.depends_on, completed_results, failures
            )
            out = output_dir / f"{job_id}.json"
            log.info(
                "batch=dag_worker_start packet=%s title=%s dependencies=%s dependency_context_chars=%s chars=%s",
                name,
                job.title,
                job.depends_on,
                len(dependency_context),
                len(text),
            )
            workflow = BoundedInvestigationWorkflow(config)
            result = await workflow.run(
                packet_name=name,
                packet_text=text,
                dependency_context=dependency_context,
                dependency_ids=job.depends_on,
                artifact_dir=output_dir / "_artifacts" / job_id,
            )
            write_result(out, result)
            log.info("batch=dag_worker_done packet=%s output=%s", name, out.name)
            return result

    while pending or running:
        ready = sorted(
            job_id
            for job_id in pending
            if all(dep in completed_results or dep in failures for dep in jobs[job_id].depends_on)
        )
        for job_id in ready:
            pending.remove(job_id)
            task = asyncio.create_task(run_job(job_id))
            running[task] = job_id

        if not running:
            # Manifest validation already checks cycles; this protects against impossible runtime state.
            raise RuntimeError(f"DAG scheduler stalled with pending jobs: {sorted(pending)}")

        finished, _ = await asyncio.wait(running.keys(), return_when=asyncio.FIRST_COMPLETED)
        for task in finished:
            job_id = running.pop(task)
            try:
                completed_results[job_id] = task.result()
            except Exception as exc:
                failures[job_id] = f"{type(exc).__name__}: {exc}"
                err = output_dir / f"{job_id}.error.json"
                write_error(err, job_id, exc)
                log.error(
                    "batch=dag_worker_failed packet=%s dependencies=%s error=%s message=%s error_file=%s",
                    job_id,
                    jobs[job_id].depends_on,
                    type(exc).__name__,
                    exc,
                    err.name,
                )
            done_count += 1
            log.info(
                "batch=dag_progress completed=%s/%s succeeded=%s failed=%s running=%s pending=%s elapsed=%.1fs",
                done_count,
                total,
                len(completed_results),
                len(failures),
                len(running),
                len(pending),
                time.monotonic() - batch_started,
            )

    automatic_scope, material_requests = build_automatic_scope(inventory, completed_results)
    final_scope = merge_expert_scope(automatic_scope, evidence_scope or EvidenceScope())
    (output_dir / "_evidence_scope.json").write_text(
        final_scope.model_dump_json(indent=2), encoding="utf-8"
    )
    (output_dir / "_material_requests.json").write_text(
        json.dumps([r.model_dump(mode="json") for r in material_requests], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    log.info(
        "batch=material_inventory indexed_functions=%s additional_files=%s requests=%s "
        "resolved_materials=%s scan_complete=%s",
        final_scope.inventory_summary.get("indexed_function_count", 0),
        len(inventory.additional_files), len(material_requests),
        len(final_scope.materials), inventory.scan_complete,
    )

    global_summary: dict[str, object] = {"enabled": config.global_reconciliation.enabled}
    if config.global_reconciliation.enabled:
        global_started = time.monotonic()
        try:
            global_workflow = BoundedInvestigationWorkflow(config)
            global_result = await global_workflow.run_global_reconciliation(
                completed_results=completed_results,
                failures=failures,
                artifact_dir=output_dir / "_global_artifacts",
                evidence_scope=final_scope,
                packet_coverage=packet_coverage_from_results(completed_results, failures),
            )
            report_summary = _save_final_outputs(
                output_dir, global_result, evidence_scope=final_scope,
                packet_coverage=packet_coverage_from_results(completed_results, failures),
                packet_source_dir=packets[0].parent if packets else None,
            )
            global_path = output_dir / "_global_reconciliation.json"
            global_path.write_text(global_result.model_dump_json(indent=2), encoding="utf-8")
            global_summary = {
                "enabled": True,
                "status": global_result.delivery_status.value,
                "result_status": (global_result.result.status.value if global_result.result else None),
                "output": global_path.name,
                "elapsed_seconds": global_result.elapsed_seconds,
                "provenance_valid": global_result.provenance_valid,
                "source_claim_count": global_result.source_claim_count,
                "source_artifact_count": global_result.source_artifact_count,
                "reduced_claim_count": global_result.reduced_claim_count,
                "reduced_artifact_count": global_result.reduced_artifact_count,
                "artifact_conflict_count": global_result.artifact_conflict_count,
                "historical_gap_count": global_result.source_gap_count,
                "knowledge_report": report_summary,
                "usage": global_result.usage.model_dump(),
            }
            log.info(
                "batch=global_reconciliation_done status=%s result_status=%s provenance_valid=%s output=%s elapsed=%.1fs",
                global_result.delivery_status.value,
                global_result.result.status.value if global_result.result else "none",
                global_result.provenance_valid,
                global_path.name,
                time.monotonic() - global_started,
            )
        except Exception as exc:
            for stale in ("_report_ready.json", "_final_report.md", "_final_assessment.json", "_knowledge_audit.json", "_evidence_reference_audit.json"):
                path = output_dir / stale
                if path.exists():
                    path.unlink()
            stale_global = output_dir / "_global_reconciliation.json"
            if stale_global.exists():
                stale_global.unlink()
            err = output_dir / "_global_reconciliation.error.json"
            write_error(err, "GLOBAL", exc)
            global_summary = {
                "enabled": True,
                "status": "failed",
                "error": f"{type(exc).__name__}: {exc}",
                "error_file": err.name,
                "elapsed_seconds": round(time.monotonic() - global_started, 3),
            }
            log.error(
                "batch=global_reconciliation_failed error=%s message=%s error_file=%s",
                type(exc).__name__, exc, err.name,
            )

    summary = {
        "manifest": str(manifest_path),
        "purpose": manifest.purpose,
        "elapsed_seconds": round(time.monotonic() - batch_started, 3),
        "succeeded": sorted(completed_results),
        "failed": failures,
        "jobs": {
            job_id: {
                "title": job.title,
                "depends_on": job.depends_on,
                "status": (
                    completed_results[job_id].delivery_status.value
                    if job_id in completed_results
                    else "failed"
                ),
            }
            for job_id, job in jobs.items()
        },
        "global_reconciliation": global_summary,
        "evidence_inventory": "_evidence_inventory.json",
        "evidence_scope": "_evidence_scope.json",
        "material_requests": "_material_requests.json",
    }
    (output_dir / "_dag_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return completed_results, failures


async def _run_batch(
    input_dir: Path,
    output_dir: Path,
    config_path: Path,
    env_file: Path | None,
    pattern: str,
    manifest_path: Path | None,
    scope_file: Path | None = None,
    additional_input_dir: Path | None = None,
) -> None:
    config = load_config(config_path, env_file)
    configure_logging(config.logging)
    log = get_logger("batch")
    packets = sorted(input_dir.glob(pattern))
    output_dir.mkdir(parents=True, exist_ok=True)
    batch_started = time.monotonic()

    discovered_manifest = discover_manifest(input_dir, manifest_path)
    if (scope_file or additional_input_dir) and not discovered_manifest:
        raise ValueError("--scope-file/--evidence-dir require DAG mode (provide a manifest)")
    # Manual expert metadata is optional; automatic material inspection is the default.
    evidence_scope = load_evidence_scope(scope_file)
    mode = "dag" if discovered_manifest else "independent"
    log.info(
        "batch=start packets=%s concurrency=%s input_dir=%s output_dir=%s pattern=%s mode=%s manifest=%s",
        len(packets),
        config.batch.concurrency,
        input_dir,
        output_dir,
        pattern,
        mode,
        discovered_manifest,
    )

    if discovered_manifest:
        completed, failures = await _run_batch_dag(
            packets=packets,
            output_dir=output_dir,
            manifest_path=discovered_manifest,
            config=config,
            log=log,
            evidence_scope=evidence_scope,
            additional_input_dir=additional_input_dir,
        )
    else:
        log.warning("batch=dag_disabled reason=no_manifest_found")
        completed, failures = await _run_batch_independent(
            packets=packets,
            output_dir=output_dir,
            config=config,
            log=log,
        )

    log.info(
        "batch=complete mode=%s packets=%s succeeded=%s failed=%s elapsed=%.1fs",
        mode,
        len(completed) + len(failures),
        len(completed),
        len(failures),
        time.monotonic() - batch_started,
    )


@app.command()
def batch(
    input_dir: Path = typer.Argument(..., exists=True, file_okay=False),
    output_dir: Path = typer.Option(Path("results"), "--output-dir", "-o"),
    config: Path = typer.Option(Path("config.yaml"), "--config", "-c", exists=True),
    env_file: Path | None = typer.Option(None, "--env-file"),
    pattern: str = typer.Option("J*.md", "--pattern"),
    manifest: Path | None = typer.Option(
        None,
        "--manifest",
        help="DAG manifest JSON. If omitted, auto-detect manifest.json beside input_dir.",
    ),
    scope_file: Path | None = typer.Option(
        None, "--scope-file", help="Optional expert-only material attestation (never required).",
    ),
    evidence_dir: Path | None = typer.Option(
        None, "--evidence-dir", help="Optional directory containing other actual source/export files to index.",
    ),
) -> None:
    """Analyze packets; DAG mode automatically indexes supplied input and material needs."""
    asyncio.run(_run_batch(input_dir, output_dir, config, env_file, pattern, manifest, scope_file, evidence_dir))


@app.command("report")
@app.command("assess")
def assess(
    global_result: Path = typer.Argument(..., exists=True, dir_okay=False,
        help="Existing _global_reconciliation.json; no Worker/DAG re-run"),
    output_dir: Path = typer.Option(Path("assessed_results"), "--output-dir", "-o"),
    config: Path = typer.Option(Path("config.yaml"), "--config", "-c", exists=True),
    env_file: Path | None = typer.Option(None, "--env-file"),
    scope_file: Path | None = typer.Option(
        None, "--scope-file", help="Optional expert-only attestation; defaults to saved automatic scope.",
    ),
    packets_dir: Path | None = typer.Option(
        None, "--packets-dir", help="Optional current-run packet directory to rebuild evidence index if no saved scope exists.",
    ),
    evidence_dir: Path | None = typer.Option(
        None, "--evidence-dir", help="Optional actual nonpacket source files to include when rebuilding index.",
    ),
    packet_results_dir: Path | None = typer.Option(
        None, "--packet-results-dir", help="Folder containing saved Jxx.json envelopes for delivery coverage.",
    ),
) -> None:
    """Produce a three-level evidence report from canonical facts, with no model calls."""
    async def _do() -> None:
        settings = load_config(config, env_file)
        configure_logging(settings.logging)
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
        summary = _save_final_outputs(
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

    asyncio.run(_do())


def _load_saved_packet_results(folder: Path) -> tuple[dict[str, RunEnvelope], dict[str, str]]:
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


async def _reconcile_saved_results(
    *,
    packet_results_dir: Path,
    output_dir: Path,
    config,
    scope_file: Path | None = None,
    packets_dir: Path | None = None,
    evidence_dir: Path | None = None,
) -> dict[str, object]:
    """Re-run global+final stages from immutable saved Worker results only."""
    from .workflow import BoundedInvestigationWorkflow
    if not config.global_reconciliation.enabled:
        raise ValueError("Set global_reconciliation.enabled=true in config")
    if evidence_dir is not None and packets_dir is None:
        raise ValueError("--evidence-dir requires --packets-dir")
    if packet_results_dir.resolve() == output_dir.resolve():
        raise ValueError("Replay output directory must differ from saved Worker results directory")
    log = get_logger("reconcile")
    completed, failures = _load_saved_packet_results(packet_results_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    # A failed new replay must never leave an older successful report looking current.
    for stale in (
        "_global_reconciliation.json", "_global_reconciliation.error.json", "_reconcile_summary.json",
        "_report_ready.json", "_final_report.md", "_final_assessment.json", "_knowledge_audit.json",
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
            evidence_scope=scope,
            packet_coverage=packet_coverage_from_results(completed, failures),
        )
        path = output_dir / "_global_reconciliation.json"
        path.write_text(global_result.model_dump_json(indent=2), encoding="utf-8")
        final_summary = _save_final_outputs(
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
        write_error(output_dir / "_global_reconciliation.error.json", "GLOBAL", exc)
        log.error("command=reconcile failed error=%s: %s", type(exc).__name__, exc)
        raise


@app.command("reconcile")
def reconcile(
    packet_results_dir: Path = typer.Argument(..., exists=True, file_okay=False,
        help="Directory containing saved Jxx.json results; does not rerun Workers"),
    output_dir: Path = typer.Option(Path("replayed_results"), "--output-dir", "-o"),
    config: Path = typer.Option(Path("config.yaml"), "--config", "-c", exists=True),
    env_file: Path | None = typer.Option(None, "--env-file"),
    packets_dir: Path | None = typer.Option(None, "--packets-dir",
        help="Optional packet folder to rebuild the material inventory without LLM calls"),
    evidence_dir: Path | None = typer.Option(None, "--evidence-dir",
        help="Optional additional actual input files for material inventory"),
    scope_file: Path | None = typer.Option(None, "--scope-file",
        help="Optional expert attestation only; never required for automatic mode"),
) -> None:
    """Reconcile saved Worker results and regenerate global/final outputs."""
    settings = load_config(config, env_file)
    configure_logging(settings.logging)
    asyncio.run(_reconcile_saved_results(
        packet_results_dir=packet_results_dir,
        output_dir=output_dir,
        config=settings,
        packets_dir=packets_dir,
        evidence_dir=evidence_dir,
        scope_file=scope_file,
    ))
