from __future__ import annotations

import json
from pathlib import Path

from .schemas import RunEnvelope


def read_packet(path: str | Path) -> tuple[str, str]:
    path = Path(path)
    return path.stem, path.read_text(encoding="utf-8")


def write_result(path: str | Path, result: RunEnvelope) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(result.model_dump_json(indent=2), encoding="utf-8")


def write_error(path: str | Path, packet_name: str, exc: BaseException) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "packet_name": packet_name,
        "error_type": type(exc).__name__,
        "error": str(exc),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
