"""Run-scoped protection for transient provider outages.

This module owns transport availability recovery. Business runners still own task-level
schema/id/budget retry semantics and only see a terminal provider failure when the
shared recovery budget is exhausted.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from .domain import ProviderResponse
from .provider import (
    ProviderHealthResult,
    ProviderRecoveryError,
    ProviderUnavailableError,
    TriageProvider,
)
from .reporting.events import AgentEventSink
from .task import TriageRequest


@dataclass(frozen=True)
class ProviderRecoveryPolicy:
    """Bounded exponential backoff for one provider instance/run."""

    max_attempts: int = 6
    initial_backoff_seconds: float = 2.0
    max_backoff_seconds: float = 20.0

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("provider recovery max_attempts 必须 >= 1")
        if self.initial_backoff_seconds <= 0:
            raise ValueError("provider recovery initial_backoff_seconds 必须 > 0")
        if self.max_backoff_seconds <= 0:
            raise ValueError("provider recovery max_backoff_seconds 必须 > 0")
        if self.max_backoff_seconds < self.initial_backoff_seconds:
            raise ValueError(
                "provider recovery max_backoff_seconds 不能小于 initial_backoff_seconds"
            )

    def delay_for(self, attempt: int) -> float:
        if attempt < 1:
            raise ValueError("attempt 必须 >= 1")
        return min(
            self.initial_backoff_seconds * (2 ** (attempt - 1)),
            self.max_backoff_seconds,
        )


@dataclass(frozen=True)
class _RecoveryOutcome:
    response: ProviderResponse | None = None
    error: Exception | None = None


class ResilientTriageProvider:
    """Decorator that converts a provider-wide outage into one shared recovery flow.

    The first transient availability failure opens a run-scoped recovery gate. One
    request becomes the recovery leader and retries with exponential backoff; all other
    tasks wait behind the same gate instead of consuming their own business attempts.
    A successful replay closes the gate. Exhaustion produces one non-retryable provider
    error so the existing Round2 fail-open policy remains the final safety net.
    """

    def __init__(
        self,
        provider: TriageProvider,
        *,
        policy: ProviderRecoveryPolicy | None = None,
    ) -> None:
        self._provider = provider
        self.policy = policy or ProviderRecoveryPolicy()
        self._activity: AgentEventSink | None = None
        self._state_lock = asyncio.Lock()
        self._recovery_task: asyncio.Task[_RecoveryOutcome] | None = None
        self._terminal_error: ProviderRecoveryError | None = None

    @property
    def round_number(self) -> int:
        return int(getattr(self._provider, "round_number", 0) or 0)

    @property
    def resolved(self) -> Any | None:
        return getattr(self._provider, "resolved", None)

    @property
    def inner_provider(self) -> TriageProvider:
        return self._provider

    def set_activity_sink(self, activity: AgentEventSink | None) -> None:
        self._activity = activity

    def set_call_recorder(self, recorder: Any | None) -> None:
        setter = getattr(self._provider, "set_call_recorder", None)
        if callable(setter):
            setter(recorder)

    def set_request_timeout(self, timeout_seconds: float) -> None:
        setter = getattr(self._provider, "set_request_timeout", None)
        if callable(setter):
            setter(timeout_seconds)

    async def probe(self, *, timeout_seconds: float) -> ProviderHealthResult:
        probe = getattr(self._provider, "probe", None)
        if not callable(probe):
            raise RuntimeError("wrapped provider does not implement probe()")
        return await probe(timeout_seconds=timeout_seconds)

    async def classify(self, request: TriageRequest) -> ProviderResponse:
        await self._wait_for_active_recovery()
        try:
            return await self._provider.classify(request)
        except ProviderUnavailableError as exc:
            return await self._recover(request, exc)

    async def _wait_for_active_recovery(self) -> None:
        async with self._state_lock:
            terminal = self._terminal_error
            recovery = self._recovery_task
        if terminal is not None:
            raise terminal
        if recovery is not None:
            await recovery

    async def _recover(
        self,
        request: TriageRequest,
        first_error: ProviderUnavailableError,
    ) -> ProviderResponse:
        async with self._state_lock:
            if self._terminal_error is not None:
                raise self._terminal_error
            recovery = self._recovery_task
            leader = recovery is None
            if recovery is None:
                recovery = asyncio.create_task(
                    self._run_recovery(request, first_error),
                    name="triage-provider-recovery-r%d" % self.round_number,
                )
                self._recovery_task = recovery

        outcome = await recovery
        if leader:
            if outcome.error is not None:
                raise outcome.error
            assert outcome.response is not None
            return outcome.response

        # This task already failed while the leader was recovering. Once the shared
        # gate closes, replay only this task; another availability failure starts a new
        # bounded incident rather than spending a business-level attempt.
        return await self.classify(request)

    async def _run_recovery(
        self,
        request: TriageRequest,
        first_error: ProviderUnavailableError,
    ) -> _RecoveryOutcome:
        current = asyncio.current_task()
        last_error = first_error
        self._report(
            "provider_degraded",
            request,
            error=str(first_error),
            status_code=first_error.status_code,
            error_code=first_error.error_code,
            max_recovery_attempts=self.policy.max_attempts,
        )
        try:
            for recovery_attempt in range(1, self.policy.max_attempts + 1):
                delay = self.policy.delay_for(recovery_attempt)
                self._report(
                    "provider_recovery_waiting",
                    request,
                    recovery_attempt=recovery_attempt,
                    max_recovery_attempts=self.policy.max_attempts,
                    delay_seconds=delay,
                )
                await asyncio.sleep(delay)
                try:
                    response = await self._provider.classify(request)
                except ProviderUnavailableError as exc:
                    last_error = exc
                    self._report(
                        "provider_recovery_attempt_failed",
                        request,
                        recovery_attempt=recovery_attempt,
                        status_code=exc.status_code,
                        error_code=exc.error_code,
                        error=str(exc),
                    )
                    continue
                except Exception as exc:
                    # The provider is answering again. Preserve the leader task's
                    # original non-availability outcome for the business runner while
                    # releasing every other task waiting behind the provider gate.
                    self._report(
                        "provider_recovered",
                        request,
                        recovery_attempt=recovery_attempt,
                        replay_status=type(exc).__name__,
                    )
                    return _RecoveryOutcome(error=exc)

                self._report(
                    "provider_recovered",
                    request,
                    recovery_attempt=recovery_attempt,
                    replay_status="OK",
                )
                return _RecoveryOutcome(response=response)

            terminal = ProviderRecoveryError(
                "provider recovery exhausted after %d attempts: %s"
                % (self.policy.max_attempts, last_error)
            )
            async with self._state_lock:
                self._terminal_error = terminal
            self._report(
                "provider_recovery_exhausted",
                request,
                attempts=self.policy.max_attempts,
                error=str(last_error),
                status_code=last_error.status_code,
                error_code=last_error.error_code,
            )
            raise terminal
        finally:
            async with self._state_lock:
                if self._recovery_task is current:
                    self._recovery_task = None

    def _report(self, event: str, request: TriageRequest, **data: object) -> None:
        if self._activity is None:
            return
        self._activity.report(
            event,
            stage="provider_runtime",
            round_number=request.task.round,
            task_id=request.task.task_id,
            refs=request.task.subject_refs,
            provider_name=str(
                getattr(self.resolved, "provider_name", type(self._provider).__name__)
            ),
            model_profile=str(getattr(self.resolved, "profile_name", "unknown")),
            model_id=str(getattr(self.resolved, "model_id", "unknown")),
            **data,
        )
