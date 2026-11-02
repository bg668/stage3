"""Stable provider boundary used by Stage-2 runtime code."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from .domain import ProviderResponse
from .task import TriageRequest


class ProviderSchemaError(ValueError):
    """Provider output could not be parsed into FunctionVerdict objects."""


class ProviderError(RuntimeError):
    """Provider invocation failed before a valid structured response existed.

    ``retryable`` describes whether the business runner may spend another task-level
    attempt on this failure. Provider-runtime recovery uses the same signal to keep
    transport recovery separate from business retries.
    """

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = bool(retryable)


class ProviderUnavailableError(ProviderError):
    """Transient service-level provider failure such as 429/502/503/504/connection loss."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        error_code: str | None = None,
    ) -> None:
        super().__init__(message, retryable=True)
        self.status_code = status_code
        self.error_code = error_code


class ProviderRecoveryError(ProviderError):
    """Provider-runtime recovery budget was exhausted for the current run."""

    def __init__(self, message: str) -> None:
        super().__init__(message, retryable=False)


class ProviderBudgetError(ProviderError):
    """A deterministic per-task usage limit was exceeded."""


class ProviderTimeoutError(ProviderError):
    """The model provider did not return before the configured request deadline."""


ProviderHealthStatus = Literal["OK", "TIMEOUT", "ERROR"]


@dataclass(frozen=True)
class ProviderHealthResult:
    """One provider/model preflight result. This is runtime health, not triage state."""

    status: ProviderHealthStatus
    provider_name: str
    model_profile: str
    model_id: str
    round_number: int
    latency_ms: int
    error: str | None = None


class ProviderProbe(Protocol):
    async def probe(self, *, timeout_seconds: float) -> ProviderHealthResult:
        ...


class TriageProvider(Protocol):
    async def classify(self, request: TriageRequest) -> ProviderResponse:
        ...
