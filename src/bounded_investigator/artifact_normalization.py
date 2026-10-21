from __future__ import annotations

"""Lossless normalization of unambiguously enumerated artifacts.

A single ArtifactFinding represents ONE observable. This module never derives
new values from code or infers extra malware behavior. It only splits a plainly
separated list of already emitted values while copying its original provenance.
"""

import re
from collections.abc import Iterable

from .schemas import ArtifactFinding, InvestigationResult, PartialInvestigationResult

_FILE_VALUE = re.compile(r"(?i)(?:^|[/\\])[^/\\\s;,:]+\.[a-z0-9]{1,8}$")


def split_artifact(artifact: ArtifactFinding) -> list[ArtifactFinding]:
    value = artifact.value.strip()
    if ";" not in value and "\n" not in value and (artifact.kind != "file" or "," not in value):
        return [artifact]
    delimiters = r"[;,]|\r?\n" if artifact.kind == "file" else r";|\r?\n"
    fragments = [piece.strip(" \r\t\n\"'") for piece in re.split(delimiters, value)]
    if len(fragments) < 2 or not all(fragments):
        return [artifact]
    # Only split values which can be interpreted as distinct, same-kind items.
    # Do not split registry data, URLs with semicolons, parser markers, etc.
    if artifact.kind == "file":
        if not all(_FILE_VALUE.search(fragment) for fragment in fragments):
            return [artifact]
    elif artifact.kind == "mutex":
        if not all(re.fullmatch(r"[A-Za-z0-9_.$-]+", part) for part in fragments):
            return [artifact]
    elif artifact.kind == "registry":
        if not all(re.match(r"(?i)^(HKLM|HKCU|HKCR|HKU|HKEY_)\\", part) for part in fragments):
            return [artifact]
    else:
        return [artifact]

    return [artifact.model_copy(update={"value": fragment}) for fragment in dict.fromkeys(fragments)]


def normalize_artifacts(artifacts: Iterable[ArtifactFinding]) -> list[ArtifactFinding]:
    result: list[ArtifactFinding] = []
    for artifact in artifacts:
        result.extend(split_artifact(artifact))
    return result


def normalize_investigation_result(
    result: InvestigationResult | PartialInvestigationResult,
) -> InvestigationResult | PartialInvestigationResult:
    """Return a copy with atomic artifacts; leave claims/evidence untouched."""
    split = normalize_artifacts(result.artifacts)
    if len(split) == len(result.artifacts) and all(
        before is after for before, after in zip(result.artifacts, split)
    ):
        return result
    return result.model_copy(update={"artifacts": split})
