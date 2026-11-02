"""Assign display-only sequence numbers and local clock times to reports."""

from __future__ import annotations

from datetime import datetime

from .presentation_model import RealtimeReport, ReportState, TaskBubble


class ReportSequencer:
    def __init__(self) -> None:
        self._sequence = 0

    def from_task(self, bubble: TaskBubble) -> RealtimeReport:
        return self.create(
            timestamp=bubble.timestamp,
            actor=bubble.actor,
            key=bubble.key,
            lines=bubble.lines,
            step_name="",
            importance=bubble.importance,
            state=ReportState.COMPLETE,
        )

    def from_message(
        self, *, timestamp: str, actor: str, text: str, importance: str, step_name: str = ""
    ) -> RealtimeReport:
        return self.create(
            timestamp=timestamp,
            actor=actor,
            key="note:%d" % (self._sequence + 1),
            lines=(text,),
            step_name=step_name,
            importance=importance,
            state=ReportState.COMPLETE,
        )

    def create(
        self,
        *,
        timestamp: str,
        actor: str,
        key: str,
        lines: tuple[str, ...],
        step_name: str = "",
        importance: str = "normal",
        state: ReportState = ReportState.WORKING,
    ) -> RealtimeReport:
        self._sequence += 1
        return RealtimeReport(
            sequence=self._sequence,
            time_text=_local_clock(timestamp),
            actor=actor,
            key=key,
            lines=lines,
            step_name=step_name,
            importance=importance,
            state=state,
        )


def _local_clock(timestamp: str) -> str:
    try:
        dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if dt.tzinfo is not None:
            dt = dt.astimezone()
        return dt.strftime("%H:%M:%S")
    except (TypeError, ValueError):
        return "--:--:--"
