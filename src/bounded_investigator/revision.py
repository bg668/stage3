from __future__ import annotations

"""Apply only explicitly reviewed edits to a copy of the Analyzer result.

The model still returns a full InvestigationResult. Paths always address the
original lists; only declared deletions compact them, and new items go last.
This is deliberately not a general JSON Patch implementation.
"""

import json
import re
from collections import Counter
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from .artifact_normalization import split_artifact
from .evidence_validation import CodeIndex, validate_investigation_result
from .schemas import CoverageReview, EvidenceLevel, InvestigationResult

_COLLECTIONS = ("claims", "artifacts", "gaps", "handoffs", "material_needs")
_TOKEN = re.compile(r"[a-z_]+|0|[1-9][0-9]*|\*")
ResultPath = tuple[str | int, ...]


@dataclass
class RevisionPlan:
    replacements: set[ResultPath]
    deletions: dict[str, set[int]]
    additions: dict[str, set[int]]


@dataclass
class RevisionApplication:
    result: InvestigationResult
    audit: dict[str, Any]


def _path_text(path: ResultPath) -> str:
    return "/" + "/".join(map(str, path))


def _tokens(path: str) -> ResultPath:
    if not path.startswith("/"):
        raise ValueError(f"invalid revision path: {path}")
    parts = path[1:].split("/")
    if not all(_TOKEN.fullmatch(part) for part in parts):
        raise ValueError(f"invalid revision path: {path}")
    return tuple(int(part) if part.isdigit() else part for part in parts)


def _expand(value: Any, parts: ResultPath, prefix: ResultPath = ()) -> set[ResultPath]:
    if not parts:
        return {prefix}
    key, *rest = parts
    if isinstance(value, list):
        if key == "*":
            return {p for i, child in enumerate(value) for p in _expand(child, tuple(rest), (*prefix, i))}
        if isinstance(key, int) and key < len(value):
            return _expand(value[key], tuple(rest), (*prefix, key))
    elif isinstance(value, dict) and key in value:
        return _expand(value[key], tuple(rest), (*prefix, key))
    raise ValueError(f"revision path does not exist: {_path_text((*prefix, *parts))}")


# These field maps are also rendered into both role prompts.
_SEMANTIC_FIELDS = {
    "claim_revision": ("behavior", "condition", "source", "action", "sink", "confidence", "evidence_level", "limitations"),
    "artifact_classification": ("kind", "role", "detection_specificity", "meaning"),
}
_EVIDENCE_OWNERS = ("claims", "artifacts", "material_needs")
_GAP_OWNERS = ("gaps", "handoffs", "material_needs")


def revision_policy_context() -> str:
    return "\n".join([
        "修正类型与程序固定范围（每种类型只作用于自身 targets；多种类型取并集）：",
        "- reference_format：/claims/N、/artifacts/N、/material_needs/N 的全部 evidence.location；也可精确选择 /claims/N/evidence/M 等证据对象。仅此类型支持 /claims/*、/artifacts/*、/material_needs/* 批量选原条目。不能改 observation。",
        "- evidence_revision：精确选择上述证据所有者，允许替换其 evidence 列表（含增删拆分）；或精确选择其 /evidence/M，允许修改该 location/observation 对。无通配符。",
        *[f"- {kind}：精确选择 /{'claims' if kind == 'claim_revision' else 'artifacts'}/N，允许修改 {', '.join(fields)}" +
          ("，并联动 summary。证据需要另选 evidence_revision/reference_format。" if kind == "claim_revision" else "。value/confidence 和证据保持原值。")
          for kind, fields in _SEMANTIC_FIELDS.items()],
        "- gap_revision：精确选择 /gaps/N、/handoffs/N、/material_needs/N，允许修改整个对象及 summary/status。",
        "- append_item：选择任一顶层列表的原长度槽位 /集合/N；多个新增槽位必须连续。允许新增整个条目及联动 summary/status。",
        "- delete_item：精确选择任一顶层列表原有条目 /集合/N；允许删除及联动 summary/status。",
        "所有 N/M 均为原结果的零基编号。不得填写字段路径、整个结果或任意权限路径。仅 reference_format/evidence_revision 可精确选择 evidence/M 子对象。",
        "批量引用格式修正自动跳过已删除条目；显式针对同一条目修正又删除视为冲突。需同步修改分类/行为/证据时组合相应类型。",
    ])


def validate_revision_scope(draft: InvestigationResult, review: CoverageReview) -> RevisionPlan:
    """Compile correction intent into permissions, failing closed before a model call."""
    if not review.requires_revision:
        raise ValueError("review does not request revision")
    original = draft.model_dump(mode="json")
    plan = RevisionPlan(set(), {k: set() for k in _COLLECTIONS}, {k: set() for k in _COLLECTIONS})
    bulk: set[ResultPath] = set()
    explicit: set[ResultPath] = set()
    explicit_objects: set[ResultPath] = set()
    for issue in review.issues:
        if issue.scope != "packet_internal":
            continue
        if issue.severity in {"major", "critical"} and not issue.fixes:
            raise ValueError(f"{issue.issue_id}: missing fixes")
        for fix in issue.fixes:
            for target in fix.targets:
                parts = _tokens(target)
                root = parts[0]
                item = len(parts) == 2 and isinstance(parts[1], int)
                evidence = (len(parts) == 4 and isinstance(parts[1], int)
                            and parts[2] == "evidence" and isinstance(parts[3], int))
                batch = len(parts) == 2 and parts[1] == "*"
                if fix.type in {"reference_format", "evidence_revision"}:
                    if root not in _EVIDENCE_OWNERS or not (item or evidence or (batch and fix.type == "reference_format")):
                        raise ValueError(f"invalid target for {fix.type}: {target}")
                    # Even an empty evidence list must have a valid owner.
                    _expand(original, parts)
                    if not batch:
                        explicit_objects.add(parts[:2])
                    if fix.type == "reference_format":
                        suffix = ("location",) if evidence else ("evidence", "*", "location")
                        (bulk if batch else explicit).update(_expand(original, (*parts, *suffix)))
                    else:
                        explicit.update(_expand(original, parts if evidence else (*parts, "evidence")))
                elif fix.type in _SEMANTIC_FIELDS:
                    expected_root = "claims" if fix.type == "claim_revision" else "artifacts"
                    if root != expected_root or not item:
                        raise ValueError(f"invalid target for {fix.type}: {target}")
                    _expand(original, parts)
                    explicit_objects.add(parts)
                    explicit.update((*parts, field) for field in _SEMANTIC_FIELDS[fix.type])
                    if fix.type == "claim_revision":
                        explicit.add(("summary",))
                elif fix.type == "gap_revision":
                    if root not in _GAP_OWNERS or not item:
                        raise ValueError(f"invalid target for {fix.type}: {target}")
                    explicit.update(_expand(original, parts))
                    explicit_objects.add(parts)
                    explicit.update({("summary",), ("status",)})
                else:
                    if root not in _COLLECTIONS or not item:
                        raise ValueError(f"append/delete requires one top-level list slot: {target}")
                    index = parts[1]
                    if fix.type == "delete_item":
                        if index >= len(original[root]):
                            raise ValueError(f"delete index does not exist: {target}")
                        plan.deletions[root].add(index)
                    elif fix.type == "append_item":
                        if index < len(original[root]):
                            raise ValueError(f"append cannot overwrite an original item: {target}")
                        plan.additions[root].add(index)
                    else:
                        raise ValueError(f"unknown correction type: {fix.type}")
                    explicit.update({("summary",), ("status",)})
    for section in _COLLECTIONS:
        expected = set(range(len(original[section]), len(original[section]) + len(plan.additions[section])))
        if plan.additions[section] != expected:
            raise ValueError(f"append slots must be consecutive from original length: {section}")
        if any(path[0] == section and path[1] in plan.deletions[section] for path in explicit_objects):
            raise ValueError(f"cannot replace and delete the same original item: {section}")
    plan.replacements = explicit | {
        path for path in bulk if path[1] not in plan.deletions[path[0]]
    }
    # A whole object/list replacement includes its children. Do not reread old
    # child indices after an authorized evidence list shrink/split.
    plan.replacements = {
        path for path in plan.replacements
        if not any(path[:i] in plan.replacements for i in range(1, len(path)))
    }
    if not plan.replacements and not any(plan.deletions.values()) and not any(plan.additions.values()):
        raise ValueError("fixes do not select any original field or new slot")
    return plan


def render_revision_scope(draft: InvestigationResult, review: CoverageReview) -> str:
    if not review.requires_revision:
        return "程序生成的最终允许范围：无；本轮未触发修订。"
    plan = validate_revision_scope(draft, review)
    return "\n".join([
        "程序生成的最终允许范围（replace 路径可包含类型规定的联动字段）：",
        *[f"- replace {_path_text(path)}" for path in sorted(plan.replacements, key=_path_text)],
        *[f"- delete /{section}/{i}" for section in _COLLECTIONS for i in sorted(plan.deletions[section])],
        *[f"- append /{section}/{i}" for section in _COLLECTIONS for i in sorted(plan.additions[section])],
    ])


def _read(value: Any, path: ResultPath) -> Any:
    try:
        for key in path:
            value = value[key]
        return value
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError(f"candidate is missing selected field: {_path_text(path)}") from exc


def _differences(left: Any, right: Any, path: ResultPath = ()) -> list[str]:
    if isinstance(left, dict) and isinstance(right, dict) and left.keys() == right.keys():
        return [p for key in left for p in _differences(left[key], right[key], (*path, key))]
    if isinstance(left, list) and isinstance(right, list) and len(left) == len(right):
        return [p for i, (a, b) in enumerate(zip(left, right)) for p in _differences(a, b, (*path, i))]
    return [] if left == right else [_path_text(path)]


def merge_scoped_revision(
    draft: InvestigationResult,
    candidate: InvestigationResult,
    review: CoverageReview,
    *,
    index: CodeIndex | None = None,
) -> RevisionApplication:
    plan = validate_revision_scope(draft, review)
    original = draft.model_dump(mode="json")
    proposed = candidate.model_dump(mode="json")
    merged = deepcopy(original)
    positions: dict[tuple[str, int], int] = {}
    for section in _COLLECTIONS:
        survivors = [i for i in range(len(original[section])) if i not in plan.deletions[section]]
        expected_length = len(survivors) + len(plan.additions[section])
        if len(proposed[section]) != expected_length:
            raise ValueError(f"candidate list length disagrees with declared edits: {section}")
        original_items = [original[section][i] for i in survivors]
        candidate_items = proposed[section][:len(survivors)]
        if candidate_items != original_items and Counter(
            json.dumps(item, sort_keys=True) for item in candidate_items
        ) == Counter(json.dumps(item, sort_keys=True) for item in original_items):
            raise ValueError(f"candidate changed original item order: {section}")
        for new_index, old_index in enumerate(survivors):
            positions[(section, old_index)] = new_index

    for path in sorted(plan.replacements, key=lambda p: (len(p), _path_text(p))):
        candidate_path = path
        if path[0] in _COLLECTIONS:
            candidate_path = (path[0], positions[(path[0], path[1])], *path[2:])
        parent = _read(merged, path[:-1])
        parent[path[-1]] = deepcopy(_read(proposed, candidate_path))
    for section in _COLLECTIONS:
        merged[section] = [item for i, item in enumerate(merged[section]) if i not in plan.deletions[section]]
        count = len(plan.additions[section])
        if count:
            merged[section].extend(deepcopy(proposed[section][-count:]))

    # Revalidate AFTER combining original and edited values, never only the
    # model's candidate. In particular complete must agree with retained gaps.
    result = InvestigationResult.model_validate(merged)
    original_positions = {(section, new): old for (section, old), new in positions.items()}
    for i, claim in enumerate(result.claims):
        # Map surviving original claims to their final positions.
        old_index = original_positions.get(("claims", i))
        old = draft.claims[old_index] if old_index is not None else None
        changed = old is None or claim.model_dump(mode="json") != original["claims"][old_index]
        if changed and claim.evidence_level == EvidenceLevel.SUSPECTED and not claim.limitations:
            raise ValueError(f"claim {i}: suspected evidence needs explicit limitations")
        if claim.evidence_level == EvidenceLevel.UNCLASSIFIED and (
            old is None or old.evidence_level != EvidenceLevel.UNCLASSIFIED
        ):
            raise ValueError(f"claim {i}: cannot introduce an unclassified evidence level")
    if any(len(split_artifact(artifact)) > 1 for artifact in result.artifacts):
        raise ValueError("artifact contains multiple objects; declare separate append targets")
    if index is not None:
        before = validate_investigation_result(draft, index=index)
        after = validate_investigation_result(result, index=index)
        old_invalid = Counter(
            (c["owner_kind"], int(c["owner_id"][1:]) - 1, c["location"], c["observation"])
            for c in before["checks"] if c["status"].startswith("invalid_")
        )
        sections = {"claim": "claims", "artifact": "artifacts", "material_need": "material_needs"}
        for check in after["checks"]:
            if not check["status"].startswith("invalid_"):
                continue
            section = sections[check["owner_kind"]]
            old_index = original_positions.get((section, int(check["owner_id"][1:]) - 1))
            key = (check["owner_kind"], old_index, check["location"], check["observation"])
            if not old_invalid[key]:
                raise ValueError(f"new invalid evidence: {check['location']}")
            old_invalid[key] -= 1
    return RevisionApplication(result, {
        "passed": True,
        "replacement_paths": sorted(_path_text(p) for p in plan.replacements),
        "deleted_paths": [f"/{section}/{i}" for section in _COLLECTIONS for i in sorted(plan.deletions[section])],
        "appended_paths": [f"/{section}/{i}" for section in _COLLECTIONS for i in sorted(plan.additions[section])],
        "applied_paths": _differences(original, merged),
        # These are paths in the final/candidate order, after declared deletions.
        "ignored_paths": _differences(proposed, merged),
        "consistency_checks": ["InvestigationResult", "evidence_levels", "atomic_artifacts"] + (
            ["new_evidence_references"] if index is not None else []
        ),
    })
