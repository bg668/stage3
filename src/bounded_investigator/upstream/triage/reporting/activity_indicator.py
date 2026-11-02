"""Low-frequency presentation-only activity animation.

The indicator never changes business state.  A small controller wakes the Rich
renderer only while WORKING reports exist, so animation cannot reintroduce the
high-frequency full-screen repaint loop removed in V14.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable


class ActivityIndicator:
    """Return one fixed-width spinner glyph for the current time."""

    _frames = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")

    def __init__(self, *, frame_interval: float = 1.0, clock: Callable[[], float] | None = None) -> None:
        if frame_interval <= 0:
            raise ValueError("frame_interval 必须 > 0")
        self.frame_interval = float(frame_interval)
        self._clock = clock or time.monotonic

    def frame(self) -> str:
        index = int(self._clock() / self.frame_interval) % len(self._frames)
        return self._frames[index]


class ActivityPulseController:
    """Wake a renderer at a deliberately low rate only while work is active."""

    def __init__(
        self,
        *,
        is_active: Callable[[], bool],
        on_tick: Callable[[], None],
        interval: float = 1.0,
    ) -> None:
        if interval <= 0:
            raise ValueError("interval 必须 > 0")
        self._is_active = is_active
        self._on_tick = on_tick
        self.interval = float(interval)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="triage-rich-pulse", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=max(1.0, self.interval * 2))
        self._thread = None

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            if self._is_active():
                self._on_tick()
