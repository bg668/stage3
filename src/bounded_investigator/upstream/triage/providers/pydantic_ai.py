"""PydanticAI adapter for real Stage-2 model calls.

Vendor/model details, structured-output schema and prompt selection are isolated here.
The business runners depend only on the stable ``TriageProvider`` protocol.
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from ..domain import (
    FunctionVerdict,
    ProviderResponse,
    Round2FunctionVerdict,
    Usage,
)
from ..llm_audit import LLMCallRecorder, utc_now
from ..llm_config import LLMConfigError, ResolvedModel
from ..provider import (
    ProviderBudgetError,
    ProviderError,
    ProviderHealthResult,
    ProviderSchemaError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from ..task import TriageRequest, sha256_text


class Round1Output(BaseModel):
    model_config = ConfigDict(extra="forbid")
    verdicts: list[FunctionVerdict]


class Round2Output(BaseModel):
    """Round2 schema structurally excludes UNCERTAIN/U_* reasons."""

    model_config = ConfigDict(extra="forbid")
    verdicts: list[Round2FunctionVerdict]


@dataclass(frozen=True)
class _Runtime:
    Agent: Any
    ModelSettings: Any
    UsageLimits: Any
    ToolOutput: Any
    NativeOutput: Any
    PromptedOutput: Any
    UsageLimitExceeded: type[Exception]
    UnexpectedModelBehavior: type[Exception]
    OpenAIChatModel: Any
    OpenAIProvider: Any


def _load_runtime() -> _Runtime:
    try:
        from pydantic_ai import (
            Agent,
            ModelSettings,
            NativeOutput,
            PromptedOutput,
            ToolOutput,
            UnexpectedModelBehavior,
            UsageLimitExceeded,
            UsageLimits,
        )
        from pydantic_ai.models.openai import OpenAIChatModel
        from pydantic_ai.providers.openai import OpenAIProvider
    except ImportError as exc:
        raise LLMConfigError(
            "未安装阶段 2 的 PydanticAI 依赖；请安装 requirements-stage2.txt"
        ) from exc
    return _Runtime(
        Agent=Agent,
        ModelSettings=ModelSettings,
        UsageLimits=UsageLimits,
        ToolOutput=ToolOutput,
        NativeOutput=NativeOutput,
        PromptedOutput=PromptedOutput,
        UsageLimitExceeded=UsageLimitExceeded,
        UnexpectedModelBehavior=UnexpectedModelBehavior,
        OpenAIChatModel=OpenAIChatModel,
        OpenAIProvider=OpenAIProvider,
    )


class PydanticAIProvider:
    def __init__(
        self,
        resolved: ResolvedModel,
        *,
        round_number: Literal[1, 2] = 1,
        instructions: str | None = None,
        runtime: _Runtime | None = None,
        agent: Any | None = None,
        call_recorder: LLMCallRecorder | None = None,
        request_timeout_seconds: float = 120.0,
    ) -> None:
        self.resolved = resolved
        self.round_number = round_number
        self._runtime = runtime or _load_runtime()
        self._instructions = instructions or _load_prompt(round_number)
        self._agent = agent or self._build_agent()
        if request_timeout_seconds <= 0:
            raise ValueError("request_timeout_seconds 必须 > 0")
        self._call_recorder = call_recorder
        self._request_timeout_seconds = float(request_timeout_seconds)
        self._audit_attempts: dict[str, int] = {}

    @property
    def _output_model(self) -> type[BaseModel]:
        return Round2Output if self.round_number == 2 else Round1Output

    def _build_agent(self) -> Any:
        runtime = self._runtime
        model: Any
        if self.resolved.provider_kind == "pydantic-native":
            model = self.resolved.model
        elif self.resolved.provider_kind == "openai-compatible":
            assert self.resolved.api_key_env is not None
            api_key = os.environ.get(self.resolved.api_key_env)
            if not api_key:
                raise LLMConfigError(
                    "环境变量 %s 未设置(provider=%s)"
                    % (self.resolved.api_key_env, self.resolved.provider_name)
                )
            model = runtime.OpenAIChatModel(
                self.resolved.model,
                provider=runtime.OpenAIProvider(
                    base_url=self.resolved.base_url,
                    api_key=api_key,
                ),
            )
        else:
            raise LLMConfigError("不支持的 provider kind: %s" % self.resolved.provider_kind)

        settings_kwargs: dict[str, Any] = {
            "max_tokens": self.resolved.max_output_tokens,
        }
        from ....execution.pacing import PacedModel
        model = PacedModel(model, service_key=(self.resolved.base_url or self.resolved.model.split(':', 1)[0]).rstrip('/'),
                           interval=self.resolved.min_request_interval_seconds)
        reasoning_effort = self.resolved.reasoning_effort
        if reasoning_effort is not None:
            settings_kwargs["openai_reasoning_effort"] = reasoning_effort
        if reasoning_effort in (None, "none"):
            settings_kwargs["temperature"] = self.resolved.temperature

        return runtime.Agent(
            model,
            output_type=self._build_output_type(),
            instructions=self._instructions,
            model_settings=runtime.ModelSettings(**settings_kwargs),
            retries={"tools": 0, "output": 0},
        )

    def _build_output_type(self) -> Any:
        runtime = self._runtime
        output_model = self._output_model
        if self.resolved.output_mode == "tool":
            return runtime.ToolOutput(output_model, max_retries=0)
        if self.resolved.output_mode == "native":
            return runtime.NativeOutput(output_model)
        if self.resolved.output_mode == "prompted":
            return runtime.PromptedOutput(output_model)
        raise LLMConfigError("未知 output_mode: %s" % self.resolved.output_mode)

    def set_call_recorder(self, recorder: LLMCallRecorder | None) -> None:
        """Attach one run-scoped recorder without changing the provider protocol."""
        self._call_recorder = recorder
        self._audit_attempts = {}

    def set_request_timeout(self, timeout_seconds: float) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds 必须 > 0")
        self._request_timeout_seconds = float(timeout_seconds)

    async def classify(self, request: TriageRequest) -> ProviderResponse:
        if request.task.round != self.round_number:
            raise ProviderError(
                "provider round=%d 不能执行 round=%d task"
                % (self.round_number, request.task.round)
            )

        attempt = self._next_audit_attempt(request.task.task_id)
        started_at = utc_now()
        started = time.perf_counter()
        limits = self.resolved.usage_limits
        usage_limits = self._runtime.UsageLimits(
            request_limit=1,
            input_tokens_limit=limits.input_tokens_limit,
            output_tokens_limit=limits.output_tokens_limit,
            total_tokens_limit=limits.total_tokens_limit,
        )
        try:
            result = await asyncio.wait_for(
                self._agent.run(
                    request.input_text,
                    usage_limits=usage_limits,
                ),
                timeout=self._request_timeout_seconds,
            )
        except TimeoutError as exc:
            error = "LLM request timed out after %.1fs" % self._request_timeout_seconds
            self._record_call(
                request,
                attempt=attempt,
                started_at=started_at,
                started=started,
                status="PROVIDER_TIMEOUT",
                error=error,
            )
            raise ProviderTimeoutError(error) from exc
        except self._runtime.UsageLimitExceeded as exc:
            self._record_call(
                request,
                attempt=attempt,
                started_at=started_at,
                started=started,
                status="BUDGET_EXCEEDED",
                error=str(exc),
            )
            raise ProviderBudgetError(str(exc)) from exc
        except self._runtime.UnexpectedModelBehavior as exc:
            self._record_call(
                request,
                attempt=attempt,
                started_at=started_at,
                started=started,
                status="SCHEMA_FAILED",
                error=str(exc),
            )
            raise ProviderSchemaError(str(exc)) from exc
        except Exception as exc:
            error = "%s: %s" % (type(exc).__name__, exc)
            self._record_call(
                request,
                attempt=attempt,
                started_at=started_at,
                started=started,
                status="PROVIDER_ERROR",
                error=error,
            )
            unavailable = _provider_unavailable_details(exc)
            if unavailable is not None:
                status_code, error_code = unavailable
                raise ProviderUnavailableError(
                    error,
                    status_code=status_code,
                    error_code=error_code,
                ) from exc
            raise ProviderError(error) from exc

        latency_ms = max(0, round((time.perf_counter() - started) * 1000))
        output_model = self._output_model
        try:
            output = (
                result.output
                if isinstance(result.output, output_model)
                else output_model.model_validate(result.output)
            )
        except (ValidationError, TypeError, ValueError) as exc:
            usage = _usage_from_result(result, latency_ms)
            self._record_call(
                request,
                attempt=attempt,
                started_at=started_at,
                started=started,
                status="SCHEMA_FAILED",
                error=str(exc),
                usage=usage,
                structured_output=_jsonable(getattr(result, "output", None)),
            )
            raise ProviderSchemaError(str(exc)) from exc

        if self.round_number == 2:
            verdicts = [item.to_function_verdict() for item in output.verdicts]
        else:
            verdicts = list(output.verdicts)

        usage = _usage_from_result(result, latency_ms)
        response = ProviderResponse(
            verdicts=verdicts,
            model_id=self.resolved.model_id,
            usage=usage,
        )
        self._record_call(
            request,
            attempt=attempt,
            started_at=started_at,
            started=started,
            status="OK",
            usage=usage,
            structured_output=output.model_dump(mode="json"),
        )
        return response

    async def probe(self, *, timeout_seconds: float) -> ProviderHealthResult:
        """Verify that this concrete model can answer through the configured adapter.

        The probe reuses the real structured-output agent but is kept outside normal
        task/LLM audit accounting.  Its dedicated health record is written by the
        provider-health coordinator.
        """
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds 必须 > 0")
        started = time.perf_counter()
        limits = self.resolved.usage_limits
        usage_limits = self._runtime.UsageLimits(
            request_limit=1,
            input_tokens_limit=limits.input_tokens_limit,
            output_tokens_limit=limits.output_tokens_limit,
            total_tokens_limit=limits.total_tokens_limit,
        )
        try:
            result = await asyncio.wait_for(
                self._agent.run(
                    _probe_input(self.round_number),
                    usage_limits=usage_limits,
                ),
                timeout=float(timeout_seconds),
            )
        except TimeoutError:
            return self._health_result(
                "TIMEOUT",
                started,
                "provider probe timed out after %.1fs" % float(timeout_seconds),
            )
        except Exception as exc:
            return self._health_result(
                "ERROR",
                started,
                "%s: %s" % (type(exc).__name__, exc),
            )

        output_model = self._output_model
        try:
            output = (
                result.output
                if isinstance(result.output, output_model)
                else output_model.model_validate(result.output)
            )
            if not output.verdicts:
                raise ValueError("provider probe returned no verdict")
        except (ValidationError, TypeError, ValueError) as exc:
            return self._health_result("ERROR", started, "invalid probe output: %s" % exc)
        return self._health_result("OK", started, None)

    def _health_result(
        self,
        status: str,
        started: float,
        error: str | None,
    ) -> ProviderHealthResult:
        return ProviderHealthResult(
            status=status,  # type: ignore[arg-type]
            provider_name=self.resolved.provider_name,
            model_profile=self.resolved.profile_name,
            model_id=self.resolved.model_id,
            round_number=self.round_number,
            latency_ms=max(0, round((time.perf_counter() - started) * 1000)),
            error=error,
        )

    def _next_audit_attempt(self, task_id: str) -> int:
        attempt = self._audit_attempts.get(task_id, 0) + 1
        self._audit_attempts[task_id] = attempt
        return attempt

    def _record_call(
        self,
        request: TriageRequest,
        *,
        attempt: int,
        started_at: str,
        started: float,
        status: str,
        error: str | None = None,
        usage: Usage | None = None,
        structured_output: Any | None = None,
    ) -> None:
        if self._call_recorder is None:
            return
        latency_ms = max(0, round((time.perf_counter() - started) * 1000))
        effective_usage = usage or Usage(latency_ms=latency_ms)
        self._call_recorder.append(
            {
                "started_at": started_at,
                "finished_at": utc_now(),
                "round": request.task.round,
                "task_id": request.task.task_id,
                "parent_task_id": request.task.parent_task_id,
                "unit_id": request.task.unit_id,
                "attempt": attempt,
                "refs": list(request.task.subject_refs),
                "provider_name": self.resolved.provider_name,
                "provider_kind": self.resolved.provider_kind,
                "model_profile": self.resolved.profile_name,
                "model_id": self.resolved.model_id,
                "output_mode": self.resolved.output_mode,
                "model_settings": {
                    "temperature": (
                        self.resolved.temperature
                        if self.resolved.reasoning_effort in (None, "none")
                        else None
                    ),
                    "reasoning_effort": self.resolved.reasoning_effort,
                    "max_output_tokens": self.resolved.max_output_tokens,
                    "usage_limits": self.resolved.usage_limits.model_dump(exclude_none=True),
                },
                "output_schema": self._output_model.model_json_schema(),
                "instructions": self._instructions,
                "input_text": request.input_text,
                "input_sha256": sha256_text(request.input_text),
                "structured_output": structured_output,
                "status": status,
                "usage": effective_usage.model_dump(),
                "latency_ms": latency_ms,
                "error": error,
            }
        )


def _provider_unavailable_details(exc: Exception) -> tuple[int | None, str | None] | None:
    """Normalize transient provider failures without leaking vendor types upward.

    PydanticAI/openai-compatible adapters may wrap HTTP exceptions differently across
    versions. Prefer structured status/body attributes and only use text/type-name
    fallbacks for connection failures and gateway-specific error codes.
    """
    transient_codes = {"no_accounts", "upstream_unavailable", "rate_limit_exceeded"}
    connection_markers = (
        "connectionerror",
        "connecterror",
        "connecttimeout",
        "apiconnectionerror",
        "remoteprotocolerror",
    )
    text_markers = {
        "no_accounts": "no_accounts",
        "upstream_unavailable": "upstream_unavailable",
        "all upstream accounts are unavailable": "no_accounts",
    }

    for item in _exception_chain(exc):
        status_code = _status_code_from_exception(item)
        error_code = _error_code_from_exception(item)
        if status_code is not None and (
            status_code in {429, 502, 503, 504, 529} or 520 <= status_code <= 524
        ):
            return status_code, error_code
        if error_code is not None and error_code.lower() in transient_codes:
            return status_code, error_code

        if isinstance(item, ConnectionError):
            return status_code, error_code
        type_name = type(item).__name__.lower()
        if any(marker in type_name for marker in connection_markers):
            return status_code, error_code

        message = str(item).lower()
        for marker, normalized_code in text_markers.items():
            if marker in message:
                return (
                    status_code or _status_code_from_text(message),
                    error_code or normalized_code,
                )
    return None


def _exception_chain(exc: Exception):
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def _status_code_from_exception(exc: BaseException) -> int | None:
    candidates = [
        getattr(exc, "status_code", None),
        getattr(getattr(exc, "response", None), "status_code", None),
    ]
    for value in candidates:
        try:
            code = int(value)
        except (TypeError, ValueError):
            continue
        if 100 <= code <= 599:
            return code
    return None


def _status_code_from_text(message: str) -> int | None:
    match = re.search(r"\bhttp\s+(\d{3})\b", message, flags=re.IGNORECASE)
    if match is None:
        return None
    code = int(match.group(1))
    return code if 100 <= code <= 599 else None


def _error_code_from_exception(exc: BaseException) -> str | None:
    for value in (
        getattr(exc, "code", None),
        getattr(exc, "error_code", None),
    ):
        if isinstance(value, str) and value.strip():
            return value.strip()

    for body in (
        getattr(exc, "body", None),
        getattr(exc, "data", None),
    ):
        code = _error_code_from_body(body)
        if code is not None:
            return code
    return None


def _error_code_from_body(body: Any) -> str | None:
    if not isinstance(body, dict):
        return None
    direct = body.get("code")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    error = body.get("error")
    if isinstance(error, dict):
        nested = error.get("code")
        if isinstance(nested, str) and nested.strip():
            return nested.strip()
    return None

def _usage_from_result(result: Any, latency_ms: int) -> Usage:
    provider_usage = getattr(result, "usage", None)
    return Usage(
        input_tokens=int(getattr(provider_usage, "input_tokens", 0) or 0),
        output_tokens=int(getattr(provider_usage, "output_tokens", 0) or 0),
        requests=int(getattr(provider_usage, "requests", 0) or 0),
        latency_ms=latency_ms,
    )


def _jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return repr(value)


def _probe_input(round_number: Literal[1, 2]) -> str:
    decisions = "[ANALYZE | SKIP | UNCERTAIN]" if round_number == 1 else "[ANALYZE | SKIP]"
    example_decision = "UNCERTAIN" if round_number == 1 else "SKIP"
    example_reason = "U_CALLER_CONTEXT" if round_number == 1 else "S_PURE_COMPUTE"
    return (
        "# PROVIDER PREFLIGHT\n"
        "这是一次模型服务连通性与结构化输出检查，不是样本分析。\n"
        "请仅对 F-0000 返回一个合法 verdict；这个函数是纯常量返回。\n"
        "<verdicts>: <数组；仅一项>\n"
        "<ref>: <F-0000>\n"
        "<decision>: %s\n"
        "<reason>: <与 decision 匹配的合法 reason>\n"
        "<note>: <一句简短理由>\n"
        "简短示例: {\"verdicts\":[{\"ref\":\"F-0000\",\"decision\":\"%s\",\"reason\":\"%s\",\"note\":\"仅用于结构化输出探测\"}]}\n"
        "ref: F-0000\n"
        "code:\nint preflight(void) { return 0; }\n"
        "round: %d\n" % (decisions, example_decision, example_reason, round_number)
    )


def _load_prompt(round_number: Literal[1, 2]) -> str:
    name = "round2.txt" if round_number == 2 else "round1.txt"
    path = Path(__file__).resolve().parents[1] / "prompts" / name
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise LLMConfigError("读取 Round%d prompt 失败 %s: %s" % (round_number, path, exc)) from exc
