from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError

from .schemas import ArtifactFinding, BehaviorClaim, PartialInvestigationResult

_CODE_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL | re.IGNORECASE)


class FormatRepairFailed(RuntimeError):
    def __init__(
        self,
        stage: str,
        attempts: int,
        last_error: str,
        *,
        last_candidate: str = "",
        validation_errors: list[dict[str, Any]] | None = None,
        required_missing: bool = False,
    ):
        super().__init__(
            f"format repair failed for stage={stage} after {attempts} attempts: {last_error}"
        )
        self.stage = stage
        self.attempts = attempts
        self.last_error = last_error
        self.last_candidate = last_candidate
        self.validation_errors = list(validation_errors or [])
        self.required_missing = required_missing


@dataclass(frozen=True)
class ValidatedCandidate:
    output: BaseModel | None
    error: str | None
    normalized_json: str | None
    data: Any | None = None
    validation_errors: list[dict[str, Any]] | None = None

    @property
    def has_required_missing(self) -> bool:
        return any(item.get("type") == "missing" for item in (self.validation_errors or []))


def exception_chain_text(exc: BaseException) -> str:
    """Render the useful exception chain, including the original Pydantic error when preserved."""
    parts: list[str] = []
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        text = str(cur).strip()
        label = type(cur).__name__
        if text:
            parts.append(f"{label}: {text}")
        else:
            parts.append(label)
        cur = cur.__cause__ or cur.__context__
    return "\ncaused by: ".join(parts)


def _strip_code_fence(text: str) -> str:
    match = _CODE_FENCE_RE.match(text)
    return match.group(1).strip() if match else text.strip()


def _first_json_value(text: str) -> Any:
    """Extract the first decodable JSON object/array from noisy text without mutating values."""
    text = _strip_code_fence(text)
    decoder = json.JSONDecoder()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    for idx, char in enumerate(text):
        if char not in "[{":
            continue
        try:
            value, _ = decoder.raw_decode(text[idx:])
            return value
        except json.JSONDecodeError:
            continue
    raise json.JSONDecodeError("no complete JSON object/array found", text, 0)


def _compact_validation_errors(exc: ValidationError) -> list[dict[str, Any]]:
    """Keep only serializable fields needed for routing and diagnostics."""
    items: list[dict[str, Any]] = []
    for error in exc.errors(include_url=False, include_context=False, include_input=False):
        items.append(
            {
                "type": str(error.get("type", "")),
                "loc": [str(part) for part in error.get("loc", ())],
                "msg": str(error.get("msg", "")),
            }
        )
    return items


def validate_candidate(schema: type[BaseModel], raw_text: str) -> ValidatedCandidate:
    try:
        data = _first_json_value(raw_text)
    except json.JSONDecodeError as exc:
        return ValidatedCandidate(None, f"JSONDecodeError: {exc}", None, None, [])

    try:
        output = schema.model_validate(data)
    except ValidationError as exc:
        normalized = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        return ValidatedCandidate(
            None,
            f"ValidationError: {exc}",
            normalized,
            data,
            _compact_validation_errors(exc),
        )

    normalized = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return ValidatedCandidate(output, None, normalized, data, [])


def salvage_investigation_result(raw_text: str) -> PartialInvestigationResult | None:
    """Salvage only independently valid claims/artifacts from an invalid InvestigationResult.

    This function never repairs values and never invents missing fields. Invalid items are dropped.
    """
    try:
        data = _first_json_value(raw_text)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None

    claims: list[BehaviorClaim] = []
    artifacts: list[ArtifactFinding] = []
    rejected_claims = 0
    rejected_artifacts = 0

    raw_claims = data.get("claims", [])
    if isinstance(raw_claims, list):
        for item in raw_claims:
            try:
                claims.append(BehaviorClaim.model_validate(item))
            except ValidationError:
                rejected_claims += 1

    raw_artifacts = data.get("artifacts", [])
    if isinstance(raw_artifacts, list):
        for item in raw_artifacts:
            try:
                artifacts.append(ArtifactFinding.model_validate(item))
            except ValidationError:
                rejected_artifacts += 1

    if not claims and not artifacts:
        return None

    summary = data.get("summary")
    if not isinstance(summary, str):
        summary = None

    return PartialInvestigationResult(
        summary=summary,
        claims=claims,
        artifacts=artifacts,
        rejected_claims=rejected_claims,
        rejected_artifacts=rejected_artifacts,
    )


def is_repairable_output_failure(exc: BaseException, raw_text: str) -> bool:
    """Only route failures that look like structured-output parsing/validation problems."""
    if not raw_text.strip():
        return False
    chain = exception_chain_text(exc).lower()
    markers = (
        "validationerror",
        "validation error",
        "output retries",
        "toolretryerror",
        "invalid json",
        "jsondecode",
        "structured output",
    )
    return any(marker in chain for marker in markers)
