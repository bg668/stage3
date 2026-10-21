from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel
from pydantic_ai import AgentStreamEvent, PartDeltaEvent, PartStartEvent, TextPartDelta, ToolCallPartDelta
from pydantic_ai.usage import RunUsage, UsageLimits

from .config import AppConfig
from .llm import StageAgent, build_stage_agent, build_text_stage_agent
from .artifact_normalization import normalize_investigation_result
from .review_guard import constrain_reviewer_scope
from .revision import merge_scoped_revision, validate_revision_scope
from .json_compat import ensure_json_keyword
from .logging_utils import get_logger
from .prompts import (
    analyzer_instructions,
    analyzer_prompt,
    format_repair_instructions,
    format_repair_prompt,
    global_reconciliation_instructions,
    global_reconciliation_prompt,
    final_assessment_instructions,
    final_assessment_prompt,
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
from .final_assessment import (
    FinalAssessmentProvenanceError,
    project_final_assessment_input,
    separate_operational_uncertainties,
    build_material_suggestions,
    validate_assessment,
)
from .evidence_scope import EvidenceScope, PacketCoverage, coverage_warnings
from .evidence_validation import validate_investigation_result, validation_prompt_context
from .source_snippets import build_code_index_from_text
from .repair import (
    FormatRepairFailed,
    exception_chain_text,
    is_repairable_output_failure,
    salvage_investigation_result,
    validate_candidate,
)
from .schemas import (
    CoverageReview,
    DeliveryStatus,
    FormatRepairStats,
    GlobalGap,
    FinalAssessmentEnvelope,
    FinalEvidenceAssessment,
    GlobalReconciliationDecisionResult,
    GlobalReconciliationEnvelope,
    GlobalReconciliationResult,
    InvestigationResult,
    PartialInvestigationResult,
    ReconciliationStatus,
    RunEnvelope,
    UsageSummary,
)
from .usage_utils import merge_usage_summaries, usage_delta, usage_summary


class _ModelOutputObserver:
    """Capture visible model output while deliberately ignoring hidden reasoning parts."""

    def __init__(self, packet_name: str, stage: str, print_stream: bool):
        self.packet_name = packet_name
        self.stage = stage
        self.print_stream = print_stream
        self.started = False
        self.wrote_content = False
        self.text_parts: list[str] = []
        self.tool_parts: list[str] = []

    def _start(self) -> None:
        if not self.print_stream or self.started:
            return
        self.started = True
        print(f"\n--- model stream packet={self.packet_name} stage={self.stage} ---", flush=True)

    @staticmethod
    def _to_text(value) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        try:
            return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        except TypeError:
            return str(value)

    def _emit(self, text: str) -> None:
        if not text or not self.print_stream:
            return
        self._start()
        print(text, end="", flush=True)
        self.wrote_content = True

    async def handle(self, _ctx, event_stream: AsyncIterable[AgentStreamEvent]) -> None:
        async for event in event_stream:
            if isinstance(event, PartStartEvent):
                part_name = type(event.part).__name__
                if part_name == "TextPart":
                    text = self._to_text(getattr(event.part, "content", None))
                    self.text_parts.append(text)
                    self._emit(text)
                elif part_name == "ToolCallPart":
                    text = self._to_text(getattr(event.part, "args", None))
                    self.tool_parts.append(text)
                    self._emit(text)
                continue

            if isinstance(event, PartDeltaEvent):
                if isinstance(event.delta, TextPartDelta):
                    text = event.delta.content_delta
                    self.text_parts.append(text)
                    self._emit(text)
                elif isinstance(event.delta, ToolCallPartDelta):
                    text = self._to_text(event.delta.args_delta)
                    self.tool_parts.append(text)
                    self._emit(text)

    @property
    def raw_output(self) -> str:
        tool = "".join(self.tool_parts).strip()
        text = "".join(self.text_parts).strip()
        if tool and text:
            return f"{tool}\n{text}"
        return tool or text

    def finish(self) -> None:
        if not self.print_stream or not self.started:
            return
        if self.wrote_content:
            print()
        print(f"--- end model stream packet={self.packet_name} stage={self.stage} ---", flush=True)


@dataclass
class _RepairRuntime:
    usage: RunUsage = field(default_factory=RunUsage)
    triggered: int = 0
    successful: int = 0
    failed: int = 0
    attempts: int = 0
    stage_attempts: dict[str, int] = field(default_factory=dict)


@dataclass
class _StageOutcome:
    status: DeliveryStatus
    output: BaseModel | None = None
    partial: PartialInvestigationResult | None = None
    raw_output_path: str | None = None
    validation_errors: list[dict[str, Any]] = field(default_factory=list)


class BoundedInvestigationWorkflow:
    """Bounded investigation with clean-context format repair and graceful result preservation."""

    def __init__(self, config: AppConfig):
        self.config = config
        self.log = get_logger("workflow")
        self.analyzer = build_stage_agent(
            config=config, stage="analyzer", schema=InvestigationResult, instructions=analyzer_instructions()
        )
        self.reviewer = build_stage_agent(
            config=config, stage="reviewer", schema=CoverageReview, instructions=reviewer_instructions()
        )
        self.reviser = build_stage_agent(
            config=config, stage="reviser", schema=InvestigationResult, instructions=reviser_instructions()
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
        self.final_assessment = None
        # Legacy v1.6 stage retained as a callable for compatibility only.
        # Batch/reconcile/report NEVER construct or call a final judgement model.

    def _limits(self) -> UsageLimits:
        return UsageLimits(
            request_limit=self.config.workflow.max_model_requests,
            total_tokens_limit=self.config.workflow.total_tokens_limit,
        )

    def _repair_limits(self) -> UsageLimits:
        request_limit = self.config.format_repair.max_attempts * self.config.workflow.max_model_requests
        return UsageLimits(
            request_limit=request_limit,
            total_tokens_limit=self.config.format_repair.total_tokens_limit,
        )

    def _global_limits(self) -> UsageLimits:
        cfg = self.config.global_reconciliation
        return UsageLimits(
            request_limit=cfg.max_model_requests,
            total_tokens_limit=cfg.total_tokens_limit,
        )

    def _assessment_limits(self) -> UsageLimits:
        cfg = self.config.final_evidence_assessment
        return UsageLimits(
            request_limit=cfg.max_model_requests,
            total_tokens_limit=cfg.total_tokens_limit,
        )

    @staticmethod
    def _usage_summary(usage: RunUsage) -> UsageSummary:
        return usage_summary(usage)

    @staticmethod
    def _stage_dir(artifact_dir: Path | None, stage: str) -> Path | None:
        if artifact_dir is None:
            return None
        path = artifact_dir / stage
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _save_raw(self, artifact_dir: Path | None, stage: str, raw: str) -> str | None:
        stage_dir = self._stage_dir(artifact_dir, stage)
        if stage_dir is None:
            return None
        path = stage_dir / "raw_output.txt"
        path.write_text(raw, encoding="utf-8")
        return str(path)

    def _save_model(self, artifact_dir: Path | None, stage: str, name: str, value: BaseModel) -> str | None:
        stage_dir = self._stage_dir(artifact_dir, stage)
        if stage_dir is None:
            return None
        path = stage_dir / name
        path.write_text(value.model_dump_json(indent=2), encoding="utf-8")
        return str(path)

    def _save_sidecar_json(
        self, artifact_dir: Path | None, group: str, name: str, value: dict[str, Any]
    ) -> str | None:
        stage_dir = self._stage_dir(artifact_dir, group)
        if stage_dir is None:
            return None
        path = stage_dir / name
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        return str(path)

    def _save_validation_errors(
        self, artifact_dir: Path | None, stage: str, errors: list[dict[str, Any]]
    ) -> None:
        stage_dir = self._stage_dir(artifact_dir, stage)
        if stage_dir is None:
            return
        (stage_dir / "validation_errors.json").write_text(
            json.dumps(errors, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def _finish_stage_usage(
        self,
        packet_name: str,
        stage: str,
        usage: RunUsage,
        before: UsageSummary,
        stage_started: float,
    ) -> UsageSummary:
        after = self._usage_summary(usage)
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

    async def _run_plain_agent(
        self,
        *,
        stage_agent: StageAgent,
        prompt: str,
        packet_name: str,
        usage: RunUsage,
        limits: UsageLimits,
    ) -> Any:
        observer = _ModelOutputObserver(
            packet_name,
            stage_agent.stage,
            print_stream=stage_agent.model_config.stream_output,
        )
        try:
            return await stage_agent.agent.run(
                prompt,
                usage=usage,
                usage_limits=limits,
                event_stream_handler=observer.handle,
            )
        finally:
            observer.finish()

    async def _repair_structured_output(
        self,
        *,
        packet_name: str,
        target_stage: str,
        schema: type[BaseModel],
        original_raw_output: str,
        validation_error: str,
        runtime: _RepairRuntime,
        artifact_dir: Path | None,
    ) -> BaseModel:
        if self.format_repair is None:
            initial = validate_candidate(schema, original_raw_output)
            raise FormatRepairFailed(
                target_stage,
                0,
                "format repair is disabled",
                last_candidate=initial.normalized_json or original_raw_output,
                validation_errors=initial.validation_errors or [],
                required_missing=initial.has_required_missing,
            )

        runtime.triggered += 1
        current_candidate = original_raw_output
        current_error = validation_error
        current_errors: list[dict[str, Any]] = []

        # First inspect what we already have. Required semantic fields must never be invented.
        deterministic = validate_candidate(schema, current_candidate)
        if deterministic.output is not None:
            runtime.successful += 1
            self.log.info(
                "packet=%s stage=%s format_repair=deterministic_success raw_chars=%s",
                packet_name, target_stage, len(original_raw_output),
            )
            return deterministic.output
        current_error = deterministic.error or current_error
        current_errors = deterministic.validation_errors or []
        if deterministic.normalized_json:
            current_candidate = deterministic.normalized_json
        if deterministic.has_required_missing:
            runtime.failed += 1
            self.log.warning(
                "packet=%s stage=%s format_repair=skip reason=required_field_missing",
                packet_name, target_stage,
            )
            raise FormatRepairFailed(
                target_stage,
                0,
                current_error,
                last_candidate=current_candidate,
                validation_errors=current_errors,
                required_missing=True,
            )

        schema_json = json.dumps(schema.model_json_schema(), ensure_ascii=False, separators=(",", ":"))
        max_attempts = self.config.format_repair.max_attempts

        for attempt in range(1, max_attempts + 1):
            runtime.attempts += 1
            runtime.stage_attempts[target_stage] = runtime.stage_attempts.get(target_stage, 0) + 1
            before = self._usage_summary(runtime.usage)
            started = time.monotonic()
            self.log.warning(
                "packet=%s stage=%s format_repair=start attempt=%s/%s raw_chars=%s candidate_chars=%s error=%s",
                packet_name,
                target_stage,
                attempt,
                max_attempts,
                len(original_raw_output),
                len(current_candidate),
                current_error.replace("\n", " ")[:800],
            )

            prompt = format_repair_prompt(
                target_stage=target_stage,
                attempt=attempt,
                target_schema=schema_json,
                validation_error=current_error,
                original_raw_output=original_raw_output,
                current_candidate=current_candidate,
            )
            repair_run = await self._run_plain_agent(
                stage_agent=self.format_repair,
                prompt=prompt,
                packet_name=packet_name,
                usage=runtime.usage,
                limits=self._repair_limits(),
            )
            candidate = str(repair_run.output).strip()
            repair_dir = self._stage_dir(artifact_dir, "format_repair")
            if repair_dir is not None:
                (repair_dir / f"{target_stage}_attempt_{attempt}.txt").write_text(candidate, encoding="utf-8")

            delta = usage_delta(before, self._usage_summary(runtime.usage))
            self.log.info(
                "packet=%s stage=%s format_repair=attempt_done attempt=%s elapsed=%.1fs requests=%s input_tokens=%s output_tokens=%s reasoning_tokens=%s candidate_chars=%s",
                packet_name,
                target_stage,
                attempt,
                time.monotonic() - started,
                delta.requests,
                delta.input_tokens,
                delta.output_tokens,
                delta.reasoning_tokens,
                len(candidate),
            )

            try:
                marker = json.loads(candidate)
            except Exception:
                marker = None
            if isinstance(marker, dict) and marker.get("__repair_failed__") is True:
                current_error = f"repairer refused semantic invention: {marker.get('reason', 'no reason')}"
                current_candidate = candidate
                break

            validated = validate_candidate(schema, candidate)
            if validated.output is not None:
                runtime.successful += 1
                self.log.info(
                    "packet=%s stage=%s format_repair=success attempt=%s",
                    packet_name, target_stage, attempt,
                )
                return validated.output

            current_error = validated.error or "unknown validation error"
            current_errors = validated.validation_errors or []
            current_candidate = validated.normalized_json or candidate

            # The first time a required semantic field is known missing, stop immediately.
            if validated.has_required_missing:
                self.log.warning(
                    "packet=%s stage=%s format_repair=stop attempt=%s reason=required_field_missing",
                    packet_name, target_stage, attempt,
                )
                break

        runtime.failed += 1
        self.log.error(
            "packet=%s stage=%s format_repair=failed attempts=%s error=%s",
            packet_name,
            target_stage,
            runtime.stage_attempts.get(target_stage, 0),
            current_error.replace("\n", " ")[:1200],
        )
        raise FormatRepairFailed(
            target_stage,
            runtime.stage_attempts.get(target_stage, 0),
            current_error,
            last_candidate=current_candidate,
            validation_errors=current_errors,
            required_missing=any(item.get("type") == "missing" for item in current_errors),
        )

    async def _run_agent(
        self,
        *,
        stage_agent: StageAgent,
        prompt: str,
        packet_name: str,
        usage: RunUsage,
        limits: UsageLimits,
        repair_runtime: _RepairRuntime,
        artifact_dir: Path | None,
    ) -> _StageOutcome:
        observer = _ModelOutputObserver(
            packet_name,
            stage_agent.stage,
            print_stream=stage_agent.model_config.stream_output,
        )
        prompt = ensure_json_keyword(prompt, stage_agent.model_config.output_mode)
        try:
            run = await stage_agent.agent.run(
                prompt,
                usage=usage,
                usage_limits=limits,
                event_stream_handler=observer.handle,
            )
            raw_path = self._save_raw(artifact_dir, stage_agent.stage, observer.raw_output)
            output = cast(BaseModel, run.output)
            if isinstance(output, InvestigationResult) and stage_agent.stage != "reviser":
                output = normalize_investigation_result(output)
            self._save_model(artifact_dir, stage_agent.stage, "validated_result.json", output)
            return _StageOutcome(
                status=DeliveryStatus.SUCCESS,
                output=output,
                raw_output_path=raw_path,
            )
        except Exception as exc:
            raw = observer.raw_output
            raw_path = self._save_raw(artifact_dir, stage_agent.stage, raw)
            if not (
                stage_agent.schema is not None
                and is_repairable_output_failure(exc, raw)
            ):
                raise

            error_text = exception_chain_text(exc)
            self.log.warning(
                "packet=%s stage=%s structured_output_validation_failed raw_chars=%s error=%s",
                packet_name,
                stage_agent.stage,
                len(raw),
                error_text.replace("\n", " ")[:1200],
            )
            try:
                repaired = await self._repair_structured_output(
                    packet_name=packet_name,
                    target_stage=stage_agent.stage,
                    schema=stage_agent.schema,
                    original_raw_output=raw,
                    validation_error=error_text,
                    runtime=repair_runtime,
                    artifact_dir=artifact_dir,
                )
                if isinstance(repaired, InvestigationResult) and stage_agent.stage != "reviser":
                    repaired = normalize_investigation_result(repaired)
                self._save_model(artifact_dir, stage_agent.stage, "validated_result.json", repaired)
                return _StageOutcome(
                    status=DeliveryStatus.SUCCESS_AFTER_REPAIR,
                    output=repaired,
                    raw_output_path=raw_path,
                )
            except FormatRepairFailed as repair_exc:
                errors = repair_exc.validation_errors
                self._save_validation_errors(artifact_dir, stage_agent.stage, errors)
                candidate = repair_exc.last_candidate or raw
                partial = None
                if stage_agent.schema is InvestigationResult:
                    partial = salvage_investigation_result(candidate)
                    if partial is not None:
                        partial = normalize_investigation_result(partial)
                        self._save_model(artifact_dir, stage_agent.stage, "partial_result.json", partial)
                        self.log.warning(
                            "packet=%s stage=%s recovery=partial_validated claims=%s artifacts=%s rejected_claims=%s rejected_artifacts=%s",
                            packet_name,
                            stage_agent.stage,
                            len(partial.claims),
                            len(partial.artifacts),
                            partial.rejected_claims,
                            partial.rejected_artifacts,
                        )
                        return _StageOutcome(
                            status=DeliveryStatus.PARTIAL_VALIDATED,
                            partial=partial,
                            raw_output_path=raw_path,
                            validation_errors=errors,
                        )

                self.log.warning(
                    "packet=%s stage=%s recovery=raw_only repair_attempts=%s",
                    packet_name, stage_agent.stage, repair_exc.attempts,
                )
                return _StageOutcome(
                    status=DeliveryStatus.RAW_ONLY,
                    raw_output_path=raw_path,
                    validation_errors=errors,
                )
        finally:
            observer.finish()

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
        repair_runtime: _RepairRuntime,
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
        core_usage = self._usage_summary(usage)
        repair_usage = self._usage_summary(repair_runtime.usage)
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
        repair_runtime = _RepairRuntime()
        limits = self._limits()
        cfg = self.config.workflow
        dependency_ids = list(dependency_ids or [])
        artifact_dir = Path(artifact_dir) if artifact_dir is not None else None
        if artifact_dir is not None:
            artifact_dir.mkdir(parents=True, exist_ok=True)
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
                before = self._usage_summary(usage)
                self._stage_log(packet_name, self.analyzer, "start")
                analyzer_outcome = await self._run_agent(
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
                        self._save_sidecar_json(
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
                self._save_sidecar_json(
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
                before = self._usage_summary(usage)
                self._stage_log(packet_name, self.reviewer, "start")
                reviewer_outcome = await self._run_agent(
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
                    self._save_sidecar_json(
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
                    self._save_model(artifact_dir, "reviewer", "scope_guarded_result.json", review)
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
                        before = self._usage_summary(usage)
                        self._stage_log(packet_name, self.reviser, "start")
                        reviser_outcome = await self._run_agent(
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
                                self._save_sidecar_json(artifact_dir, "reviser", "revision_scope.json", applied.audit)
                                self._save_model(artifact_dir, "reviser", "scope_applied_result.json", final)
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
                        self._save_sidecar_json(
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
                self._save_sidecar_json(
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
            core_u = self._usage_summary(usage)
            repair_u = self._usage_summary(repair_runtime.usage)
            total_u = merge_usage_summaries(core_u, repair_u)
            self.log.error(
                "packet=%s timeout elapsed=%.1fs requests=%s total_tokens=%s reasoning_tokens=%s repair_requests=%s limit=%ss",
                packet_name, elapsed, total_u.requests, total_u.total_tokens, total_u.reasoning_tokens,
                repair_u.requests, cfg.wall_clock_seconds,
            )
            raise
        except Exception:
            elapsed = time.monotonic() - started
            core_u = self._usage_summary(usage)
            repair_u = self._usage_summary(repair_runtime.usage)
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

    async def run_final_evidence_assessment(
        self,
        *,
        reconciled: GlobalReconciliationResult,
        artifact_dir: str | Path | None = None,
        evidence_scope: EvidenceScope | None = None,
        packet_coverage: list[PacketCoverage] | None = None,
    ) -> FinalAssessmentEnvelope:
        """One bounded, read-only assessment after canonical reconciliation.

        No historical gap is promoted to a report unknown without a model decision.
        Failure never mutates the canonical facts or creates an authoritative empty report.
        """
        if self.final_assessment is None:
            raise RuntimeError("final_evidence_assessment is disabled")

        started = time.monotonic()
        cfg = self.config.final_evidence_assessment
        directory = Path(artifact_dir) if artifact_dir is not None else None
        stage_name = "final_evidence_assessment"
        stage_dir = self._stage_dir(directory, stage_name)
        if stage_dir is not None:
            # A previous success must not survive a later failed re-assessment.
            for stale_name in (
                "assessed_result.json", "validated_result.json", "assessment_envelope.json",
                "raw_output.txt", "validation_errors.json", "material_suggestions.json",
                "redirected_uncertainties.json",
            ):
                stale = stage_dir / stale_name
                if stale.exists():
                    stale.unlink()
        evidence_scope = evidence_scope or EvidenceScope()
        packet_coverage = packet_coverage or []
        projection = project_final_assessment_input(
            reconciled, evidence_scope=evidence_scope, packet_coverage=packet_coverage
        )
        if stage_dir is not None:
            (stage_dir / "input_projection.md").write_text(projection.text, encoding="utf-8")
            (stage_dir / "projection_audit.json").write_text(
                json.dumps({
                    "source_claim_count": projection.source_claim_count,
                    "source_artifact_count": projection.source_artifact_count,
                    "source_gap_count": projection.source_gap_count,
                    "claim_ids": sorted(projection.claim_ids),
                    "artifact_ids": sorted(projection.artifact_ids),
                    "gap_ids": sorted(projection.gap_ids),
                    "historical_gaps_selected": projection.included_historical_gap_count,
                    "material_scope": [m.model_dump(mode="json") for m in evidence_scope.materials],
                    "input_coverage_warnings": [
                        w.model_dump(mode="json")
                        for w in coverage_warnings(list(projection.packet_items.values()))
                    ],
                }, ensure_ascii=False, indent=2), encoding="utf-8",
            )

        usage = RunUsage()
        repair_runtime = _RepairRuntime()
        completed = False
        provenance_valid = False
        result: FinalEvidenceAssessment | None = None
        delivery_status = DeliveryStatus.EXECUTION_FAILED
        raw_path: str | None = None
        errors: list[dict[str, Any]] = []
        warning: str | None = None
        fallback_scope_ids: list[str] = []  # retained for v1.5 schema compatibility
        material_suggestions = build_material_suggestions(evidence_scope)
        redirected_uncertainties: list[dict[str, object]] = []
        if stage_dir is not None:
            (stage_dir / "material_suggestions.json").write_text(
                json.dumps([m.model_dump(mode="json") for m in material_suggestions],
                           ensure_ascii=False, indent=2), encoding="utf-8",
            )

        self.log.info(
            "stage=final_evidence_assessment start canonical_claims=%s artifacts=%s historical_gaps=%s input_chars=%s wall_clock=%ss",
            projection.source_claim_count,
            projection.source_artifact_count,
            projection.source_gap_count,
            len(projection.text),
            cfg.wall_clock_seconds,
        )
        try:
            async with asyncio.timeout(cfg.wall_clock_seconds):
                outcome = await self._run_agent(
                    stage_agent=self.final_assessment,
                    prompt=final_assessment_prompt(projection.text),
                    packet_name="FINAL-ASSESSMENT",
                    usage=usage,
                    limits=self._assessment_limits(),
                    repair_runtime=repair_runtime,
                    artifact_dir=directory,
                )
            delivery_status = outcome.status
            raw_path = outcome.raw_output_path
            errors = outcome.validation_errors
            if outcome.output is not None:
                candidate = cast(FinalEvidenceAssessment, outcome.output)
                try:
                    result, redirected_uncertainties = separate_operational_uncertainties(candidate)
                    if stage_dir is not None:
                        (stage_dir / "redirected_uncertainties.json").write_text(
                            json.dumps(redirected_uncertainties, ensure_ascii=False, indent=2),
                            encoding="utf-8",
                        )
                    if redirected_uncertainties:
                        self.log.warning(
                            "stage=final_evidence_assessment redirected_nonbehavior_items=%s; "
                            "inventory suggestions are generated independently",
                            len(redirected_uncertainties),
                        )
                    result = validate_assessment(result, projection)
                except FinalAssessmentProvenanceError as exc:
                    # JSON-schema validity is not enough: refuse unsupported fact IDs.
                    warning = str(exc)
                    delivery_status = DeliveryStatus.RAW_ONLY
                    errors = [*errors, {"type": "provenance_error", "message": warning}]
                    self._save_validation_errors(directory, stage_name, errors)
                else:
                    completed = True
                    provenance_valid = True
                    self._save_model(directory, stage_name, "assessed_result.json", result)
            else:
                warning = "Structured final assessment was not validated; canonical facts are preserved"
        except TimeoutError:
            warning = f"Final evidence assessment timeout after {cfg.wall_clock_seconds}s"
            self.log.error("stage=final_evidence_assessment timeout=%ss", cfg.wall_clock_seconds)
        except Exception as exc:
            warning = f"{type(exc).__name__}: {exc}"
            self.log.error("stage=final_evidence_assessment error=%s", warning)

        if raw_path is None and stage_dir is not None:
            path = stage_dir / "raw_output.txt"
            if path.exists():
                raw_path = str(path)

        repair_usage = self._usage_summary(repair_runtime.usage)
        combined_usage = merge_usage_summaries(self._usage_summary(usage), repair_usage)
        repair_stats = FormatRepairStats(
            triggered=repair_runtime.triggered,
            successful=repair_runtime.successful,
            failed=repair_runtime.failed,
            attempts=repair_runtime.attempts,
            stage_attempts=repair_runtime.stage_attempts,
            usage=repair_usage,
        )
        envelope = FinalAssessmentEnvelope(
            delivery_status=delivery_status,
            completed=completed,
            elapsed_seconds=round(time.monotonic() - started, 3),
            result=result,
            usage=combined_usage,
            format_repair=repair_stats,
            raw_output_path=raw_path,
            validation_errors=errors,
            source_claim_count=projection.source_claim_count,
            source_artifact_count=projection.source_artifact_count,
            source_gap_count=projection.source_gap_count,
            provenance_valid=provenance_valid,
            warning=warning,
            material_scope=[m.model_dump(mode="json") for m in evidence_scope.materials],
            input_coverage_warnings=[
                item.model_dump(mode="json")
                for item in coverage_warnings(list(projection.packet_items.values()))
            ],
            fallback_material_scope_ids=fallback_scope_ids,
            material_suggestions=material_suggestions,
            redirected_uncertainties=redirected_uncertainties,
        )
        self._save_model(directory, stage_name, "assessment_envelope.json", envelope)
        self.log.info(
            "stage=final_evidence_assessment complete=%s delivery_status=%s uncertainties=%s requests=%s tokens=%s elapsed=%.1fs material_suggestions=%s redirected=%s coverage_warnings=%s",
            completed, delivery_status.value,
            len(result.remaining_uncertainties) if result else 0,
            combined_usage.requests, combined_usage.total_tokens, envelope.elapsed_seconds,
            len(material_suggestions), len(redirected_uncertainties), len(envelope.input_coverage_warnings),
        )
        return envelope

    async def run_global_reconciliation(
        self,
        *,
        completed_results: dict[str, RunEnvelope],
        failures: dict[str, str],
        artifact_dir: str | Path | None = None,
        evidence_scope: EvidenceScope | None = None,
        packet_coverage: list[PacketCoverage] | None = None,
    ) -> GlobalReconciliationEnvelope:
        if self.global_reconciliation is None:
            raise RuntimeError("global reconciliation is disabled")

        started = time.monotonic()
        usage = RunUsage()
        repair_runtime = _RepairRuntime()
        cfg = self.config.global_reconciliation
        artifact_dir = Path(artifact_dir) if artifact_dir is not None else None
        if artifact_dir is not None:
            artifact_dir.mkdir(parents=True, exist_ok=True)

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
                outcome = await self._run_agent(
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
                self._save_model(
                    artifact_dir, "global_reconciliation", "resolver_decisions.json", decisions
                )
                reconciled = assemble_reconciliation_result(decisions, projection)

            reconciled = validate_and_finalize_reconciliation(reconciled, projection)
            if semantic_degraded and not reconciled.unavailable_packets:
                reconciled.status = ReconciliationStatus.COMPLETE_WITH_CONFLICTS

            # Historical gaps stay in reconciled.gaps for audit ONLY. Final report
            # unknowns come exclusively from FinalEvidenceAssessment.
            self._save_model(
                artifact_dir, "global_reconciliation", "canonical_result.json", reconciled
            )

            # v1.7: no model-generated remaining-unknowns stage.
            # Preserve historical gaps exclusively as investigation appendix records.
            assessment: FinalAssessmentEnvelope | None = None

            global_usage = self._usage_summary(usage)
            repair_usage = self._usage_summary(repair_runtime.usage)
            total_usage = merge_usage_summaries(global_usage, repair_usage)
            if assessment is not None:
                total_usage = merge_usage_summaries(total_usage, assessment.usage)
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
                final_assessment=assessment,
                source_gap_count=len(reconciled.gaps),
            )

        except TimeoutError:
            self.log.error(
                "stage=global_reconciliation timeout elapsed=%.1fs limit=%ss",
                time.monotonic() - started, cfg.wall_clock_seconds,
            )
            raise

