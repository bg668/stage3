"""Persist attempt-level model execution trace without polluting triage.json tasks."""

from __future__ import annotations

import json
import threading
from pathlib import Path

from .domain import AttemptTrace


class TraceRecorder:
    """Append each business attempt immediately so partial runs remain inspectable."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def reset(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("", encoding="utf-8")

    def append(self, trace: AttemptTrace) -> None:
        line = json.dumps(trace.model_dump(), ensure_ascii=False) + "\n"
        with self._lock:
            with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(line)
                handle.flush()


def write_trace(path: Path, traces: list[AttemptTrace]) -> None:
    """Compatibility helper for callers that already have a complete trace list."""
    recorder = TraceRecorder(path)
    recorder.reset()
    for trace in traces:
        recorder.append(trace)
