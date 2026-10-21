from __future__ import annotations

from dataclasses import dataclass
from typing import Any, TypeVar

from openai import AsyncOpenAI
from pydantic import BaseModel
from pydantic_ai import Agent, ModelSettings, NativeOutput, PromptedOutput, ToolOutput
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIResponsesModel
from pydantic_ai.providers.deepseek import DeepSeekProvider
from pydantic_ai.providers.openai import OpenAIProvider

from .config import AppConfig, ModelConfig, ProviderConfig, StageName

T = TypeVar("T", bound=BaseModel)


@dataclass(frozen=True)
class StageAgent:
    stage: StageName
    model_key: str
    model_config: ModelConfig
    provider_config: ProviderConfig
    agent: Agent[Any, Any]
    schema: type[BaseModel] | None = None


def _openai_client(config: AppConfig, model_cfg: ModelConfig, provider_cfg: ProviderConfig) -> AsyncOpenAI:
    kwargs: dict[str, Any] = {
        "api_key": config.provider_api_key(model_cfg.provider),
        "timeout": provider_cfg.request_timeout_seconds,
        "max_retries": provider_cfg.sdk_max_retries,
    }
    if provider_cfg.base_url:
        kwargs["base_url"] = provider_cfg.base_url.rstrip("/")
    return AsyncOpenAI(**kwargs)


def build_model(config: AppConfig, model_key: str):
    model_cfg = config.models[model_key]
    provider_cfg = config.providers[model_cfg.provider]
    client = _openai_client(config, model_cfg, provider_cfg)

    if provider_cfg.type == "deepseek":
        provider = DeepSeekProvider(openai_client=client)
    else:
        provider = OpenAIProvider(openai_client=client)

    if model_cfg.api_mode == "responses":
        return OpenAIResponsesModel(model_cfg.model_name, provider=provider)
    return OpenAIChatModel(model_cfg.model_name, provider=provider)


def build_model_settings(
    model_cfg: ModelConfig,
    provider_cfg: ProviderConfig,
    *,
    max_output_tokens: int,
) -> ModelSettings:
    values: dict[str, Any] = {
        "max_tokens": max_output_tokens,
        "timeout": provider_cfg.request_timeout_seconds,
    }
    if model_cfg.temperature is not None:
        values["temperature"] = model_cfg.temperature

    if model_cfg.reasoning == "disabled":
        values["thinking"] = False
    elif model_cfg.reasoning != "omit":
        values["thinking"] = model_cfg.reasoning

    return ModelSettings(**values)


def wrap_output(model_cfg: ModelConfig, schema: type[T]):
    if model_cfg.output_mode == "native":
        return NativeOutput(schema)
    if model_cfg.output_mode == "prompted":
        return PromptedOutput(schema)
    return ToolOutput(schema)


def build_stage_agent(
    *,
    config: AppConfig,
    stage: StageName,
    schema: type[T],
    instructions: str,
) -> StageAgent:
    stage_cfg, model_cfg, provider_cfg = config.stage(stage)
    model_key = stage_cfg.model
    model = build_model(config, model_key)
    # Some OpenAI-compatible JSON-object endpoints reject requests unless the
    # MESSAGES explicitly contain the word "JSON". Output schema metadata alone
    # does not satisfy this requirement (notably PromptedOutput + json_object).
    if model_cfg.output_mode in {"prompted", "native"}:
        instructions = instructions.rstrip() + (
            "\nReturn exactly one valid JSON object matching the required output "
            "schema; do not wrap JSON in Markdown fences."
        )
    agent = Agent(
        model=model,
        instructions=instructions,
        output_type=wrap_output(model_cfg, schema),
        model_settings=build_model_settings(
            model_cfg,
            provider_cfg,
            max_output_tokens=stage_cfg.max_output_tokens,
        ),
        retries={"output": config.workflow.output_retries},
    )
    return StageAgent(
        stage=stage,
        model_key=model_key,
        model_config=model_cfg,
        provider_config=provider_cfg,
        agent=agent,
        schema=schema,
    )


def build_text_stage_agent(
    *,
    config: AppConfig,
    stage: StageName,
    instructions: str,
) -> StageAgent:
    """Build a stateless plain-text stage, used by format repair.

    Repair deliberately avoids structured output so a malformed JSON response cannot
    recursively trigger the same structured-output validation failure.
    """
    stage_cfg, model_cfg, provider_cfg = config.stage(stage)
    model_key = stage_cfg.model
    model = build_model(config, model_key)
    agent = Agent(
        model=model,
        instructions=instructions,
        output_type=str,
        model_settings=build_model_settings(
            model_cfg,
            provider_cfg,
            max_output_tokens=stage_cfg.max_output_tokens,
        ),
        retries={"output": 0},
    )
    return StageAgent(
        stage=stage,
        model_key=model_key,
        model_config=model_cfg,
        provider_config=provider_cfg,
        agent=agent,
        schema=None,
    )
