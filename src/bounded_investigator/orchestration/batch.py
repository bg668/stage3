"""Run individual packets and schedule independent or dependent batches."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

from ..config import AppConfig
from ..harness import ResolvedHarness, load_harness
from ..io import read_packet, write_error, write_result
from ..logging_utils import get_logger
from ..schemas import GlobalReconciliationEnvelope, RunEnvelope
from ..evidence.scope import (
    EvidenceScope, load_evidence_scope, packet_coverage_from_results,
)
from ..evidence.inventory import (
    build_input_inventory,
    build_automatic_scope,
    dump_inventory_json,
    merge_expert_scope,
)
from ..reporting.service import clear_report_outputs, save_final_outputs
from ..reporting.html_report import clear_html_outputs, create_html_report
from .dag import discover_manifest, load_manifest, render_dependency_handoffs


async def run_one(
    packet: Path,
    output: Path,
    config: AppConfig,
) -> None:
    from ..workflow import BoundedInvestigationWorkflow
    log = get_logger("cli")
    name, text = read_packet(packet)
    log.info("command=run packet=%s input=%s output=%s", name, packet, output)
    harness = load_harness(config.harness_dir)
    workflow = BoundedInvestigationWorkflow(config, harness=harness)
    result = await workflow.run(
        packet_name=name,
        packet_text=text,
        artifact_dir=output.parent / f"{output.stem}.artifacts",
    )
    write_result(output, result)
    log.info("command=run wrote=%s", output)


async def run_batch_independent(
    *,
    packets: list[Path],
    output_dir: Path,
    config: AppConfig,
    log: logging.Logger,
    harness: ResolvedHarness | None = None,
) -> tuple[dict[str, RunEnvelope], dict[str, str]]:
    from ..workflow import BoundedInvestigationWorkflow
    harness = harness or load_harness(config.harness_dir)
    harness.save(output_dir)
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
                workflow = BoundedInvestigationWorkflow(config, harness=harness)
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


async def run_batch_dag(
    *,
    packets: list[Path],
    output_dir: Path,
    manifest_path: Path,
    config: AppConfig,
    log: logging.Logger,
    evidence_scope: EvidenceScope | None = None,
    additional_input_dir: Path | None = None,
    harness: ResolvedHarness | None = None,
) -> tuple[dict[str, RunEnvelope], dict[str, str]]:
    from ..workflow import BoundedInvestigationWorkflow
    harness = harness or load_harness(config.harness_dir)
    harness.save(output_dir)
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
            workflow = BoundedInvestigationWorkflow(config, harness=harness)
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
            global_workflow = BoundedInvestigationWorkflow(config, harness=harness)
            global_result = await global_workflow.run_global_reconciliation(
                completed_results=completed_results,
                failures=failures,
                artifact_dir=output_dir / "_global_artifacts",
            )
            report_summary = save_final_outputs(
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
            clear_report_outputs(output_dir)
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


async def run_batch(
    input_dir: Path,
    output_dir: Path,
    config: AppConfig,
    pattern: str,
    manifest_path: Path | None,
    scope_file: Path | None = None,
    additional_input_dir: Path | None = None,
    *,
    generate_html: bool = True,
    context_file: Path | None = None,
) -> Path | None:
    harness = load_harness(config.harness_dir)
    harness.save(output_dir)
    log = get_logger("batch")
    packets = sorted(input_dir.glob(pattern))
    output_dir.mkdir(parents=True, exist_ok=True)
    batch_started = time.monotonic()

    discovered_manifest = discover_manifest(input_dir, manifest_path)
    if (scope_file or additional_input_dir) and not discovered_manifest:
        raise ValueError("--scope-file/--evidence-dir require DAG mode (provide a manifest)")
    if generate_html and discovered_manifest and config.global_reconciliation.enabled \
            and 'report_composition' not in config.stages:
        raise ValueError('Missing stages.report_composition for batch HTML; configure it or use --no-html-report')
    html_dir = output_dir / 'html_report'
    if html_dir.resolve() in {input_dir.resolve(), output_dir.resolve()}:
        raise ValueError('HTML output directory must differ from input result/material directories')
    if additional_input_dir is not None and html_dir.resolve().is_relative_to(additional_input_dir.resolve()):
        raise ValueError('HTML output directory must not overlap the additional input directory')
    clear_html_outputs(html_dir, protected_inputs=[
        path for path in [context_file, scope_file, discovered_manifest, *packets] if path is not None
    ])
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
        completed, failures = await run_batch_dag(
            packets=packets,
            output_dir=output_dir,
            manifest_path=discovered_manifest,
            config=config,
            harness=harness,
            log=log,
            evidence_scope=evidence_scope,
            additional_input_dir=additional_input_dir,
        )
    else:
        log.warning("batch=dag_disabled reason=no_manifest_found")
        completed, failures = await run_batch_independent(
            packets=packets,
            output_dir=output_dir,
            config=config,
            harness=harness,
            log=log,
        )

    summary_path = output_dir / '_dag_summary.json'
    summary = json.loads(summary_path.read_text(encoding='utf-8')) if discovered_manifest else None
    html_status: dict[str, object] = {'status': 'skipped'}
    html_error: Exception | None = None
    html_path = None
    if not generate_html:
        html_status['reason'] = 'disabled_by_option'
    elif not discovered_manifest:
        html_status['reason'] = 'no_manifest'
    elif not config.global_reconciliation.enabled:
        html_status['reason'] = 'global_reconciliation_disabled'
    else:
        global_path = output_dir / '_global_reconciliation.json'
        canonical = GlobalReconciliationEnvelope.model_validate_json(global_path.read_text(encoding='utf-8')) \
            if global_path.is_file() else None
        if canonical is None or canonical.result is None or not canonical.provenance_valid:
            html_status['reason'] = 'no_valid_global_result'
        else:
            log.info('batch=html_report_start input=%s output_dir=%s', global_path, html_dir)
            try:
                report = await create_html_report(global_result=global_path, output_dir=html_dir,
                    config=config, context_file=context_file,
                    packet_paths=[path for path in packets if path.stem in summary['jobs']],
                    packet_results_dir=output_dir)
                html_path = html_dir / '_final_report.html'
                html_status = {'status': report['status'], 'output': html_path.relative_to(output_dir).as_posix(),
                    'manifest': (html_dir / '_html_report_manifest.json').relative_to(output_dir).as_posix(),
                    'usage': report['usage']}
                log.info('batch=html_report_done output=%s requests=%s', html_path,
                         report['usage']['total']['requests'])
            except Exception as exc:
                html_error = exc
                html_status = {'status': 'failed', 'error': f'{type(exc).__name__}: {exc}',
                    'manifest': (html_dir / '_html_report_manifest.json').relative_to(output_dir).as_posix()}
                log.error('batch=html_report_failed analysis_preserved=true error=%s message=%s',
                          type(exc).__name__, exc)
    if html_status['status'] == 'skipped':
        log.info('batch=html_report_skipped reason=%s', html_status['reason'])
    if summary is not None:
        summary['html_report'] = html_status
        summary['elapsed_seconds'] = round(time.monotonic() - batch_started, 3)
        summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    if html_error is not None:
        raise RuntimeError(f'Batch analysis was saved; HTML report failed: {html_error}') from html_error

    log.info(
        "batch=complete mode=%s packets=%s succeeded=%s failed=%s elapsed=%.1fs",
        mode,
        len(completed) + len(failures),
        len(completed),
        len(failures),
        time.monotonic() - batch_started,
    )
    return html_path
