"""Append-only audit records for real model requests.

The recorder is provider-agnostic. Provider adapters opt in via a tiny setter protocol;
scripted/test providers therefore do not pretend to be LLM calls.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol, runtime_checkable


class LLMCallRecorder:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def reset(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("", encoding="utf-8")

    def append(self, record: dict[str, Any]) -> None:
        line = json.dumps(record, ensure_ascii=False) + "\n"
        with self._lock:
            with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(line)
                handle.flush()


@runtime_checkable
class SupportsLLMCallAudit(Protocol):
    def set_call_recorder(self, recorder: LLMCallRecorder | None) -> None:
        ...


def attach_call_recorder(provider: object, recorder: LLMCallRecorder) -> bool:
    if not isinstance(provider, SupportsLLMCallAudit):
        return False
    provider.set_call_recorder(recorder)
    return True


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")
