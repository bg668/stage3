"""Backward-compatible aliases for the Stage-2 reporting subsystem.

New code should import from ``triage.reporting``.  This module exists so callers that
used the V6 activity API do not break during the presentation-layer refactor.
"""

from __future__ import annotations

from pathlib import Path
from typing import TextIO

from .reporting.dialogue_composer import DialogueComposer
from .reporting.events import AgentEvent as ActivityEvent
from .reporting.events import AgentEventSink as ActivitySink
from .reporting.pacing_renderer import PacingRenderer
from .reporting.plain_renderer import PlainRenderer
from .reporting.reporter import Reporter
from .reporting.trace_logger import EventTraceLogger


def render_activity_lines(item: ActivityEvent) -> list[str]:
    """Compatibility projection for callers that used the V6 helper directly."""
    update = DialogueComposer().compose(item)
    return ["%s：%s" % (message.actor, message.text) for message in update.messages]


class ActivityReporter(Reporter):
    """Compatibility constructor matching V6 ``ActivityReporter(path, actor, stream)``."""

    def __init__(
        self,
        path: Path,
        *,
        actor: str = "员工1号",
        stream: TextIO | None = None,
    ) -> None:
        self.path = Path(path)
        self.stream = stream
        presenter = None
        if stream is not None:
            presenter = PacingRenderer(
                DialogueComposer(),
                PlainRenderer(stream),
                pacing_enabled=False,
            )
        super().__init__(EventTraceLogger(path), actor=actor, presenter=presenter)
