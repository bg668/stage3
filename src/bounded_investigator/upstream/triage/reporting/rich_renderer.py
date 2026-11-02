"""Stable Rich two-column dashboard for Stage-2 reporting.

The renderer owns terminal technology only.  It never infers business state from text.
V17 uses an alternate-screen fixed layout, modular achievement cards, a bottom-anchored parallel-analysis region, one latest-result area, and coalesced repainting
so phrase-level pacing does not turn into phrase-level full-screen redraws.
"""

from __future__ import annotations

import threading
from typing import TextIO

from .activity_indicator import ActivityIndicator, ActivityPulseController
from .achievement_panel import AchievementPanelRenderer
from .highlight_viewport import HighlightViewport
from .latest_result import LatestResultBuffer
from .presentation_model import AchievementState, ProductionStats, RealtimeReport, ReportState, TaskBubble, WorkState
from .render_scheduler import RenderScheduler
from .report_viewport import ReportViewport


class RichRenderer:
    def __init__(
        self,
        stream: TextIO,
        *,
        recent_limit: int | None = None,
        history_limit: int = 256,
        highlight_total_lines: int = 10,
        highlight_lines_per_item: int = 2,
        max_refresh_fps: float = 4.0,
        active_pulse_interval: float = 1.0,
    ) -> None:
        from rich.console import Console
        from rich.live import Live

        self._Live = Live
        self.stream = stream
        self.console = Console(file=stream, highlight=False, soft_wrap=True)
        self._live = None
        self._work: WorkState | None = None
        self._stats: ProductionStats | None = None
        self._achievements: AchievementState | None = None
        # recent_limit remains only as a compatibility/testing override.  The normal
        # interactive path leaves it None and uses terminal-height capacity.
        self._viewport = ReportViewport(history_limit=history_limit, fixed_limit=recent_limit)
        self._highlight_viewport = HighlightViewport(
            total_lines=highlight_total_lines,
            lines_per_item=highlight_lines_per_item,
        )
        self._achievement_renderer = AchievementPanelRenderer(self.console, self._highlight_viewport)
        self._scheduler = RenderScheduler(max_fps=max_refresh_fps)
        self._activity_indicator = ActivityIndicator(frame_interval=active_pulse_interval)
        self._state_lock = threading.RLock()
        self._pulse = ActivityPulseController(
            is_active=self._has_active_reports,
            on_tick=self._pulse_tick,
            interval=active_pulse_interval,
        )
        self._editing_key: str | None = None
        self._editing_meta: RealtimeReport | None = None
        self._editing_body = ""
        self._editing_target_state: ReportState | None = None
        self._latest_result = LatestResultBuffer()
        # Full report text is retained only for layout reservation/re-measurement.  The
        # viewport still stores the progressively visible report body.
        self._expected_reports: dict[str, RealtimeReport] = {}
        self._last_console_size: tuple[int, int] | None = None
        self._compat_sequence = 0

    def start(self) -> None:
        if self._live is not None:
            return
        self._last_console_size = self._console_size()
        self._live = self._Live(
            self._render_live(),
            console=self.console,
            screen=True,
            transient=False,
            auto_refresh=False,
            vertical_overflow="crop",
        )
        self._live.start()
        self._pulse.start()

    def set_work(self, work: WorkState | None) -> None:
        with self._state_lock:
            self._work = work
        self._refresh()

    def set_stats(self, stats: ProductionStats | None) -> None:
        with self._state_lock:
            self._stats = stats
        self._refresh()

    def set_achievements(self, achievements: AchievementState | None) -> None:
        with self._state_lock:
            self._achievements = achievements
        self._refresh()

    def set_dashboard(
        self,
        work: WorkState | None,
        stats: ProductionStats | None,
        achievements: AchievementState | None,
    ) -> None:
        """Apply one dashboard snapshot; repaint scheduling remains renderer-local."""
        with self._state_lock:
            self._work = work
            self._stats = stats
            self._achievements = achievements
        self._refresh()

    def set_status(self, text: str | None) -> None:
        with self._state_lock:
            if self._work is None and text:
                self._work = WorkState(actor="员工1号", phase=text, busy=True)
        self._refresh()

    def begin_report(
        self,
        report: RealtimeReport,
        *,
        final_state: ReportState | None = None,
    ) -> None:
        """Open/update one paced report without mixing current work and latest result.

        ``final_state`` is supplied by PacingRenderer from the original lifecycle
        report.  COMPLETE reports own the dedicated latest-result area; WORKING
        reports remain presentation progress only and are represented primarily by
        the fixed current-work slots.
        """
        with self._state_lock:
            self._sync_geometry()
            existing = self._viewport.contains(report.key)
            target_state = final_state if final_state is not None else ReportState.COMPLETE
            self._editing_key = report.key
            self._editing_meta = report
            self._editing_body = ""
            self._editing_target_state = target_state
            self._expected_reports[report.key] = report
            reserved = self._measure_report_height_raw(report)
            self._viewport.reserve_height(report.key, reserved)
            if target_state == ReportState.COMPLETE:
                self._latest_result.begin(report, reserved_height=reserved)

            # Keep legacy viewport state for compatibility/audit-oriented tests.
            # The actual Rich layout renders only COMPLETE history from this store.
            if not existing:
                self._viewport.upsert(_with_lines(report, ()))
            self._prune_expected_reports()
        self._refresh(force=True)

    def append_report_chunk(self, text: str) -> None:
        with self._state_lock:
            if self._editing_meta is None:
                return
            self._editing_body += text
            partial = _with_lines(self._editing_meta, _body_lines(self._editing_body))
            self._viewport.upsert(partial)
            if self._editing_target_state == ReportState.COMPLETE:
                self._latest_result.append(text)
        # Phrase chunks update only the pre-reserved latest-result slot.  They never
        # repaginate current-work slots or completed history.
        self._refresh()

    def finish_report(self, final_report: RealtimeReport | None = None) -> None:
        with self._state_lock:
            if self._editing_meta is None:
                return
            lines = _body_lines(self._editing_body) or self._editing_meta.lines
            target = final_report or self._editing_meta
            target = _with_lines(target, lines)
            self._expected_reports[target.key] = target
            reserved = self._measure_report_height_raw(target)
            self._viewport.reserve_height(target.key, reserved)
            self._viewport.upsert(target)
            if self._editing_target_state == ReportState.COMPLETE:
                self._latest_result.reserve_height(reserved)
                self._latest_result.finish(target)
            self._editing_key = None
            self._editing_meta = None
            self._editing_body = ""
            self._editing_target_state = None
            self._prune_expected_reports()
        self._refresh(force=True)

    # Compatibility wrappers for earlier presentation tests/callers.
    def show_note(self, actor: str, text: str, importance: str = "normal") -> None:
        self._compat_sequence += 1
        report = RealtimeReport(
            sequence=self._compat_sequence,
            time_text="--:--:--",
            actor=actor,
            key="compat-note:%d" % self._compat_sequence,
            lines=(text,),
            importance=importance,
            state=ReportState.COMPLETE,
        )
        self.begin_report(_with_state(report, ReportState.WORKING))
        self.append_report_chunk(text)
        self.finish_report(report)

    def begin_task(self, bubble: TaskBubble) -> None:
        self._compat_sequence += 1
        self.begin_report(
            RealtimeReport(
                sequence=self._compat_sequence,
                time_text="--:--:--",
                actor=bubble.actor,
                key=bubble.key,
                lines=bubble.lines,
                importance=bubble.importance,
                state=ReportState.WORKING,
            )
        )

    def append_task_chunk(self, text: str) -> None:
        self.append_report_chunk(text)

    def finish_task(self) -> None:
        if self._editing_meta is None:
            return
        self.finish_report(_with_state(self._editing_meta, ReportState.COMPLETE))

    def begin_message(self, actor: str) -> None:
        self._compat_actor = actor
        self._compat_text = ""

    def append_chunk(self, text: str) -> None:
        self._compat_text = getattr(self, "_compat_text", "") + text
        self._refresh()

    def finish_message(self) -> None:
        actor = getattr(self, "_compat_actor", None)
        if actor is not None:
            self.show_note(actor, getattr(self, "_compat_text", ""))
            self._compat_actor = None
            self._compat_text = ""

    def stop(self) -> None:
        self.finish_report()
        self._pulse.stop()
        if self._live is not None:
            self._scheduler.flush(self._paint)
            self._live.update(self._render_live(), refresh=True)
            self._live.stop()
            self._live = None

    @property
    def recent_tasks(self) -> tuple[RealtimeReport, ...]:
        return self._viewport.reports

    @property
    def recent_reports(self) -> tuple[RealtimeReport, ...]:
        return self._viewport.reports

    def _refresh(self, *, force: bool = False) -> None:
        if self._live is None:
            return
        geometry_changed = self._sync_geometry()
        self._scheduler.request(self._paint, force=force or geometry_changed)

    def _paint(self) -> None:
        with self._state_lock:
            if self._live is not None:
                self._live.update(self._render_live(), refresh=True)

    def _pulse_tick(self) -> None:
        # Deliberately low-frequency.  The existing scheduler still caps repaint rate,
        # and fixed report slots mean this glyph-only update cannot repaginate the feed.
        self._refresh()

    def _has_active_reports(self) -> bool:
        with self._state_lock:
            latest = self._latest_result.snapshot()
            return bool(
                (self._work is not None and self._work.active_tasks)
                or latest.streaming
            )

    def _console_size(self) -> tuple[int, int]:
        size = self.console.size
        return int(size.width), int(size.height)

    def _sync_geometry(self) -> bool:
        """Re-measure reserved slots only when terminal geometry actually changes."""
        current = self._console_size()
        if self._last_console_size == current:
            return False
        self._last_console_size = current
        for key in self._viewport.keys:
            report = self._expected_reports.get(key)
            if report is not None:
                self._viewport.reserve_height(key, self._measure_report_height_raw(report))
        latest = self._latest_result.snapshot()
        if latest.report is not None:
            self._latest_result.reserve_height(self._measure_report_height_raw(latest.report))
        return True

    def _prune_expected_reports(self) -> None:
        keys = set(self._viewport.keys)
        for key in tuple(self._expected_reports):
            if key not in keys:
                self._expected_reports.pop(key, None)

    def _render_live(self):
        from rich.layout import Layout

        # Layout owns the full alternate screen, so the live region does not grow/shrink
        # with report content and never pushes the PowerShell/terminal scroll buffer.
        layout = Layout(name="root")
        layout.split_row(
            Layout(self._render_achievement_panel(), name="achievement", ratio=2, minimum_size=38),
            Layout(self._render_messages_panel(), name="reports", ratio=3, minimum_size=48),
        )
        return layout

    def _render_achievement_panel(self):
        stats = self._stats or ProductionStats()
        achievements = self._achievements or _achievements_from_stats(stats)
        return self._achievement_renderer.render(
            work=self._work,
            stats=stats,
            achievements=achievements,
            height=max(12, int(self.console.size.height)),
            text_width=self._highlight_text_width_budget(),
        )

    def _render_messages_panel(self):
        from rich.console import Group
        from rich.layout import Layout
        from rich.panel import Panel
        from rich.rule import Rule
        from rich.text import Text

        available_height = self._message_height_budget()
        parallel_height = self._current_work_height()
        feed_height = max(6, available_height - parallel_height)
        latest = self._latest_result.snapshot()
        latest_height = self._latest_result_height(
            latest.reserved_height,
            feed_height,
            current_height=0,
        )
        # The upper feed owns only history + latest result.  The parallel-analysis
        # region is a fixed-size sibling Layout anchored at the bottom, so changes in
        # latest-result height can never move the concurrent-work rows.
        separator_height = 2  # 历史消息 + 最新结果
        feed_panel_overhead = 2  # outer feed panel border
        history_budget = max(0, feed_height - latest_height - separator_height - feed_panel_overhead)

        exclude = (latest.report.key,) if latest.report is not None else ()
        history = self._viewport.visible_history(
            available_height=history_budget,
            measure_height=self._measure_report_height,
            gap_height=0,
            exclude_keys=exclude,
        )

        rows = [Rule("历史消息", style="bright_green")]
        rows.extend(self._render_report_panel(report) for report in history)
        if not history:
            rows.append(Text("暂无历史消息", style="dim"))

        rows.append(Rule("最新结果", style="bright_cyan"))
        if latest.report is not None:
            rows.append(self._render_latest_result_panel(latest.report, latest_height, streaming=latest.streaming))
        else:
            rows.append(Panel(Text("等待最新处理结果…", style="dim"), border_style="dim", height=latest_height, padding=(0, 1)))

        actor = self._work.actor if self._work is not None else "员工1号"
        feed = Panel(
            Group(*rows),
            title="%s · 实时汇报" % actor,
            title_align="left",
            border_style="bold bright_white",
            padding=(0, 1),
        )

        layout = Layout(name="report-column")
        layout.split_column(
            Layout(feed, name="report-feed", ratio=1, minimum_size=6),
            Layout(self._render_current_work_panel(parallel_height), name="parallel-analysis", size=parallel_height),
        )
        return layout

    def _render_latest_result_panel(self, report: RealtimeReport, height: int, *, streaming: bool):
        state = ReportState.WORKING if streaming else report.state
        visible = _with_state(report, state)
        return self._build_report_panel(
            visible,
            fixed_height=height,
            working_label="结果生成中" if streaming else None,
        )

    def _render_current_work_panel(self, height: int):
        from rich.panel import Panel
        from rich.table import Table
        from rich.text import Text

        work = self._work
        capacity = self._current_work_capacity()
        by_slot = {item.slot: item for item in (work.active_tasks if work is not None else ()) if item.slot > 0}
        table = Table.grid(expand=True, padding=(0, 1))
        table.add_column(width=8, no_wrap=True)
        table.add_column(width=12, no_wrap=True)
        table.add_column(ratio=1)

        if not by_slot and work is not None and work.busy:
            # Non-model deterministic work (provider probe / second-hop assembly) gets
            # one truthful current-state row; no fake model task is invented.
            table.add_row(
                Text("%s 当前" % self._activity_indicator.frame(), style="bold bright_yellow"),
                Text(work.phase, style="bright_yellow"),
                Text(work.note or "处理中", style="bright_white", overflow="ellipsis", no_wrap=True),
            )
        else:
            for slot in range(1, capacity + 1):
                item = by_slot.get(slot)
                if item is None:
                    table.add_row(Text("槽位 %d" % slot, style="dim"), Text("", style="dim"), Text("空闲", style="dim"))
                    continue
                table.add_row(
                    Text("%s 槽位 %d" % (self._activity_indicator.frame(), slot), style="bold bright_yellow"),
                    Text(item.step_name or "处理中", style="bright_yellow"),
                    Text("%s · 模型处理中" % item.label, style="bold bright_white", overflow="ellipsis", no_wrap=True),
                )

        return Panel(
            table,
            title="并行分析",
            title_align="left",
            border_style="bold bright_yellow",
            padding=(0, 1),
            height=height,
        )

    def _current_work_capacity(self) -> int:
        configured = self._work.max_concurrency if self._work is not None else 0
        active_max = max((item.slot for item in self._work.active_tasks), default=0) if self._work is not None else 0
        if configured > 0:
            return max(int(configured), int(active_max or 0), 1)
        # Compatibility fallback for manually-constructed WorkState values.  Runtime
        # WorkState always carries the actual round max_concurrency.
        return max(3, int(active_max or 0))

    def _current_work_height(self) -> int:
        # Border lines + one compact row per real concurrency slot.  This height depends
        # only on configured concurrency, never on task text, so the bottom region stays fixed.
        return max(5, self._current_work_capacity() + 2)

    def _latest_result_height(self, reserved: int | None, available_height: int, current_height: int) -> int:
        desired = max(4, int(reserved or 4))
        # Current work is non-negotiable.  Latest result receives the remaining space before
        # history; history may shrink to zero on very small terminals rather than hiding work.
        maximum = max(4, int(available_height) - int(current_height) - 2)
        return min(desired, maximum)

    def _message_height_budget(self) -> int:
        return max(5, int(self.console.size.height) - 4)

    def _message_width_budget(self) -> int:
        total = max(80, int(self.console.size.width))
        return max(42, int((total - 4) * 3 / 5))

    def _highlight_text_width_budget(self) -> int:
        # Left column is 2/5 of the fixed two-column layout.  Reserve room for panel
        # border/padding and the F-xxxx/section-label column; Rich handles CJK wrapping.
        total = max(80, int(self.console.size.width))
        left = max(38, int((total - 4) * 2 / 5))
        return max(12, left - 16)

    def _measure_report_height_raw(self, report: RealtimeReport) -> int:
        panel = self._build_report_panel(report, fixed_height=None)
        options = self.console.options.update(width=self._message_width_budget())
        try:
            return max(3, len(self.console.render_lines(panel, options, pad=False)))
        except Exception:
            return max(3, len(report.lines) + 2)

    def _measure_report_height(self, report: RealtimeReport) -> int:
        reserved = self._viewport.reserved_height(report.key)
        if reserved is not None:
            return reserved
        return self._measure_report_height_raw(report)

    def _render_report_panel(self, report: RealtimeReport):
        return self._build_report_panel(report, fixed_height=self._viewport.reserved_height(report.key))

    def _build_report_panel(
        self,
        report: RealtimeReport,
        *,
        fixed_height: int | None,
        working_label: str | None = None,
    ):
        from rich.panel import Panel
        from rich.text import Text

        body = Text()
        for index, line in enumerate(report.lines):
            if index:
                body.append("\n")
            body.append(line, style="bright_white")
        border = {
            ReportState.WORKING: "bold bright_yellow",
            ReportState.COMPLETE: "bold bright_green",
        }.get(report.state, "bold bright_yellow")
        status_suffix = ""
        if report.state == ReportState.WORKING:
            status_suffix = " · %s %s" % (self._activity_indicator.frame(), working_label or "处理中")
        return Panel(
            body,
            title=Text(
                "#%03d · %s%s%s"
                % (
                    report.sequence,
                    report.time_text,
                    (" · " + report.step_name) if report.step_name else "",
                    status_suffix,
                ),
                style="bold bright_white",
            ),
            title_align="left",
            border_style=border,
            padding=(0, 1),
            height=fixed_height,
        )


def _body_lines(body: str) -> tuple[str, ...]:
    if not body:
        return ()
    lines = body.split("\n")
    return tuple(line for line in lines if line != "")


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


def _achievements_from_stats(stats: ProductionStats) -> AchievementState:
    retained = stats.round1_analyze + stats.round2_analyze
    excluded = stats.round1_skip + stats.round2_skip
    converged = retained + excluded
    return AchievementState(
        converged=converged,
        retained=retained,
        excluded=excluded,
        pending=max(0, stats.input_total - converged),
        conclusion_count=converged,
    )
