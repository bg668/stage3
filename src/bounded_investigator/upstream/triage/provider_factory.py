"""Composition root for task-specific real LLM providers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .domain import TriageConfig
from .llm_config import LLMRegistry, ResolvedModel
from .provider import TriageProvider
from .provider_runtime import ProviderRecoveryPolicy, ResilientTriageProvider
from .providers.pydantic_ai import PydanticAIProvider


@dataclass(frozen=True)
class ProviderSelection:
    provider: TriageProvider
    config: TriageConfig
    resolved: ResolvedModel


@dataclass(frozen=True)
class ProviderPairSelection:
    round1_provider: TriageProvider
    round2_provider: TriageProvider
    config: TriageConfig
    round1_resolved: ResolvedModel
    round2_resolved: ResolvedModel


def build_llm_provider(
    config_path: Path,
    *,
    task_name: str,
    model_profile: str | None = None,
    round_number: Literal[1, 2] = 1,
    max_concurrency: int = 3,
    max_attempts_per_task: int = 2,
    second_hop_max_chars: int = 48_000,
    second_hop_max_related_functions: int = 8,
    second_hop_max_code_chars_per_related_function: int = 12_000,
    provider_probe_timeout_seconds: float = 30.0,
    provider_request_timeout_seconds: float = 120.0,
    provider_recovery_max_attempts: int = 6,
    provider_recovery_initial_backoff_seconds: float = 2.0,
    provider_recovery_max_backoff_seconds: float = 20.0,
) -> ProviderSelection:
    """Resolve one logical task to a concrete model without leaking vendor logic upward."""
    registry = LLMRegistry.from_path(config_path)
    resolved = registry.resolve(task_name, override_profile=model_profile)
    recovery_policy = _recovery_policy(
        max_attempts=provider_recovery_max_attempts,
        initial_backoff_seconds=provider_recovery_initial_backoff_seconds,
        max_backoff_seconds=provider_recovery_max_backoff_seconds,
    )
    provider = _configured_provider(
        resolved,
        round_number,
        provider_request_timeout_seconds,
        recovery_policy,
    )
    effective = _base_config(
        resolved,
        max_concurrency=max_concurrency,
        max_attempts_per_task=max_attempts_per_task,
        second_hop_max_chars=second_hop_max_chars,
        second_hop_max_related_functions=second_hop_max_related_functions,
        second_hop_max_code_chars_per_related_function=(
            second_hop_max_code_chars_per_related_function
        ),
        provider_probe_timeout_seconds=provider_probe_timeout_seconds,
        provider_request_timeout_seconds=provider_request_timeout_seconds,
        recovery_policy=recovery_policy,
    )
    return ProviderSelection(provider=provider, config=effective, resolved=resolved)


def build_llm_provider_pair(
    config_path: Path,
    *,
    round1_task_name: str = "triage.round1",
    round2_task_name: str = "triage.round2",
    round1_model_profile: str | None = None,
    round2_model_profile: str | None = None,
    max_concurrency: int = 3,
    max_attempts_per_task: int = 2,
    second_hop_max_chars: int = 48_000,
    second_hop_max_related_functions: int = 8,
    second_hop_max_code_chars_per_related_function: int = 12_000,
    provider_probe_timeout_seconds: float = 30.0,
    provider_request_timeout_seconds: float = 120.0,
    provider_recovery_max_attempts: int = 6,
    provider_recovery_initial_backoff_seconds: float = 2.0,
    provider_recovery_max_backoff_seconds: float = 20.0,
) -> ProviderPairSelection:
    """Resolve Round1/Round2 independently and add run-time provider protection."""
    registry = LLMRegistry.from_path(config_path)
    r1 = registry.resolve(round1_task_name, override_profile=round1_model_profile)
    r2 = registry.resolve(round2_task_name, override_profile=round2_model_profile)
    recovery_policy = _recovery_policy(
        max_attempts=provider_recovery_max_attempts,
        initial_backoff_seconds=provider_recovery_initial_backoff_seconds,
        max_backoff_seconds=provider_recovery_max_backoff_seconds,
    )
    config = _base_config(
        r1,
        max_concurrency=max_concurrency,
        max_attempts_per_task=max_attempts_per_task,
        second_hop_max_chars=second_hop_max_chars,
        second_hop_max_related_functions=second_hop_max_related_functions,
        second_hop_max_code_chars_per_related_function=(
            second_hop_max_code_chars_per_related_function
        ),
        provider_probe_timeout_seconds=provider_probe_timeout_seconds,
        provider_request_timeout_seconds=provider_request_timeout_seconds,
        recovery_policy=recovery_policy,
    ).model_copy(
        update={
            "round2_enabled": True,
            "round2_provider_name": r2.provider_name,
            "round2_llm_task": r2.task_name,
            "round2_model_profile": r2.profile_name,
            "round2_model_id": r2.model_id,
            "round2_temperature": r2.temperature,
            "round2_reasoning_effort": r2.reasoning_effort,
            "round2_max_output_tokens": r2.max_output_tokens,
            "round2_output_mode": r2.output_mode,
            "round2_usage_limits": r2.usage_limits.model_dump(exclude_none=True),
        }
    )
    return ProviderPairSelection(
        round1_provider=_configured_provider(
            r1, 1, provider_request_timeout_seconds, recovery_policy
        ),
        round2_provider=_configured_provider(
            r2, 2, provider_request_timeout_seconds, recovery_policy
        ),
        config=config,
        round1_resolved=r1,
        round2_resolved=r2,
    )


def _configured_provider(
    resolved: ResolvedModel,
    round_number: Literal[1, 2],
    timeout_seconds: float,
    recovery_policy: ProviderRecoveryPolicy,
) -> TriageProvider:
    provider = PydanticAIProvider(resolved, round_number=round_number)
    _configure_request_timeout(provider, timeout_seconds)
    return ResilientTriageProvider(provider, policy=recovery_policy)


def _configure_request_timeout(provider: object, timeout_seconds: float) -> None:
    setter = getattr(provider, "set_request_timeout", None)
    if callable(setter):
        setter(timeout_seconds)


def _recovery_policy(
    *,
    max_attempts: int,
    initial_backoff_seconds: float,
    max_backoff_seconds: float,
) -> ProviderRecoveryPolicy:
    return ProviderRecoveryPolicy(
        max_attempts=max_attempts,
        initial_backoff_seconds=initial_backoff_seconds,
        max_backoff_seconds=max_backoff_seconds,
    )


def _base_config(
    resolved: ResolvedModel,
    *,
    max_concurrency: int,
    max_attempts_per_task: int,
    second_hop_max_chars: int,
    second_hop_max_related_functions: int,
    second_hop_max_code_chars_per_related_function: int,
    provider_probe_timeout_seconds: float,
    provider_request_timeout_seconds: float,
    recovery_policy: ProviderRecoveryPolicy,
) -> TriageConfig:
    return TriageConfig(
        provider="pydantic-ai",
        provider_name=resolved.provider_name,
        llm_task=resolved.task_name,
        model_profile=resolved.profile_name,
        model_id=resolved.model_id,
        temperature=resolved.temperature,
        reasoning_effort=resolved.reasoning_effort,
        max_output_tokens=resolved.max_output_tokens,
        output_mode=resolved.output_mode,
        max_concurrency=max_concurrency,
        max_attempts_per_task=max_attempts_per_task,
        provider_preflight_enabled=True,
        provider_probe_timeout_seconds=provider_probe_timeout_seconds,
        provider_request_timeout_seconds=provider_request_timeout_seconds,
        provider_recovery_enabled=True,
        provider_recovery_max_attempts=recovery_policy.max_attempts,
        provider_recovery_initial_backoff_seconds=recovery_policy.initial_backoff_seconds,
        provider_recovery_max_backoff_seconds=recovery_policy.max_backoff_seconds,
        usage_limits=resolved.usage_limits.model_dump(exclude_none=True),
        second_hop_max_chars=second_hop_max_chars,
        second_hop_max_related_functions=second_hop_max_related_functions,
        second_hop_max_code_chars_per_related_function=(
            second_hop_max_code_chars_per_related_function
        ),
        round2_enabled=False,
    )
