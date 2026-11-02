"""Line-budget policy for representative conclusions in the Rich achievement panel.

AchievementTracker keeps structured candidates; this viewport alone decides which
recent candidates fit the current terminal width.  It does not alter or summarize the
underlying conclusion text.
"""

from __future__ import annotations

from dataclasses import dataclass

from .presentation_model import AchievementHighlight


@dataclass(frozen=True)
class VisibleHighlight:
    ref: str
    lines: tuple[str, ...]


class HighlightViewport:
    def __init__(self, *, total_lines: int = 10, lines_per_item: int = 2) -> None:
        if total_lines < 1:
            raise ValueError("total_lines 必须 >= 1")
        if lines_per_item < 1:
            raise ValueError("lines_per_item 必须 >= 1")
        self.total_lines = int(total_lines)
        self.lines_per_item = int(lines_per_item)

    def visible(self, highlights: tuple[AchievementHighlight, ...], *, console, text_width: int) -> tuple[VisibleHighlight, ...]:
        """Return the newest conclusions that fit the visual line budget.

        Wrapping is delegated to Rich's Text implementation so CJK and wide characters
        use the same width rules as the actual terminal renderer.  The selected items are
        returned oldest-to-newest for stable reading order.
        """
        from rich.text import Text

        width = max(8, int(text_width))
        remaining = self.total_lines
        chosen_reversed: list[VisibleHighlight] = []

        for item in reversed(highlights):
            if remaining <= 0:
                break
            wrapped = list(Text(item.text, style="bright_white").wrap(console, width, overflow="fold"))
            if not wrapped:
                wrapped = [Text("")]
            max_lines = min(self.lines_per_item, remaining)
            visible = wrapped[:max_lines]
            if len(wrapped) > max_lines and visible:
                last = visible[-1].copy()
                last.truncate(max(1, width - 1), overflow="crop", pad=False)
                last.append("…")
                visible[-1] = last
            lines = tuple(line.plain.rstrip() for line in visible)
            chosen_reversed.append(VisibleHighlight(ref=item.ref, lines=lines))
            remaining -= len(lines)

        chosen_reversed.reverse()
        return tuple(chosen_reversed)
