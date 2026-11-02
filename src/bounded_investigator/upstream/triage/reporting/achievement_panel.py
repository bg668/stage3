"""Stable Rich renderer for the left achievement dashboard.

Each conceptual area owns a fixed-height card derived only from terminal geometry.
Content changes therefore update text in place instead of making lower sections jump.
"""

from __future__ import annotations

from .highlight_viewport import HighlightViewport
from .presentation_model import AchievementState, ProductionStats, WorkState


class AchievementPanelRenderer:
    def __init__(self, console, highlight_viewport: HighlightViewport) -> None:
        self.console = console
        self.highlight_viewport = highlight_viewport

    def render(
        self,
        *,
        work: WorkState | None,
        stats: ProductionStats,
        achievements: AchievementState,
        height: int,
        text_width: int,
    ):
        from rich.console import Group
        from rich.panel import Panel

        sizes = _section_heights(height)
        cards = (
            self._current_card(work, stats, sizes[0]),
            self._stage_card(achievements, stats, sizes[1]),
            self._reason_card(achievements, sizes[2]),
            self._highlight_card(achievements, sizes[3], text_width),
        )
        actor = work.actor if work is not None else "员工1号"
        return Panel(
            Group(*cards),
            title=f"{actor} · 第二阶段成果",
            title_align="left",
            border_style="bold bright_cyan",
            padding=(0, 0),
            height=max(12, int(height)),
        )

    def _current_card(self, work: WorkState | None, stats: ProductionStats, height: int):
        from rich.panel import Panel
        from rich.progress_bar import ProgressBar
        from rich.table import Table
        from rich.text import Text

        body = Table.grid(expand=True, padding=(0, 1))
        body.add_column(style="bright_white", no_wrap=True)
        body.add_column(ratio=1)
        if work is not None:
            phase = Text(work.phase, style="bold bright_cyan")
            body.add_row("阶段", phase)
            if work.note:
                body.add_row("状态", Text(work.note, style="bright_white", overflow="ellipsis", no_wrap=True))
            if work.total:
                body.add_row("进度", Text(f"{work.completed} / {work.total}", style="bold bright_white"))
                if height >= 7:
                    body.add_row("", ProgressBar(total=work.total, completed=work.completed, width=20))
            if work.active_tasks:
                body.add_row("处理中", Text(f"{len(work.active_tasks)} 个模型任务", style="bold bright_yellow"))
        else:
            body.add_row("待定输入", Text(str(stats.input_total), style="bold bright_white"))
        return Panel(body, title="当前状态", title_align="left", border_style="bright_cyan", height=height, padding=(0, 1))

    def _stage_card(self, achievements: AchievementState, stats: ProductionStats, height: int):
        from rich.panel import Panel
        from rich.table import Table
        from rich.text import Text

        body = Table.grid(expand=True, padding=(0, 1))
        body.add_column(ratio=1)
        for stage in achievements.completed_stages[-4:]:
            line = Text()
            line.append("✓ ", style="bold bright_green")
            line.append(stage.label, style="bold green")
            if stage.summary:
                line.append("  " + stage.summary, style="green")
            body.add_row(line)
        if not achievements.completed_stages:
            body.add_row(Text("等待阶段成果…", style="dim"))
        body.add_row(
            Text(
                "累计成果  已收敛 %d/%d  ·  保留 %d  ·  排除 %d"
                % (achievements.converged, stats.input_total, achievements.retained, achievements.excluded),
                style="bright_white",
            )
        )
        body.add_row(
            Text(
                "结论沉淀 %d" % achievements.conclusion_count,
                style="bold bright_green",
            )
        )
        if achievements.pending:
            body.add_row(Text("待处理 %d" % achievements.pending, style="bold bright_yellow"))
        return Panel(body, title="阶段成果", title_align="left", border_style="bright_green", height=height, padding=(0, 1))

    def _reason_card(self, achievements: AchievementState, height: int):
        from rich.panel import Panel
        from rich.table import Table
        from rich.text import Text

        body = Table.grid(expand=True, padding=(0, 1))
        body.add_column(ratio=1)
        body.add_column(width=5, justify="right")
        if achievements.reason_counts:
            max_rows = max(1, height - 2)
            for item in achievements.reason_counts[:max_rows]:
                body.add_row(Text(item.label, style="bright_white"), Text(str(item.count), style="bold bright_magenta"))
        else:
            body.add_row(Text("等待分析方向沉淀…", style="dim"), Text(""))
        return Panel(body, title="发现方向", title_align="left", border_style="bright_magenta", height=height, padding=(0, 1))

    def _highlight_card(self, achievements: AchievementState, height: int, text_width: int):
        from rich.panel import Panel
        from rich.table import Table
        from rich.text import Text

        body = Table.grid(expand=True, padding=(0, 1))
        body.add_column(style="bold yellow", no_wrap=True, width=7)
        body.add_column(ratio=1)
        available_lines = max(1, min(10, height - 2))
        if achievements.highlights:
            viewport = HighlightViewport(total_lines=available_lines, lines_per_item=2)
            visible = viewport.visible(achievements.highlights, console=self.console, text_width=text_width)
            for item in visible:
                body.add_row(Text(item.ref, style="bold yellow"), Text("\n".join(item.lines), style="bright_white"))
        else:
            body.add_row(Text(""), Text("等待代表性结论…", style="dim"))
        return Panel(body, title="代表性结论", title_align="left", border_style="bright_yellow", height=height, padding=(0, 1))


def _section_heights(total_height: int) -> tuple[int, int, int, int]:
    """Return fixed card heights based only on terminal height, never content."""
    inner = max(18, int(total_height) - 2)
    if inner >= 40:
        current, stage, reason = 7, 10, 8
    elif inner >= 31:
        current, stage, reason = 6, 9, 7
    elif inner >= 24:
        current, stage, reason = 5, 8, 6
    else:
        current, stage, reason = 4, 6, 4
    highlights = max(4, inner - current - stage - reason)
    return current, stage, reason, highlights
