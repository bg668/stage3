"""Plain-text renderer used when Rich is unavailable/disabled or output is redirected."""

from __future__ import annotations

import threading
from typing import Protocol, TextIO

from .presentation_model import AchievementState, ProductionStats, RealtimeReport, ReportState, TaskBubble, WorkState


class OutputRenderer(Protocol):
    def start(self) -> None: ...
    def set_work(self, work: WorkState | None) -> None: ...
    def set_stats(self, stats: ProductionStats | None) -> None: ...
    def set_achievements(self, achievements: AchievementState | None) -> None: ...
    def set_dashboard(
        self, work: WorkState | None, stats: ProductionStats | None, achievements: AchievementState | None
    ) -> None: ...
    def begin_report(self, report: RealtimeReport, *, final_state: ReportState | None = None) -> None: ...
    def append_report_chunk(self, text: str) -> None: ...
    def finish_report(self, final_report: RealtimeReport | None = None) -> None: ...
    def stop(self) -> None: ...


class PlainRenderer:
    """Compact non-interactive fallback; it never emulates the Rich dashboard."""

    def __init__(self, stream: TextIO) -> None:
        self.stream = stream
        self._lock = threading.Lock()
        self._report: RealtimeReport | None = None
        self._body = ""

    def start(self) -> None:
        return None

    def set_work(self, work: WorkState | None) -> None:
        return None

    def set_stats(self, stats: ProductionStats | None) -> None:
        return None

    def set_achievements(self, achievements: AchievementState | None) -> None:
        return None

    def set_dashboard(
        self,
        work: WorkState | None,
        stats: ProductionStats | None,
        achievements: AchievementState | None,
    ) -> None:
        return None

    def set_status(self, text: str | None) -> None:
        return None

    def begin_report(self, report: RealtimeReport, *, final_state: ReportState | None = None) -> None:
        self._report = report
        self._body = ""

    def append_report_chunk(self, text: str) -> None:
        self._body += text

    def finish_report(self, final_report: RealtimeReport | None = None) -> None:
        if self._report is None:
            return
        with self._lock:
            body = self._body.strip()
            lines = body.splitlines() if body else list(self._report.lines)
            if lines:
                print(
                    "#%03d %s%s %s：%s"
                    % (
                        self._report.sequence,
                        self._report.time_text,
                        (" · " + self._report.step_name) if self._report.step_name else "",
                        self._report.actor,
                        lines[0],
                    ),
                    file=self.stream,
                    flush=True,
                )
                for line in lines[1:]:
                    print("  %s" % line, file=self.stream, flush=True)
            else:
                print("#%03d %s %s" % (self._report.sequence, self._report.time_text, self._report.actor), file=self.stream, flush=True)
        self._report = None
        self._body = ""

    # Compatibility wrappers retained for tests/older callers.
    def show_note(self, actor: str, text: str, importance: str = "normal") -> None:
        with self._lock:
            print("%s：%s" % (actor, text), file=self.stream, flush=True)

    def begin_task(self, bubble: TaskBubble) -> None:
        self._compat_bubble = bubble
        self._body = ""

    def append_task_chunk(self, text: str) -> None:
        self._body += text

    def finish_task(self) -> None:
        bubble = getattr(self, "_compat_bubble", None)
        if bubble is None:
            return
        with self._lock:
            body = self._body.strip() or "\n".join(bubble.lines)
            for line in body.splitlines():
                print("%s：%s" % (bubble.actor, line), file=self.stream, flush=True)
        self._compat_bubble = None
        self._body = ""

    def begin_message(self, actor: str) -> None:
        self._compat_actor = actor
        self._compat_text = ""

    def append_chunk(self, text: str) -> None:
        self._compat_text = getattr(self, "_compat_text", "") + text

    def finish_message(self) -> None:
        actor = getattr(self, "_compat_actor", None)
        if actor is not None:
            self.show_note(actor, getattr(self, "_compat_text", ""))
            self._compat_actor = None
            self._compat_text = ""

    def stop(self) -> None:
        self.finish_report()
        self.finish_task()
        self.finish_message()
