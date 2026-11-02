"""Structured presentation events emitted by Stage-2 runtime code.

AgentEvent is a presentation/audit fact, not business state.  Runtime code may emit
these events, but triage decisions must never depend on them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class AgentEvent:
    timestamp: str
    actor: str
    event: str
    stage: str
    round: int | None = None
    task_id: str | None = None
    refs: tuple[str, ...] = ()
    data: dict[str, Any] = field(default_factory=dict)

    def public_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "actor": self.actor,
            "event": self.event,
            "stage": self.stage,
            "round": self.round,
            "task_id": self.task_id,
            "refs": list(self.refs),
            "data": self.data,
        }


class AgentEventSink(Protocol):
    def report(
        self,
        event: str,
        *,
        stage: str,
        round_number: int | None = None,
        task_id: str | None = None,
        refs: tuple[str, ...] | list[str] = (),
        **data: Any,
    ) -> None:
        ...


class PresentationSink(Protocol):
    def submit(self, event: AgentEvent) -> None:
        ...

    def close(self) -> None:
        ...
