"""Execute a model stage with bounded repair and partial-result preservation."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterable
from dataclasses import dataclass, field
from logging import Logger
from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel
from pydantic_ai import AgentStreamEvent, PartDeltaEvent, PartStartEvent, TextPartDelta, ToolCallPartDelta
from pydantic_ai.usage import RunUsage, UsageLimits

from ..config import AppConfig
from ..investigation.artifact_normalization import normalize_investigation_result
from ..prompts import format_repair_prompt
from ..schemas import DeliveryStatus, InvestigationResult, PartialInvestigationResult
from .artifacts import stage_directory, save_raw_output, save_model, save_validation_errors
from .json_compat import ensure_json_keyword
from .llm import StageAgent
from .repair import (
    FormatRepairFailed,
    exception_chain_text,
    is_repairable_output_failure,
    salvage_investigation_result,
    validate_candidate,
)
from .usage_utils import usage_delta, usage_summary


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
class RepairRuntime:
    usage: RunUsage = field(default_factory=RunUsage)
    triggered: int = 0
    successful: int = 0
    failed: int = 0
    attempts: int = 0
    stage_attempts: dict[str, int] = field(default_factory=dict)


@dataclass
class StageOutcome:
    status: DeliveryStatus
    output: BaseModel | None = None
    partial: PartialInvestigationResult | None = None
    raw_output_path: str | None = None
    validation_errors: list[dict[str, Any]] = field(default_factory=list)


class StageRunner:
    """Run stages using supplied agents; keep per-run repair state with the caller."""

    def __init__(self, *, config: AppConfig, format_repair: StageAgent | None, log: Logger,
                 repair_limits: UsageLimits | None = None):
        self.config = config
        self.format_repair = format_repair
        self.log = log
        self.repair_limits = repair_limits

    def _repair_limits(self) -> UsageLimits:
        if self.repair_limits is not None:
            return self.repair_limits
        request_limit = self.config.format_repair.max_attempts * self.config.workflow.max_model_requests
        return UsageLimits(
            request_limit=request_limit,
            total_tokens_limit=self.config.format_repair.total_tokens_limit,
        )

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
        runtime: RepairRuntime,
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
            # Multiple report passes share a repair budget. A rejected request
            # must not be recorded as another repair attempt.
            self._repair_limits().check_before_request(runtime.usage)
            runtime.attempts += 1
            runtime.stage_attempts[target_stage] = runtime.stage_attempts.get(target_stage, 0) + 1
            before = usage_summary(runtime.usage)
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
            repair_dir = stage_directory(artifact_dir, "format_repair")
            if repair_dir is not None:
                (repair_dir / f"{target_stage}_attempt_{attempt}.txt").write_text(candidate, encoding="utf-8")

            delta = usage_delta(before, usage_summary(runtime.usage))
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

    async def run(
        self,
        *,
        stage_agent: StageAgent,
        prompt: str,
        packet_name: str,
        usage: RunUsage,
        limits: UsageLimits,
        repair_runtime: RepairRuntime,
        artifact_dir: Path | None,
    ) -> StageOutcome:
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
            raw_path = save_raw_output(artifact_dir, stage_agent.stage, observer.raw_output)
            output = cast(BaseModel, run.output)
            if isinstance(output, InvestigationResult) and stage_agent.stage != "reviser":
                output = normalize_investigation_result(output)
            save_model(artifact_dir, stage_agent.stage, "validated_result.json", output)
            return StageOutcome(
                status=DeliveryStatus.SUCCESS,
                output=output,
                raw_output_path=raw_path,
            )
        except asyncio.CancelledError:
            save_raw_output(artifact_dir, stage_agent.stage, observer.raw_output)
            raise
        except Exception as exc:
            raw = observer.raw_output
            raw_path = save_raw_output(artifact_dir, stage_agent.stage, raw)
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
                save_model(artifact_dir, stage_agent.stage, "validated_result.json", repaired)
                return StageOutcome(
                    status=DeliveryStatus.SUCCESS_AFTER_REPAIR,
                    output=repaired,
                    raw_output_path=raw_path,
                )
            except FormatRepairFailed as repair_exc:
                errors = repair_exc.validation_errors
                save_validation_errors(artifact_dir, stage_agent.stage, errors)
                candidate = repair_exc.last_candidate or raw
                partial = None
                if stage_agent.schema is InvestigationResult:
                    partial = salvage_investigation_result(candidate)
                    if partial is not None:
                        partial = normalize_investigation_result(partial)
                        save_model(artifact_dir, stage_agent.stage, "partial_result.json", partial)
                        self.log.warning(
                            "packet=%s stage=%s recovery=partial_validated claims=%s artifacts=%s rejected_claims=%s rejected_artifacts=%s",
                            packet_name,
                            stage_agent.stage,
                            len(partial.claims),
                            len(partial.artifacts),
                            partial.rejected_claims,
                            partial.rejected_artifacts,
                        )
                        return StageOutcome(
                            status=DeliveryStatus.PARTIAL_VALIDATED,
                            partial=partial,
                            raw_output_path=raw_path,
                            validation_errors=errors,
                        )

                self.log.warning(
                    "packet=%s stage=%s recovery=raw_only repair_attempts=%s",
                    packet_name, stage_agent.stage, repair_exc.attempts,
                )
                return StageOutcome(
                    status=DeliveryStatus.RAW_ONLY,
                    raw_output_path=raw_path,
                    validation_errors=errors,
                )
        finally:
            observer.finish()


