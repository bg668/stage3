"""Coalesce Rich dashboard repaints without changing presentation semantics.

The scheduler is deliberately unaware of reports, terminal layout, or pacing text.  It
only decides whether a dirty frame should be painted now.  This keeps phrase pacing
independent from the much more expensive full-screen Rich repaint.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable


class RenderScheduler:
    """Rate-limit non-critical repaints while allowing explicit forced frames."""

    def __init__(self, *, max_fps: float = 4.0, clock: Callable[[], float] | None = None) -> None:
        if max_fps <= 0:
            raise ValueError("max_fps 必须 > 0")
        self._min_interval = 1.0 / float(max_fps)
        self._clock = clock or time.monotonic
        self._last_render_at: float | None = None
        self._dirty = False
        self._lock = threading.RLock()

    @property
    def dirty(self) -> bool:
        return self._dirty

    def request(self, render: Callable[[], None], *, force: bool = False) -> bool:
        """Mark the frame dirty and render only when the schedule allows it.

        Returns ``True`` when a repaint happened.  Callers use ``force=True`` for
        lifecycle/layout boundaries (new report, completion, shutdown).  Phrase chunks
        use the normal path and are naturally coalesced into at most ``max_fps`` frames.
        """
        with self._lock:
            self._dirty = True
            now = self._clock()
            if not force and self._last_render_at is not None and now - self._last_render_at < self._min_interval:
                return False
            render()
            self._dirty = False
            self._last_render_at = now
            return True

    def flush(self, render: Callable[[], None]) -> bool:
        with self._lock:
            if not self._dirty:
                return False
        return self.request(render, force=True)
