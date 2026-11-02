"""Event distribution composition root for Stage-2 reporting."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

from .dialogue_composer import DialogueComposer
from .events import AgentEvent, PresentationSink
from .pacing_renderer import PacingRenderer
from .plain_renderer import PlainRenderer
from .trace_logger import EventTraceLogger


class Reporter:
    """Persist raw facts and fan them out to an optional non-blocking presenter."""

    def __init__(self, logger: EventTraceLogger, *, actor: str = "员工1号", presenter: PresentationSink | None = None) -> None:
        actor = actor.strip()
        if not actor:
            raise ValueError("activity actor 不能为空")
        self.logger = logger
        self.actor = actor
        self.presenter = presenter

    def reset(self) -> None:
        self.logger.reset()
        if self.presenter is not None and hasattr(self.presenter, "start"):
            self.presenter.start()  # type: ignore[attr-defined]

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
        item = AgentEvent(
            timestamp=_utc_now(),
            actor=self.actor,
            event=event,
            stage=stage,
            round=round_number,
            task_id=task_id,
            refs=tuple(refs),
            data=data,
        )
        self.logger.append(item)
        if self.presenter is not None:
            self.presenter.submit(item)

    def close(self) -> None:
        if self.presenter is not None:
            self.presenter.close()


def build_reporter(
    path: Path,
    *,
    actor: str = "员工1号",
    stream: TextIO | None = None,
    prefer_rich: bool = True,
) -> Reporter:
    """Build the CLI stack.

    Rich is the default interactive renderer.  Missing Rich is an explicit downgrade:
    the user receives a one-time installation hint instead of silent fallback.
    Redirected/non-TTY output stays plain by design because a live dashboard has no
    useful semantics there.
    """
    logger = EventTraceLogger(path)
    presenter = None
    if stream is not None:
        is_tty = bool(getattr(stream, "isatty", lambda: False)())
        renderer = None
        if prefer_rich and is_tty:
            try:
                from .rich_renderer import RichRenderer

                renderer = RichRenderer(stream)
            except (ImportError, ModuleNotFoundError):
                _print_rich_fallback_warning(stream)
        if renderer is None:
            renderer = PlainRenderer(stream)
        presenter = PacingRenderer(
            DialogueComposer(),
            renderer,
            pacing_enabled=is_tty,
        )
    return Reporter(logger, actor=actor, presenter=presenter)


def _print_rich_fallback_warning(stream: TextIO) -> None:
    project_managed = Path("pyproject.toml").exists()
    install = "uv add rich" if project_managed else "uv pip install rich"
    print(
        "[提示] 未检测到 Rich，已降级为简化文本模式；任务面板、颜色、按屏幕高度滚动的实时汇报等效果不可用。",
        file=stream,
        flush=True,
    )
    print("       安装 Rich 后重新运行：%s" % install, file=stream, flush=True)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")
