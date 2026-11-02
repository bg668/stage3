"""Durable append-only logging for raw AgentEvent facts."""

from __future__ import annotations

import json
import threading
from pathlib import Path

from .events import AgentEvent


class EventTraceLogger:
    """Persist every raw AgentEvent immediately, independent of UI pacing."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def reset(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("", encoding="utf-8")

    def append(self, event: AgentEvent) -> None:
        line = json.dumps(event.public_dict(), ensure_ascii=False) + "\n"
        with self._lock:
            with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(line)
                handle.flush()
