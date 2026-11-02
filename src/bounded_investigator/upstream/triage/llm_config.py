"""Model/provider registry for task-specific LLM selection.

The triage domain knows only a logical task name such as ``triage.round1``.
This module resolves that task to a named model profile and keeps vendor/provider
configuration out of the runner and application service.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class LLMConfigError(ValueError):
    """The LLM registry is missing, malformed, or internally inconsistent."""


ProviderKind = Literal["pydantic-native", "openai-compatible"]
OutputMode = Literal["tool", "native", "prompted"]
ReasoningEffort = Literal["none", "minimal", "low", "medium", "high", "xhigh"]


class ProviderSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: ProviderKind
    base_url: str | None = None
    api_key_env: str | None = None

    @model_validator(mode="after")
    def validate_kind_fields(self) -> "ProviderSpec":
        if self.kind == "openai-compatible":
            if not self.base_url:
                raise ValueError("openai-compatible provider 必须配置 base_url")
            if not self.api_key_env:
                raise ValueError("openai-compatible provider 必须配置 api_key_env")
        elif self.base_url or self.api_key_env:
            raise ValueError(
                "pydantic-native 使用 PydanticAI 对应厂商的标准认证环境变量，"
                "不能配置 base_url/api_key_env；自定义端点请用 openai-compatible"
            )
        return self


class UsageLimitsSpec(BaseModel):
    """Shared registry limits. Stage-specific runtimes enforce their own request semantics."""

    model_config = ConfigDict(extra="forbid")

    # llm.json is shared by Stage2 and Stage3. Stage3 needs request_limit > 1
    # for its agent/tool loop, while Stage2 still hard-enforces one model request
    # per classification attempt inside its provider runtime.
    request_limit: int = Field(default=1, ge=1)
    input_tokens_limit: int | None = Field(default=None, ge=1)
    output_tokens_limit: int | None = Field(default=None, ge=1)
    total_tokens_limit: int | None = Field(default=None, ge=1)


class ModelProfileSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str
    model: str = Field(min_length=1)
    temperature: float = 0.0
    reasoning_effort: ReasoningEffort | None = None
    max_output_tokens: int | None = Field(default=None, ge=1)
    output_mode: OutputMode = "tool"
    usage_limits: UsageLimitsSpec = Field(default_factory=UsageLimitsSpec)


class LLMRegistrySpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    providers: dict[str, ProviderSpec]
    models: dict[str, ModelProfileSpec]
    tasks: dict[str, str]

    @model_validator(mode="after")
    def validate_references(self) -> "LLMRegistrySpec":
        if not self.providers:
            raise ValueError("providers 不能为空")
        if not self.models:
            raise ValueError("models 不能为空")
        if not self.tasks:
            raise ValueError("tasks 不能为空")

        for profile_name, profile in self.models.items():
            provider = self.providers.get(profile.provider)
            if provider is None:
                raise ValueError(
                    "model profile %s 引用了不存在的 provider %s"
                    % (profile_name, profile.provider)
                )
            if provider.kind == "pydantic-native" and ":" not in profile.model:
                raise ValueError(
                    "pydantic-native model %s 必须使用带厂商前缀的 PydanticAI model spec，"
                    "例如 anthropic:claude-... 或 openai-chat:gpt-..." % profile_name
                )
            if (
                profile.reasoning_effort is not None
                and provider.kind == "pydantic-native"
                and not profile.model.startswith(("openai:", "openai-chat:", "openai-responses:"))
            ):
                raise ValueError(
                    "model profile %s 配置了 reasoning_effort，但该字段仅适用于 "
                    "openai-compatible 或 PydanticAI OpenAI 模型" % profile_name
                )

        for task_name, profile_name in self.tasks.items():
            if profile_name not in self.models:
                raise ValueError(
                    "task %s 引用了不存在的 model profile %s"
                    % (task_name, profile_name)
                )
        return self


class ResolvedModel(BaseModel):
    """Immutable, secret-free effective model configuration for one logical task."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    task_name: str
    profile_name: str
    provider_name: str
    provider_kind: ProviderKind
    model: str
    base_url: str | None = None
    api_key_env: str | None = None
    temperature: float = 0.0
    reasoning_effort: ReasoningEffort | None = None
    max_output_tokens: int | None = None
    output_mode: OutputMode = "tool"
    usage_limits: UsageLimitsSpec

    @property
    def model_id(self) -> str:
        if self.provider_kind == "pydantic-native":
            return self.model
        return "%s/%s" % (self.provider_name, self.model)

    def public_dict(self) -> dict:
        # api_key_env is safe to persist; the secret value is never loaded here.
        return self.model_dump()


class LLMRegistry:
    def __init__(self, spec: LLMRegistrySpec, *, source: Path | None = None) -> None:
        self.spec = spec
        self.source = source

    @classmethod
    def from_path(cls, path: Path) -> "LLMRegistry":
        path = Path(path)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise LLMConfigError("读取 LLM 配置失败 %s: %s" % (path, exc)) from exc
        try:
            spec = LLMRegistrySpec.model_validate(raw)
        except Exception as exc:
            raise LLMConfigError("LLM 配置非法 %s: %s" % (path, exc)) from exc
        return cls(spec, source=path)

    def resolve(self, task_name: str, *, override_profile: str | None = None) -> ResolvedModel:
        profile_name = override_profile or self.spec.tasks.get(task_name)
        if not profile_name:
            raise LLMConfigError("LLM 配置没有 task 映射: %s" % task_name)
        profile = self.spec.models.get(profile_name)
        if profile is None:
            raise LLMConfigError("model profile 不存在: %s" % profile_name)
        provider = self.spec.providers[profile.provider]
        return ResolvedModel(
            task_name=task_name,
            profile_name=profile_name,
            provider_name=profile.provider,
            provider_kind=provider.kind,
            model=profile.model,
            base_url=provider.base_url,
            api_key_env=provider.api_key_env,
            temperature=profile.temperature,
            reasoning_effort=profile.reasoning_effort,
            max_output_tokens=profile.max_output_tokens,
            output_mode=profile.output_mode,
            usage_limits=profile.usage_limits,
        )
