"""Presentation-only state for the single latest detailed result.

The worker may have many model requests in flight, but detailed phrase-level output is
serialized.  This buffer owns only the latest visible result body and its pre-measured
stable height; completed history remains in ReportViewport.
"""

from __future__ import annotations

from dataclasses import dataclass

from .presentation_model import RealtimeReport, ReportState


@dataclass(frozen=True)
class LatestResultSnapshot:
    report: RealtimeReport | None
    reserved_height: int | None
    streaming: bool


class LatestResultBuffer:
    def __init__(self) -> None:
        self._meta: RealtimeReport | None = None
        self._body = ""
        self._reserved_height: int | None = None
        self._streaming = False

    def begin(self, report: RealtimeReport, *, reserved_height: int) -> None:
        self._meta = report
        self._body = ""
        self._reserved_height = max(3, int(reserved_height))
        self._streaming = True

    def append(self, text: str) -> None:
        if self._meta is None:
            return
        self._body += text

    def reserve_height(self, height: int) -> None:
        if self._meta is not None:
            self._reserved_height = max(3, int(height))

    def finish(self, final_report: RealtimeReport) -> RealtimeReport:
        lines = _body_lines(self._body) or final_report.lines
        self._meta = _with_lines(final_report, lines)
        self._body = "\n".join(lines)
        self._streaming = False
        return self._meta

    def snapshot(self) -> LatestResultSnapshot:
        report = self._meta
        if report is not None and self._streaming:
            report = _with_lines(report, _body_lines(self._body))
            report = _with_state(report, ReportState.WORKING)
        return LatestResultSnapshot(report=report, reserved_height=self._reserved_height, streaming=self._streaming)

    @property
    def key(self) -> str | None:
        return self._meta.key if self._meta is not None else None


def _body_lines(body: str) -> tuple[str, ...]:
    if not body:
        return ()
    return tuple(line for line in body.split("\n") if line != "")


def _with_lines(report: RealtimeReport, lines: tuple[str, ...]) -> RealtimeReport:
    return RealtimeReport(
        sequence=report.sequence,
        time_text=report.time_text,
        actor=report.actor,
        key=report.key,
        lines=lines,
        step_name=report.step_name,
        importance=report.importance,
        state=report.state,
    )


def _with_state(report: RealtimeReport, state: ReportState) -> RealtimeReport:
    return RealtimeReport(
        sequence=report.sequence,
        time_text=report.time_text,
        actor=report.actor,
        key=report.key,
        lines=report.lines,
        step_name=report.step_name,
        importance=report.importance,
        state=state,
    )
