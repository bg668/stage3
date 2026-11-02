"""Asynchronous presentation with phrase-level pacing and backlog catch-up."""

from __future__ import annotations

import queue
import threading
from dataclasses import dataclass

from .dialogue_composer import DialogueComposer, DialogueMessage
from .events import AgentEvent
from .phrase_chunker import PhraseChunker
from .plain_renderer import OutputRenderer
from .presentation_model import AchievementState, ProductionStats, RealtimeReport, ReportState, TaskBubble, WorkState
from .report_lifecycle import ReportLifecycle, ReportOperation, ReportUpdate


@dataclass(frozen=True)
class PacingConfig:
    phrase_delay: float = 0.045
    comma_delay: float = 0.090
    sentence_delay: float = 0.160
    backlog_soft: int = 3
    backlog_fast: int = 7
    backlog_catch_up: int = 12
    chunk_target_chars: int = 12
    chunk_min_chars: int = 5
    chunk_max_chars: int = 16


class PacingRenderer:
    """Consume AgentEvents on a worker thread so display delays never block analysis."""

    def __init__(
        self,
        composer: DialogueComposer,
        renderer: OutputRenderer,
        *,
        config: PacingConfig | None = None,
        pacing_enabled: bool = True,
    ) -> None:
        self.composer = composer
        # A separate composer updates dashboard state immediately on submit.  The worker
        # composer may intentionally lag because visible text is paced.
        self._status_composer = DialogueComposer()
        self.renderer = renderer
        self.config = config or PacingConfig()
        self.pacing_enabled = pacing_enabled
        self._lifecycle = ReportLifecycle()
        self._chunker = PhraseChunker(
            target_chars=self.config.chunk_target_chars,
            min_chars=self.config.chunk_min_chars,
            max_chars=self.config.chunk_max_chars,
        )
        self._queue: queue.Queue[AgentEvent | object] = queue.Queue()
        self._sentinel = object()
        self._thread: threading.Thread | None = None
        self._closed = False
        self._fast_close = threading.Event()
        self._wake_event = threading.Event()
        self._work_lock = threading.Lock()
        self._latest_work: WorkState | None = None
        self._rendered_work: WorkState | None = None
        self._latest_stats: ProductionStats | None = None
        self._rendered_stats: ProductionStats | None = None
        self._latest_achievements: AchievementState | None = None
        self._rendered_achievements: AchievementState | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self.renderer.start()
        self._thread = threading.Thread(target=self._worker, name="triage-presentation", daemon=True)
        self._thread.start()

    def submit(self, event: AgentEvent) -> None:
        if self._closed:
            return
        if self._thread is None:
            self.start()
        update = self._status_composer.compose(event)
        if update.work is not None or update.stats is not None or update.achievements is not None:
            with self._work_lock:
                if update.work is not None:
                    self._latest_work = update.work
                if update.stats is not None:
                    self._latest_stats = update.stats
                if update.achievements is not None:
                    self._latest_achievements = update.achievements
        self._queue.put_nowait(event)
        self._wake_event.set()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._fast_close.set()
        self._wake_event.set()
        if self._thread is not None:
            self._queue.put(self._sentinel)
            self._thread.join(timeout=10)
        self.renderer.stop()

    def _worker(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is self._sentinel:
                    return
                assert isinstance(item, AgentEvent)
                update = self.composer.compose(item)
                self._apply_latest_dashboard()
                for message in update.messages:
                    if self._should_suppress_message(message):
                        continue
                    self._render_message(message)
                if update.report_updates:
                    for report_update in update.report_updates:
                        self._render_update(report_update)
                elif update.task_bubble is not None:
                    # Compatibility path for older custom composers.
                    self._render_task(update.task_bubble)
            finally:
                self._queue.task_done()

    def _render_message(self, message: DialogueMessage) -> None:
        report = self._lifecycle.message(
            timestamp=message.timestamp,
            actor=message.actor,
            text=message.text,
            importance=message.importance,
            step_name=message.step_name,
        )
        self._render_report(report)

    def _render_update(self, update: ReportUpdate) -> None:
        self._render_report(self._lifecycle.apply(update))

    def _render_task(self, bubble: TaskBubble) -> None:
        update = ReportUpdate(
            operation=ReportOperation.COMPLETE,
            key=bubble.key,
            timestamp=bubble.timestamp,
            actor=bubble.actor,
            lines=bubble.lines,
            step_name="",
            importance=bubble.importance,
        )
        self._render_update(update)

    def _render_report(self, report: RealtimeReport) -> None:
        # OPEN/UPDATE and a COMPLETE result being streamed are both visually WORKING.
        # Only after the final chunk is actually visible do we commit COMPLETE, so border
        # color reflects what the user has seen rather than merely what the model returned.
        working_report = _with_report_state(report, ReportState.WORKING)
        try:
            self.renderer.begin_report(working_report, final_state=report.state)
        except TypeError as exc:
            # Compatibility for external/legacy renderers that implement the V11
            # one-argument begin_report contract.
            if "final_state" not in str(exc):
                raise
            self.renderer.begin_report(working_report)
        text = "\n".join(report.lines)
        for chunk in self._chunker.split(text):
            self._apply_latest_dashboard()
            self.renderer.append_report_chunk(chunk)
            delay = self._delay_for(chunk)
            if delay > 0:
                self._wake_event.wait(delay)
                self._wake_event.clear()
                self._apply_latest_dashboard()
        self.renderer.finish_report(report)

    def _delay_for(self, chunk: str) -> float:
        if not self.pacing_enabled or self._fast_close.is_set():
            return 0.0
        scale = self._backlog_scale()
        if chunk.endswith(("。", "！", "？", ".", "!", "?", "\n")):
            base = self.config.sentence_delay
        elif chunk.endswith(("，", "；", "：", ",", ";", ":")):
            base = self.config.comma_delay
        else:
            base = self.config.phrase_delay
        return base * scale

    def _backlog_scale(self) -> float:
        backlog = self._queue.qsize()
        if backlog >= self.config.backlog_catch_up:
            return 0.0
        if backlog >= self.config.backlog_fast:
            return 0.20
        if backlog >= self.config.backlog_soft:
            return 0.50
        return 1.0

    def _should_suppress_message(self, message: DialogueMessage) -> bool:
        if not self.pacing_enabled or message.importance != "normal":
            return False
        return self._queue.qsize() >= self.config.backlog_catch_up

    def _apply_latest_dashboard(self) -> None:
        with self._work_lock:
            work = self._latest_work
            stats = self._latest_stats
            achievements = self._latest_achievements
        if (
            work != self._rendered_work
            or stats != self._rendered_stats
            or achievements != self._rendered_achievements
        ):
            self.renderer.set_dashboard(work, stats, achievements)
            self._rendered_work = work
            self._rendered_stats = stats
            self._rendered_achievements = achievements


def _with_report_state(report: RealtimeReport, state: ReportState) -> RealtimeReport:
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
