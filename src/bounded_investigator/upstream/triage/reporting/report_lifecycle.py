"""Display-only report lifecycle for the right-column live feed.

A report key is stable for one visible unit of work.  The first OPEN assigns its
sequence/time; later UPDATE/COMPLETE operations reuse that identity so Rich can
update one bubble in place instead of creating a second completion bubble.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .presentation_model import RealtimeReport, ReportState
from .report_sequencer import ReportSequencer


class ReportOperation(str, Enum):
    OPEN = "OPEN"
    UPDATE = "UPDATE"
    COMPLETE = "COMPLETE"


@dataclass(frozen=True)
class ReportUpdate:
    operation: ReportOperation
    key: str
    timestamp: str
    actor: str
    lines: tuple[str, ...]
    step_name: str = ""
    importance: str = "normal"


class ReportLifecycle:
    """Materialize report updates while preserving display identity per key."""

    def __init__(self, sequencer: ReportSequencer | None = None) -> None:
        self._sequencer = sequencer or ReportSequencer()
        self._headers: dict[str, RealtimeReport] = {}

    def apply(self, update: ReportUpdate) -> RealtimeReport:
        current = self._headers.get(update.key)
        if current is None:
            current = self._sequencer.create(
                timestamp=update.timestamp,
                actor=update.actor,
                key=update.key,
                lines=update.lines,
                step_name=update.step_name,
                importance=update.importance,
            )
        state = ReportState.COMPLETE if update.operation == ReportOperation.COMPLETE else ReportState.WORKING
        report = RealtimeReport(
            sequence=current.sequence,
            time_text=current.time_text,
            actor=update.actor or current.actor,
            key=update.key,
            lines=update.lines,
            step_name=update.step_name or current.step_name,
            importance=update.importance,
            state=state,
        )
        self._headers[update.key] = report
        return report

    def message(
        self,
        *,
        timestamp: str,
        actor: str,
        text: str,
        importance: str,
        step_name: str = "",
    ) -> RealtimeReport:
        report = self._sequencer.from_message(
            timestamp=timestamp,
            actor=actor,
            text=text,
            importance=importance,
            step_name=step_name,
        )
        self._headers[report.key] = report
        return report
