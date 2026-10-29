from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any, cast

from pydantic_ai.usage import RunUsage, UsageLimits

from .config import AppConfig
from .harness import ResolvedHarness, load_harness
from .execution.stage_runner import RepairRuntime, StageRunner
from .execution.artifacts import save_model, save_sidecar_json
from .execution.llm import StageAgent, build_stage_agent, build_text_stage_agent
from .investigation.review_guard import constrain_reviewer_scope
from .investigation.revision import merge_scoped_revision, validate_revision_scope
from .logging_utils import get_logger
from .prompts import (
    analyzer_instructions,
    analyzer_prompt,
    format_repair_instructions,
    global_reconciliation_instructions,
    global_reconciliation_prompt,
    reviewer_instructions,
    reviewer_prompt,
    reviser_instructions,
    reviser_prompt,
)
from .reconciliation import (
    assemble_reconciliation_result,
    project_reconciliation_input,
    projection_audit_dict,
    validate_and_finalize_reconciliation,
)
from .evidence.validation import validate_investigation_result, validation_prompt_context
from .evidence.snippets import build_code_index_from_text
from .schemas import (
    CoverageReview,
    DeliveryStatus,
    FormatRepairStats,
    GlobalGap,
    GlobalReconciliationDecisionResult,
    GlobalReconciliationEnvelope,
    InvestigationResult,
    PartialInvestigationResult,
    ReconciliationStatus,
    RunEnvelope,
    UsageSummary,
)
from .execution.usage_utils import merge_usage_summaries, usage_delta, usage_summary


class BoundedInvestigationWorkflow:
    """Bounded investigation with clean-context format repair and graceful result preservation."""

    def __init__(self, config: AppConfig, *, harness: ResolvedHarness | None = None):
        self.harness = harness or load_harness(config.harness_dir)
        self.config = config
        self.log = get_logger("workflow")
        self.analyzer = build_stage_agent(
            config=config, stage="analyzer", schema=InvestigationResult, instructions=analyzer_instructions(self.harness)
        )
        self.reviewer = build_stage_agent(
            config=config, stage="reviewer", schema=CoverageReview, instructions=reviewer_instructions(self.harness)
        )
        self.reviser = build_stage_agent(
            config=config, stage="reviser", schema=InvestigationResult, instructions=reviser_instructions(self.harness)
        )
        self.format_repair = None
        if config.format_repair.enabled:
            self.format_repair = build_text_stage_agent(
                config=config,
                stage="format_repair",
                instructions=format_repair_instructions(),
            )
        self.global_reconciliation = None
        if config.global_reconciliation.enabled:
            self.global_reconciliation = build_stage_agent(
                config=config,
                stage="global_reconciliation",
                schema=GlobalReconciliationDecisionResult,
                instructions=global_reconciliation_instructions(),
            )
        self.stage_runner = StageRunner(config=config, format_repair=self.format_repair, log=self.log)

    def _limits(self) -> UsageLimits:
        return UsageLimits(
            request_limit=self.config.workflow.max_model_requests,
            total_tokens_limit=self.config.workflow.total_tokens_limit,
        )

    def _global_limits(self) -> UsageLimits:
        cfg = self.config.global_reconciliation
        return UsageLimits(
            request_limit=cfg.max_model_requests,
            total_tokens_limit=cfg.total_tokens_limit,
        )

    def _finish_stage_usage(
        self,
        packet_name: str,
        stage: str,
        usage: RunUsage,
        before: UsageSummary,
        stage_started: float,
    ) -> UsageSummary:
        after = usage_summary(usage)
        delta = usage_delta(before, after)
        self.log.info(
            "packet=%s stage=%s done elapsed=%.1fs requests=%s input_tokens=%s output_tokens=%s reasoning_tokens=%s visible_output_tokens=%s total_tokens=%s",
            packet_name,
            stage,
            time.monotonic() - stage_started,
            delta.requests,
            delta.input_tokens,
            delta.output_tokens,
            delta.reasoning_tokens,
            delta.visible_output_tokens,
            delta.total_tokens,
        )
        return delta

    def _stage_models(self) -> dict[str, str]:
        agents = [self.analyzer, self.reviewer, self.reviser]
        if self.format_repair is not None:
            agents.append(self.format_repair)
        return {
            s.stage: f"{s.model_config.provider}:{s.model_config.model_name}"
            for s in agents
        }

    def _stage_log(self, packet_name: str, stage_agent: StageAgent, event: str) -> None:
        stage_cfg = self.config.stages[stage_agent.stage]
        self.log.info(
            "packet=%s stage=%s %s model_profile=%s provider=%s model=%s api=%s output_mode=%s reasoning=%s max_output_tokens=%s stream=%s",
            packet_name,
            stage_agent.stage,
            event,
            stage_agent.model_key,
            stage_agent.model_config.provider,
            stage_agent.model_config.model_name,
            stage_agent.model_config.api_mode,
            stage_agent.model_config.output_mode,
            stage_agent.model_config.reasoning,
            stage_cfg.max_output_tokens,
            stage_agent.model_config.stream_output,
        )

    def _build_envelope(
        self,
        *,
        packet_name: str,
        started: float,
        usage: RunUsage,
        repair_runtime: RepairRuntime,
        dependency_ids: list[str],
        dependency_context: str,
        revision_performed: bool,
        stage_usage: dict[str, UsageSummary],
        stage_statuses: dict[str, DeliveryStatus],
        raw_output_paths: dict[str, str],
        validation_errors: dict[str, list[dict[str, Any]]],
        delivery_status: DeliveryStatus,
        review_completed: bool,
        revision_failed: bool,
        review: CoverageReview | None,
        result: InvestigationResult | None,
        partial_result: PartialInvestigationResult | None = None,
    ) -> RunEnvelope:
        elapsed = time.monotonic() - started
        core_usage = usage_summary(usage)
        repair_usage = usage_summary(repair_runtime.usage)
        total_usage = merge_usage_summaries(core_usage, repair_usage)
        if repair_runtime.attempts:
            stage_usage["format_repair"] = repair_usage
        repair_stats = FormatRepairStats(
            triggered=repair_runtime.triggered,
            successful=repair_runtime.successful,
            failed=repair_runtime.failed,
            attempts=repair_runtime.attempts,
            stage_attempts=repair_runtime.stage_attempts,
            usage=repair_usage,
        )
        return RunEnvelope(
            packet_name=packet_name,
            elapsed_seconds=round(elapsed, 3),
            revision_performed=revision_performed,
            delivery_status=delivery_status,
            stage_models=self._stage_models(),
            stage_statuses=stage_statuses,
            stage_usage=stage_usage,
            format_repair=repair_stats,
            usage=total_usage,
            dependency_ids=dependency_ids,
            dependency_context_chars=len(dependency_context),
            review_completed=review_completed,
            revision_failed=revision_failed,
            review=review,
            result=result,
            partial_result=partial_result,
            raw_output_paths=raw_output_paths,
            validation_errors=validation_errors,
        )

    async def run(
        self,
        *,
        packet_name: str,
        packet_text: str,
        dependency_context: str = "none",
        dependency_ids: list[str] | None = None,
        artifact_dir: str | Path | None = None,
    ) -> RunEnvelope:
        started = time.monotonic()
        usage = RunUsage()
        repair_runtime = RepairRuntime()
        limits = self._limits()
        cfg = self.config.workflow
        dependency_ids = list(dependency_ids or [])
        artifact_dir = Path(artifact_dir) if artifact_dir is not None else None
        if artifact_dir is not None:
            artifact_dir.mkdir(parents=True, exist_ok=True)
            self.harness.save(artifact_dir)
        stage_usage: dict[str, UsageSummary] = {}
        stage_statuses: dict[str, DeliveryStatus] = {}
        raw_output_paths: dict[str, str] = {}
        validation_errors: dict[str, list[dict[str, Any]]] = {}
        packet_source_index = build_code_index_from_text(packet_text, f"{packet_name}.md")

        self.log.info(
            "packet=%s start chars=%s dependencies=%s dependency_context_chars=%s request_budget=%s token_budget=%s wall_clock=%ss stage_models=%s format_repair_enabled=%s repair_attempts=%s",
            packet_name,
            len(packet_text),
            dependency_ids,
            len(dependency_context),
            cfg.max_model_requests,
            cfg.total_tokens_limit,
            cfg.wall_clock_seconds,
            self._stage_models(),
            self.config.format_repair.enabled,
            self.config.format_repair.max_attempts if self.config.format_repair.enabled else 0,
        )

        try:
            async with asyncio.timeout(cfg.wall_clock_seconds):
                # Analyzer
                stage_started = time.monotonic()
                before = usage_summary(usage)
                self._stage_log(packet_name, self.analyzer, "start")
                analyzer_outcome = await self.stage_runner.run(
                    stage_agent=self.analyzer,
                    prompt=analyzer_prompt(packet_name, packet_text, dependency_context),
                    packet_name=packet_name,
                    usage=usage,
                    limits=limits,
                    repair_runtime=repair_runtime,
                    artifact_dir=artifact_dir,
                )
                stage_usage["analyzer"] = self._finish_stage_usage(
                    packet_name, "analyzer", usage, before, stage_started
                )
                stage_statuses["analyzer"] = analyzer_outcome.status
                if analyzer_outcome.raw_output_path:
                    raw_output_paths["analyzer"] = analyzer_outcome.raw_output_path
                if analyzer_outcome.validation_errors:
                    validation_errors["analyzer"] = analyzer_outcome.validation_errors

                if analyzer_outcome.output is None:
                    if analyzer_outcome.partial is not None:
                        partial_evidence_validation = validate_investigation_result(
                            analyzer_outcome.partial, index=packet_source_index, scope="current_packet"
                        )
                        save_sidecar_json(
                            artifact_dir, "evidence_validation", "partial.json", partial_evidence_validation
                        )
                    envelope = self._build_envelope(
                        packet_name=packet_name,
                        started=started,
                        usage=usage,
                        repair_runtime=repair_runtime,
                        dependency_ids=dependency_ids,
                        dependency_context=dependency_context,
                        revision_performed=False,
                        stage_usage=stage_usage,
                        stage_statuses=stage_statuses,
                        raw_output_paths=raw_output_paths,
                        validation_errors=validation_errors,
                        delivery_status=analyzer_outcome.status,
                        review_completed=False,
                        revision_failed=False,
                        review=None,
                        result=None,
                        partial_result=analyzer_outcome.partial,
                    )
                    self.log.warning(
                        "packet=%s complete_degraded delivery_status=%s partial_claims=%s partial_artifacts=%s",
                        packet_name,
                        envelope.delivery_status.value,
                        len(envelope.partial_result.claims) if envelope.partial_result else 0,
                        len(envelope.partial_result.artifacts) if envelope.partial_result else 0,
                    )
                    return envelope

                draft = cast(InvestigationResult, analyzer_outcome.output)
                self.log.info(
                    "packet=%s stage=analyzer result status=%s claims=%s artifacts=%s gaps=%s handoffs=%s",
                    packet_name, draft.status.value, len(draft.claims), len(draft.artifacts), len(draft.gaps), len(draft.handoffs),
                )
                analyzer_evidence_validation = validate_investigation_result(
                    draft, index=packet_source_index, scope="current_packet"
                )
                save_sidecar_json(
                    artifact_dir, "evidence_validation", "analyzer.json", analyzer_evidence_validation
                )
                analyzer_validation_context = validation_prompt_context(analyzer_evidence_validation)
                self.log.info(
                    "packet=%s stage=evidence_validation analyzer_valid=%s deferred_cross_packet=%s invalid=%s",
                    packet_name, analyzer_evidence_validation["valid_segments"],
                    analyzer_evidence_validation["not_in_current_packet"],
                    analyzer_evidence_validation["invalid_segments"],
                )

                # Reviewer. If reviewer formatting fails, preserve the already validated analyzer result.
                stage_started = time.monotonic()
                before = usage_summary(usage)
                self._stage_log(packet_name, self.reviewer, "start")
                reviewer_outcome = await self.stage_runner.run(
                    stage_agent=self.reviewer,
                    prompt=reviewer_prompt(
                        packet_name, packet_text, draft, dependency_context, analyzer_validation_context
                    ),
                    packet_name=packet_name,
                    usage=usage,
                    limits=limits,
                    repair_runtime=repair_runtime,
                    artifact_dir=artifact_dir,
                )
                stage_usage["reviewer"] = self._finish_stage_usage(
                    packet_name, "reviewer", usage, before, stage_started
                )
                stage_statuses["reviewer"] = reviewer_outcome.status
                if reviewer_outcome.raw_output_path:
                    raw_output_paths["reviewer"] = reviewer_outcome.raw_output_path
                if reviewer_outcome.validation_errors:
                    validation_errors["reviewer"] = reviewer_outcome.validation_errors

                if reviewer_outcome.output is None:
                    delivery = (
                        DeliveryStatus.SUCCESS_AFTER_REPAIR
                        if analyzer_outcome.status == DeliveryStatus.SUCCESS_AFTER_REPAIR
                        else DeliveryStatus.SUCCESS
                    )
                    envelope = self._build_envelope(
                        packet_name=packet_name,
                        started=started,
                        usage=usage,
                        repair_runtime=repair_runtime,
                        dependency_ids=dependency_ids,
                        dependency_context=dependency_context,
                        revision_performed=False,
                        stage_usage=stage_usage,
                        stage_statuses=stage_statuses,
                        raw_output_paths=raw_output_paths,
                        validation_errors=validation_errors,
                        delivery_status=delivery,
                        review_completed=False,
                        revision_failed=False,
                        review=None,
                        result=draft,
                    )
                    save_sidecar_json(
                        artifact_dir, "evidence_validation", "final.json", analyzer_evidence_validation
                    )
                    self.log.warning(
                        "packet=%s reviewer_degraded preserved_analyzer_result delivery_status=%s",
                        packet_name, envelope.delivery_status.value,
                    )
                    return envelope

                original_review = cast(CoverageReview, reviewer_outcome.output)
                review = constrain_reviewer_scope(original_review, packet_text)
                if review is not original_review:
                    save_model(artifact_dir, "reviewer", "scope_guarded_result.json", review)
                    self.log.info(
                        "packet=%s stage=reviewer scope_guard demoted_issues=%s requires_revision=%s",
                        packet_name,
                        sum(a.severity != b.severity for a, b in zip(original_review.issues, review.issues)),
                        review.requires_revision,
                    )
                major_issues = sum(
                    1 for issue in review.issues
                    if issue.scope == "packet_internal" and issue.severity in {"critical", "major"}
                )
                self.log.info(
                    "packet=%s stage=reviewer result requires_revision=%s issues=%s major_or_critical=%s",
                    packet_name, review.requires_revision, len(review.issues), major_issues,
                )

                revision_performed = False
                revision_failed = False
                final = draft
                final_stage_status = analyzer_outcome.status

                if review.requires_revision and cfg.enable_revision:
                    scope_error = None
                    try:
                        validate_revision_scope(draft, review)
                    except ValueError as exc:
                        scope_error = str(exc)
                    if scope_error is None:
                        stage_started = time.monotonic()
                        before = usage_summary(usage)
                        self._stage_log(packet_name, self.reviser, "start")
                        reviser_outcome = await self.stage_runner.run(
                            stage_agent=self.reviser,
                            prompt=reviser_prompt(
                                packet_name, packet_text, draft, review, dependency_context,
                                analyzer_validation_context,
                            ),
                            packet_name=packet_name,
                            usage=usage,
                            limits=limits,
                            repair_runtime=repair_runtime,
                            artifact_dir=artifact_dir,
                        )
                        stage_usage["reviser"] = self._finish_stage_usage(
                            packet_name, "reviser", usage, before, stage_started
                        )
                        stage_statuses["reviser"] = reviser_outcome.status
                        if reviser_outcome.raw_output_path:
                            raw_output_paths["reviser"] = reviser_outcome.raw_output_path
                        if reviser_outcome.validation_errors:
                            validation_errors["reviser"] = reviser_outcome.validation_errors

                        if reviser_outcome.output is not None:
                            try:
                                applied = merge_scoped_revision(
                                    draft, cast(InvestigationResult, reviser_outcome.output), review,
                                    index=packet_source_index,
                                )
                            except ValueError as exc:
                                scope_error = str(exc)
                            else:
                                final = applied.result
                                revision_performed = True
                                final_stage_status = reviser_outcome.status
                                save_sidecar_json(artifact_dir, "reviser", "revision_scope.json", applied.audit)
                                save_model(artifact_dir, "reviser", "scope_applied_result.json", final)
                                self.log.info(
                                    "packet=%s stage=reviser result status=%s claims=%s artifacts=%s gaps=%s handoffs=%s ignored_changes=%s",
                                    packet_name, final.status.value, len(final.claims), len(final.artifacts), len(final.gaps), len(final.handoffs),
                                    len(applied.audit["ignored_paths"]),
                                )
                        else:
                            revision_failed = True
                            self.log.warning(
                                "packet=%s stage=reviser recovery=preserve_analyzer_result failed_status=%s",
                                packet_name, reviser_outcome.status.value,
                            )
                    if scope_error is not None:
                        revision_failed = True
                        stage_statuses["reviser"] = DeliveryStatus.EXECUTION_FAILED
                        validation_errors["revision_scope"] = [{"type": "revision_scope_error", "msg": scope_error}]
                        save_sidecar_json(
                            artifact_dir, "reviser", "revision_scope.json", {"passed": False, "error": scope_error}
                        )
                        self.log.warning(
                            "packet=%s stage=reviser recovery=preserve_analyzer_result scope_error=%s",
                            packet_name, scope_error,
                        )
                elif review.requires_revision:
                    self.log.warning("packet=%s stage=reviser skipped reason=revision_disabled", packet_name)
                else:
                    self.log.info("packet=%s stage=reviser skipped reason=no_major_internal_issue", packet_name)

                statuses_for_success = [analyzer_outcome.status, reviewer_outcome.status, final_stage_status]
                delivery = (
                    DeliveryStatus.SUCCESS_AFTER_REPAIR
                    if DeliveryStatus.SUCCESS_AFTER_REPAIR in statuses_for_success
                    else DeliveryStatus.SUCCESS
                )
                final_evidence_validation = validate_investigation_result(
                    final, index=packet_source_index, scope="current_packet"
                )
                save_sidecar_json(
                    artifact_dir, "evidence_validation", "final.json", final_evidence_validation
                )
                self.log.info(
                    "packet=%s stage=evidence_validation final_valid=%s deferred_cross_packet=%s invalid=%s",
                    packet_name, final_evidence_validation["valid_segments"],
                    final_evidence_validation["not_in_current_packet"],
                    final_evidence_validation["invalid_segments"],
                )
                envelope = self._build_envelope(
                    packet_name=packet_name,
                    started=started,
                    usage=usage,
                    repair_runtime=repair_runtime,
                    dependency_ids=dependency_ids,
                    dependency_context=dependency_context,
                    revision_performed=revision_performed,
                    stage_usage=stage_usage,
                    stage_statuses=stage_statuses,
                    raw_output_paths=raw_output_paths,
                    validation_errors=validation_errors,
                    delivery_status=delivery,
                    review_completed=True,
                    revision_failed=revision_failed,
                    review=review,
                    result=final,
                )

        except TimeoutError:
            elapsed = time.monotonic() - started
            core_u = usage_summary(usage)
            repair_u = usage_summary(repair_runtime.usage)
            total_u = merge_usage_summaries(core_u, repair_u)
            self.log.error(
                "packet=%s timeout elapsed=%.1fs requests=%s total_tokens=%s reasoning_tokens=%s repair_requests=%s limit=%ss",
                packet_name, elapsed, total_u.requests, total_u.total_tokens, total_u.reasoning_tokens,
                repair_u.requests, cfg.wall_clock_seconds,
            )
            raise
        except Exception:
            elapsed = time.monotonic() - started
            core_u = usage_summary(usage)
            repair_u = usage_summary(repair_runtime.usage)
            total_u = merge_usage_summaries(core_u, repair_u)
            self.log.exception(
                "packet=%s failed elapsed=%.1fs requests=%s total_tokens=%s reasoning_tokens=%s repair_requests=%s repair_attempts=%s",
                packet_name, elapsed, total_u.requests, total_u.total_tokens, total_u.reasoning_tokens,
                repair_u.requests, repair_runtime.attempts,
            )
            raise

        self.log.info(
            "packet=%s complete elapsed=%.1fs delivery_status=%s revision=%s revision_failed=%s requests=%s total_tokens=%s reasoning_tokens=%s repair_triggered=%s repair_attempts=%s final_status=%s claims=%s artifacts=%s",
            packet_name,
            envelope.elapsed_seconds,
            envelope.delivery_status.value,
            envelope.revision_performed,
            envelope.revision_failed,
            envelope.usage.requests,
            envelope.usage.total_tokens,
            envelope.usage.reasoning_tokens,
            envelope.format_repair.triggered,
            envelope.format_repair.attempts,
            envelope.result.status.value if envelope.result else "none",
            len(envelope.result.claims) if envelope.result else 0,
            len(envelope.result.artifacts) if envelope.result else 0,
        )
        return envelope

    async def run_global_reconciliation(
        self,
        *,
        completed_results: dict[str, RunEnvelope],
        failures: dict[str, str],
        artifact_dir: str | Path | None = None,
    ) -> GlobalReconciliationEnvelope:
        if self.global_reconciliation is None:
            raise RuntimeError("global reconciliation is disabled")

        started = time.monotonic()
        usage = RunUsage()
        repair_runtime = RepairRuntime()
        cfg = self.config.global_reconciliation
        artifact_dir = Path(artifact_dir) if artifact_dir is not None else None
        if artifact_dir is not None:
            artifact_dir.mkdir(parents=True, exist_ok=True)
            self.harness.save(artifact_dir)

        projection = project_reconciliation_input(completed_results, failures)
        if artifact_dir is not None:
            (artifact_dir / "input_projection.md").write_text(projection.text, encoding="utf-8")
            (artifact_dir / "reducer_summary.json").write_text(
                json.dumps(projection_audit_dict(projection), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        artifact_conflicts = sum(1 for g in projection.reduced_artifacts.values() if g.metadata_conflict)
        self.log.info(
            "stage=global_reconciliation start packets=%s source_claims=%s reduced_claims=%s "
            "source_artifacts=%s reduced_artifacts=%s artifact_conflicts=%s unavailable=%s "
            "input_chars=%s request_budget=%s token_budget=%s wall_clock=%ss",
            len(completed_results) + len(failures),
            len(projection.claims),
            len(projection.reduced_claims),
            len(projection.artifacts),
            len(projection.reduced_artifacts),
            artifact_conflicts,
            len(projection.unavailable_packets),
            len(projection.text),
            cfg.max_model_requests,
            cfg.total_tokens_limit,
            cfg.wall_clock_seconds,
        )

        try:
            async with asyncio.timeout(cfg.wall_clock_seconds):
                outcome = await self.stage_runner.run(
                    stage_agent=self.global_reconciliation,
                    prompt=global_reconciliation_prompt(projection.text),
                    packet_name="GLOBAL",
                    usage=usage,
                    limits=self._global_limits(),
                    repair_runtime=repair_runtime,
                    artifact_dir=artifact_dir,
                )

            semantic_degraded = outcome.output is None
            if semantic_degraded:
                self.log.warning(
                    "stage=global_reconciliation degraded delivery_status=%s fallback=deterministic_keep_all",
                    outcome.status.value,
                )
                decisions = GlobalReconciliationDecisionResult(
                    keep_claim_ids=list(projection.reduced_claims)
                )
                reconciled = assemble_reconciliation_result(decisions, projection)
                reconciled.gaps.append(GlobalGap(
                    question="Global semantic reconciliation did not complete",
                    reason=(
                        f"resolver delivery_status={outcome.status.value}; "
                        "all reduced claims were kept conservatively and artifacts were deterministically merged"
                    ),
                    source_packets=[],
                    impact="major",
                ))
            else:
                decisions = cast(GlobalReconciliationDecisionResult, outcome.output)
                save_model(
                    artifact_dir, "global_reconciliation", "resolver_decisions.json", decisions
                )
                reconciled = assemble_reconciliation_result(decisions, projection)

            reconciled = validate_and_finalize_reconciliation(reconciled, projection)
            if semantic_degraded and not reconciled.unavailable_packets:
                reconciled.status = ReconciliationStatus.COMPLETE_WITH_CONFLICTS

            # Gaps remain investigation records, separate from confirmed facts.
            save_model(
                artifact_dir, "global_reconciliation", "canonical_result.json", reconciled
            )


            global_usage = usage_summary(usage)
            repair_usage = usage_summary(repair_runtime.usage)
            total_usage = merge_usage_summaries(global_usage, repair_usage)
            repair_stats = FormatRepairStats(
                triggered=repair_runtime.triggered,
                successful=repair_runtime.successful,
                failed=repair_runtime.failed,
                attempts=repair_runtime.attempts,
                stage_attempts=repair_runtime.stage_attempts,
                usage=repair_usage,
            )

            self.log.info(
                "stage=global_reconciliation complete elapsed=%.1fs status=%s canonical_claims=%s "
                "canonical_artifacts=%s conflicts=%s historical_gaps=%s report_projection=deterministic "
                "unaccounted_claims=%s unaccounted_artifacts=%s",
                time.monotonic() - started,
                reconciled.status.value,
                len(reconciled.canonical_claims),
                len(reconciled.canonical_artifacts),
                len(reconciled.unresolved_conflicts),
                len(reconciled.gaps),
                len(reconciled.unaccounted_input_claim_ids),
                len(reconciled.unaccounted_input_artifact_ids),
            )
            return GlobalReconciliationEnvelope(
                delivery_status=outcome.status,
                elapsed_seconds=round(time.monotonic() - started, 3),
                result=reconciled,
                usage=total_usage,
                format_repair=repair_stats,
                raw_output_path=outcome.raw_output_path,
                validation_errors=outcome.validation_errors,
                source_claim_count=len(projection.claims),
                source_artifact_count=len(projection.artifacts),
                reduced_claim_count=len(projection.reduced_claims),
                reduced_artifact_count=len(projection.reduced_artifacts),
                artifact_conflict_count=artifact_conflicts,
                provenance_valid=True,
                source_gap_count=len(reconciled.gaps),
            )

        except TimeoutError:
            self.log.error(
                "stage=global_reconciliation timeout elapsed=%.1fs limit=%ss",
                time.monotonic() - started, cfg.wall_clock_seconds,
            )
            raise

