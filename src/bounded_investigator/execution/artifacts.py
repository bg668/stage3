"""Read and write stage artifacts without retaining workflow state."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel

def stage_directory(artifact_dir: Path | None, stage: str) -> Path | None:
    if artifact_dir is None:
        return None
    path = artifact_dir / stage
    path.mkdir(parents=True, exist_ok=True)
    return path


def save_raw_output(artifact_dir: Path | None, stage: str, raw: str) -> str | None:
    stage_dir = stage_directory(artifact_dir, stage)
    if stage_dir is None:
        return None
    path = stage_dir / "raw_output.txt"
    path.write_text(raw, encoding="utf-8")
    return str(path)


def save_model(artifact_dir: Path | None, stage: str, name: str, value: BaseModel) -> str | None:
    stage_dir = stage_directory(artifact_dir, stage)
    if stage_dir is None:
        return None
    path = stage_dir / name
    path.write_text(value.model_dump_json(indent=2), encoding="utf-8")
    return str(path)


def save_sidecar_json(
    artifact_dir: Path | None, group: str, name: str, value: dict[str, Any]
) -> str | None:
    stage_dir = stage_directory(artifact_dir, group)
    if stage_dir is None:
        return None
    path = stage_dir / name
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(path)


def save_validation_errors(
    artifact_dir: Path | None, stage: str, errors: list[dict[str, Any]]
) -> None:
    stage_dir = stage_directory(artifact_dir, stage)
    if stage_dir is None:
        return
    (stage_dir / "validation_errors.json").write_text(
        json.dumps(errors, ensure_ascii=False, indent=2), encoding="utf-8"
    )


