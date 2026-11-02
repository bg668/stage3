"""Deterministic provider used to validate the stage-2 application without an LLM."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ..domain import FunctionVerdict, ProviderResponse, Usage
from ..provider import ProviderError, ProviderSchemaError
from ..task import TriageRequest


class ScriptedConfigError(ValueError):
    pass


class ScriptedProvider:
    """Return pre-authored verdicts keyed by unit_id.

    The script is intentionally raw until ``classify`` so malformed model-like output
    exercises the same SCHEMA_FAILED path as a real provider adapter.
    """

    def __init__(self, script: dict[str, list[Any]], model_id: str = "scripted/v1") -> None:
        self._script = script
        self._model_id = model_id

    @classmethod
    def from_path(cls, path: Path) -> "ScriptedProvider":
        try:
            value = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ScriptedConfigError("读取 scripted verdicts 失败: %s" % exc) from exc
        if not isinstance(value, dict):
            raise ScriptedConfigError("scripted verdicts 顶层必须是 {unit_id: verdicts[]}")
        script: dict[str, list[Any]] = {}
        for key, verdicts in value.items():
            if not isinstance(key, str) or not isinstance(verdicts, list):
                raise ScriptedConfigError("scripted verdicts 每项必须是 unit_id -> 数组")
            script[key] = verdicts
        return cls(script)

    async def classify(self, request: TriageRequest) -> ProviderResponse:
        raw = self._script.get(request.task.unit_id)
        if raw is None:
            raise ProviderError("script 中没有 task %s" % request.task.unit_id)
        try:
            verdicts = [FunctionVerdict.model_validate(item) for item in raw]
        except ValidationError as exc:
            raise ProviderSchemaError(str(exc)) from exc
        return ProviderResponse(
            verdicts=verdicts,
            model_id=self._model_id,
            # ScriptedProvider performs no model/API request; do not pollute model usage metrics.
            usage=Usage(),
        )
