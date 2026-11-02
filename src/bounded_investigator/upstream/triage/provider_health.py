"""Startup health checks for real model providers.

This module coordinates provider probes and persists their results.  It does not own
triage decisions and is intentionally independent from the normal attempt trace.
"""

from __future__ import annotations

import json
import threading
from dataclasses import asdict
from pathlib import Path
from typing import Iterable

from .provider import ProviderHealthResult, TriageProvider
from .reporting.events import AgentEventSink


class ProviderPreflightError(RuntimeError):
    """Raised when one or more required real-model providers fail preflight."""

    def __init__(self, results: Iterable[ProviderHealthResult]) -> None:
        failed = tuple(item for item in results if item.status != "OK")
        self.results = failed
        detail = "; ".join(
            "round%d %s/%s: %s%s"
            % (
                item.round_number,
                item.model_profile,
                item.model_id,
                item.status,
                (" - " + item.error) if item.error else "",
            )
            for item in failed
        )
        super().__init__(detail or "provider preflight failed")


class ProviderHealthRecorder:
    """Append-only JSONL recorder for startup provider health checks."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def reset(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("", encoding="utf-8")

    def append(self, result: ProviderHealthResult) -> None:
        row = asdict(result)
        with self._lock:
            with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
                handle.flush()


async def preflight_providers(
    providers: Iterable[TriageProvider | None],
    *,
    timeout_seconds: float,
    recorder: ProviderHealthRecorder,
    activity: AgentEventSink | None = None,
) -> tuple[ProviderHealthResult, ...]:
    """Probe each configured real provider sequentially before Stage2 work starts."""
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds 必须 > 0")

    results: list[ProviderHealthResult] = []
    seen: set[int] = set()
    for provider in providers:
        if provider is None or id(provider) in seen:
            continue
        seen.add(id(provider))
        probe = getattr(provider, "probe", None)
        if not callable(probe):
            # Preflight is only enabled by composition roots that build a real provider.
            # Missing probe support therefore indicates an integration error, not a
            # sample/task failure.
            result = ProviderHealthResult(
                status="ERROR",
                provider_name=type(provider).__name__,
                model_profile="unknown",
                model_id="unknown",
                round_number=int(getattr(provider, "round_number", 0) or 0),
                latency_ms=0,
                error="provider does not implement probe()",
            )
            recorder.append(result)
            results.append(result)
            _report_completed(activity, result)
            continue

        round_number = int(getattr(provider, "round_number", 0) or 0)
        resolved = getattr(provider, "resolved", None)
        _report(
            activity,
            "provider_probe_started",
            stage="provider_health",
            round_number=round_number or None,
            provider_name=str(getattr(resolved, "provider_name", type(provider).__name__)),
            model_profile=str(getattr(resolved, "profile_name", "unknown")),
            model_id=str(getattr(resolved, "model_id", "unknown")),
            timeout_seconds=float(timeout_seconds),
        )
        try:
            result = await probe(timeout_seconds=float(timeout_seconds))
        except Exception as exc:
            result = ProviderHealthResult(
                status="ERROR",
                provider_name=str(getattr(resolved, "provider_name", type(provider).__name__)),
                model_profile=str(getattr(resolved, "profile_name", "unknown")),
                model_id=str(getattr(resolved, "model_id", "unknown")),
                round_number=round_number,
                latency_ms=0,
                error="%s: %s" % (type(exc).__name__, exc),
            )
        recorder.append(result)
        results.append(result)
        _report_completed(activity, result)

    failed = [item for item in results if item.status != "OK"]
    if failed:
        raise ProviderPreflightError(results)
    return tuple(results)


def _report_completed(activity: AgentEventSink | None, result: ProviderHealthResult) -> None:
    _report(
        activity,
        "provider_probe_completed",
        stage="provider_health",
        round_number=result.round_number or None,
        provider_name=result.provider_name,
        model_profile=result.model_profile,
        model_id=result.model_id,
        status=result.status,
        latency_ms=result.latency_ms,
        error=result.error,
    )


def _report(
    activity: AgentEventSink | None,
    event: str,
    *,
    stage: str,
    round_number: int | None,
    **data: object,
) -> None:
    if activity is None:
        return
    activity.report(event, stage=stage, round_number=round_number, **data)
