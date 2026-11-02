"""Load and validate the stage-1 artifact contract for stage 2."""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

EXPECTED_PREPROCESS_SCHEMA_VERSION = 4
_REF_LINE_RE = re.compile(r"^\[(F-\d+)\]\s", re.MULTILINE)


class InputContractError(ValueError):
    """Stage-2 input is missing, malformed, or inconsistent."""


@dataclass(frozen=True)
class TriageFunction:
    ref: str
    name: str
    address: str
    code_chars: int
    has_indirect_call: bool | None
    in_degree: int
    out_degree: int
    api_refs: tuple[str, ...]
    string_refs: tuple[str, ...]
    basic_block_count: int | None
    loop_count: int | None
    decision: str


@dataclass(frozen=True)
class TriageUnit:
    unit_id: str
    subject_refs: tuple[str, ...]
    function_count: int
    function_metrics: tuple[dict[str, Any], ...]
    text: str
    path: Path


@dataclass(frozen=True)
class TriageInput:
    preprocess_path: Path
    units_dir: Path
    code_dir: Path
    sample: dict[str, Any]
    config: dict[str, Any]
    functions_by_ref: dict[str, TriageFunction]
    units: tuple[TriageUnit, ...]
    callers_by_ref: dict[str, tuple[str, ...]]
    callees_by_ref: dict[str, tuple[str, ...]]
    reference_edges: tuple[tuple[str, str], ...]
    reference_out_by_ref: dict[str, tuple[str, ...]]
    reference_in_by_ref: dict[str, tuple[str, ...]]
    vtable_groups: dict[str, tuple[str, ...]]
    vtable_keys_by_ref: dict[str, tuple[str, ...]]
    global_edges: tuple[dict[str, Any], ...]
    global_edges_by_ref: dict[str, tuple[dict[str, Any], ...]]
    global_edges_by_address: dict[str, tuple[dict[str, Any], ...]]
    global_edges_by_name: dict[str, tuple[dict[str, Any], ...]]
    string_users_by_value: dict[str, tuple[str, ...]]
    unit_by_ref: dict[str, TriageUnit]
    function_metric_by_ref: dict[str, dict[str, Any]]
    capability_hits_by_ref: dict[str, tuple[dict[str, Any], ...]] = field(default_factory=dict)

    @property
    def undecided_refs(self) -> tuple[str, ...]:
        return tuple(
            ref for ref, fn in self.functions_by_ref.items() if fn.decision == "UNDECIDED"
        )


@dataclass(frozen=True)
class TriageInputPaths:
    preprocess: Path
    units_dir: Path | None = None
    code_dir: Path | None = None

    def resolve(self) -> tuple[Path, Path, Path]:
        preprocess = Path(self.preprocess)
        root = preprocess.parent
        return (
            preprocess,
            Path(self.units_dir) if self.units_dir is not None else root / "units",
            Path(self.code_dir) if self.code_dir is not None else root / "code",
        )


def load_triage_input(paths: TriageInputPaths) -> TriageInput:
    preprocess_path, units_dir, code_dir = paths.resolve()
    raw = _read_json_object(preprocess_path, "preprocess.json")

    schema_version = _required(raw, "schema_version")
    if schema_version != EXPECTED_PREPROCESS_SCHEMA_VERSION:
        raise InputContractError(
            "preprocess.schema_version 必须为 %d,收到 %r"
            % (EXPECTED_PREPROCESS_SCHEMA_VERSION, schema_version)
        )

    sample = _required_dict(raw, "sample")
    for key in ("name", "sample_sha256", "deepextract_version"):
        _required(sample, key, "sample.%s" % key)

    config = _required_dict(raw, "config")
    for key in ("code_view", "function_code_max_chars"):
        _required(config, key, "config.%s" % key)
    if not isinstance(config["code_view"], str):
        raise InputContractError("config.code_view 必须是字符串")
    if (
        isinstance(config["function_code_max_chars"], bool)
        or not isinstance(config["function_code_max_chars"], int)
        or config["function_code_max_chars"] <= 0
    ):
        raise InputContractError("config.function_code_max_chars 必须是正整数")

    functions_raw = _required_list(raw, "functions")
    functions_by_ref = _load_functions(functions_raw)
    if not functions_by_ref:
        raise InputContractError("functions 不能为空")

    for key in ("call_edges", "reference_edges", "vtable_groups", "global_edges"):
        _required(raw, key)

    if not units_dir.is_dir():
        raise InputContractError("units/ 不存在: %s" % units_dir)
    if not code_dir.is_dir():
        raise InputContractError("code/ 不存在: %s" % code_dir)

    units = _load_units(
        _required_list(raw, "units"), units_dir, functions_by_ref
    )
    _validate_unit_coverage(units, functions_by_ref)
    _validate_complete_code(code_dir, functions_by_ref)

    callers, callees = _build_call_indexes(
        _required_list(raw, "call_edges"), functions_by_ref
    )
    reference_edges = _load_edges(
        _required_list(raw, "reference_edges"), "reference_edges", functions_by_ref
    )
    vtable_groups = _load_vtable_groups(
        _required_dict(raw, "vtable_groups"), functions_by_ref
    )
    global_edges = _load_global_edges(
        _required_list(raw, "global_edges"), functions_by_ref
    )
    reference_out, reference_in = _build_edge_indexes(reference_edges, functions_by_ref)
    vtable_keys_by_ref = _build_vtable_reverse_index(vtable_groups, functions_by_ref)
    global_by_ref, global_by_address, global_by_name = _build_global_indexes(
        global_edges, functions_by_ref
    )
    string_users = _build_string_users(functions_by_ref)
    capability_hits_by_ref = _load_capability_hits(raw, functions_by_ref)
    unit_by_ref, function_metric_by_ref = _build_unit_indexes(units)

    return TriageInput(
        preprocess_path=preprocess_path,
        units_dir=units_dir,
        code_dir=code_dir,
        sample=dict(sample),
        config={
            "code_view": config["code_view"],
            "function_code_max_chars": config["function_code_max_chars"],
        },
        functions_by_ref=functions_by_ref,
        units=tuple(units),
        callers_by_ref=callers,
        callees_by_ref=callees,
        reference_edges=reference_edges,
        reference_out_by_ref=reference_out,
        reference_in_by_ref=reference_in,
        vtable_groups=vtable_groups,
        vtable_keys_by_ref=vtable_keys_by_ref,
        global_edges=global_edges,
        global_edges_by_ref=global_by_ref,
        global_edges_by_address=global_by_address,
        global_edges_by_name=global_by_name,
        string_users_by_value=string_users,
        unit_by_ref=unit_by_ref,
        function_metric_by_ref=function_metric_by_ref,
        capability_hits_by_ref=capability_hits_by_ref,
    )


def _load_capability_hits(
    raw: dict[str, Any],
    functions_by_ref: dict[str, TriageFunction],
) -> dict[str, tuple[dict[str, Any], ...]]:
    rows = raw.get("capability_hits", [])
    if not isinstance(rows, list):
        raise InputContractError("capability_hits 必须是数组")
    grouped: dict[str, list[dict[str, Any]]] = {ref: [] for ref in functions_by_ref}
    seen: set[str] = set()
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise InputContractError("capability_hits[%d] 必须是对象" % index)
        hit_id = row.get("hit_id")
        ref = row.get("function_ref")
        capability = row.get("capability")
        rule_id = row.get("rule_id")
        confidence = row.get("confidence")
        if not isinstance(hit_id, str) or not re.fullmatch(r"CH-\d+", hit_id) or hit_id in seen:
            raise InputContractError("capability_hits[%d].hit_id 非法或重复" % index)
        if ref not in functions_by_ref:
            raise InputContractError("%s 引用未知函数: %r" % (hit_id, ref))
        if not all(isinstance(value, str) and value for value in (capability, rule_id, confidence)):
            raise InputContractError("%s capability/rule_id/confidence 必须是非空字符串" % hit_id)
        grouped[ref].append(dict(row))
        seen.add(hit_id)
    return {ref: tuple(items) for ref, items in grouped.items()}


def _read_json_object(path: Path, label: str) -> dict[str, Any]:
    if not path.is_file():
        raise InputContractError("%s 不存在: %s" % (label, path))
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise InputContractError("读取 %s 失败: %s" % (label, exc)) from exc
    if not isinstance(value, dict):
        raise InputContractError("%s 顶层必须是对象" % label)
    return value


def _required(mapping: dict[str, Any], key: str, path: str | None = None) -> Any:
    if key not in mapping:
        raise InputContractError("缺少必读字段: %s" % (path or key))
    return mapping[key]


def _required_dict(mapping: dict[str, Any], key: str) -> dict[str, Any]:
    value = _required(mapping, key)
    if not isinstance(value, dict):
        raise InputContractError("字段 %s 必须是对象" % key)
    return value


def _required_list(mapping: dict[str, Any], key: str) -> list[Any]:
    value = _required(mapping, key)
    if not isinstance(value, list):
        raise InputContractError("字段 %s 必须是数组" % key)
    return value


def _load_functions(items: list[Any]) -> dict[str, TriageFunction]:
    result: dict[str, TriageFunction] = {}
    required = (
        "ref",
        "name",
        "address",
        "code_chars",
        "has_indirect_call",
        "in_degree",
        "out_degree",
        "api_refs",
        "string_refs",
        "basic_block_count",
        "loop_count",
        "decision",
    )
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise InputContractError("functions[%d] 必须是对象" % index)
        for key in required:
            _required(item, key, "functions[%d].%s" % (index, key))
        ref = item["ref"]
        if not isinstance(ref, str) or not re.fullmatch(r"F-\d+", ref):
            raise InputContractError("functions[%d].ref 非法: %r" % (index, ref))
        if ref in result:
            raise InputContractError("functions[].ref 重复: %s" % ref)
        name = item["name"]
        address = item["address"]
        if not isinstance(name, str) or not isinstance(address, str):
            raise InputContractError("%s name/address 必须是字符串" % ref)
        api_refs = _string_list(item["api_refs"], "%s.api_refs" % ref)
        string_refs = _string_list(item["string_refs"], "%s.string_refs" % ref)
        has_indirect_call = item["has_indirect_call"]
        if has_indirect_call is not None and not isinstance(has_indirect_call, bool):
            raise InputContractError("%s.has_indirect_call 必须是 bool|null" % ref)
        decision = item["decision"]
        if decision not in ("PROGRAM_ANALYZE", "PROGRAM_SKIP", "UNDECIDED"):
            raise InputContractError("%s.decision 非法: %r" % (ref, decision))
        result[ref] = TriageFunction(
            ref=ref,
            name=name,
            address=address,
            code_chars=_nonnegative_int(item["code_chars"], "%s.code_chars" % ref),
            has_indirect_call=has_indirect_call,
            in_degree=_nonnegative_int(item["in_degree"], "%s.in_degree" % ref),
            out_degree=_nonnegative_int(item["out_degree"], "%s.out_degree" % ref),
            api_refs=api_refs,
            string_refs=string_refs,
            basic_block_count=_optional_nonnegative_int(
                item["basic_block_count"], "%s.basic_block_count" % ref
            ),
            loop_count=_optional_nonnegative_int(
                item["loop_count"], "%s.loop_count" % ref
            ),
            decision=decision,
        )
    return result


def _string_list(value: Any, path: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise InputContractError("%s 必须是字符串数组" % path)
    return tuple(value)


def _nonnegative_int(value: Any, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise InputContractError("%s 必须是非负整数" % path)
    return value


def _optional_nonnegative_int(value: Any, path: str) -> int | None:
    if value is None:
        return None
    return _nonnegative_int(value, path)


def _load_units(
    items: list[Any], units_dir: Path, functions_by_ref: dict[str, TriageFunction]
) -> list[TriageUnit]:
    result: list[TriageUnit] = []
    seen_unit_ids: set[str] = set()
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise InputContractError("units[%d] 必须是对象" % index)
        for key in ("unit_id", "subject_refs", "function_count", "function_metrics"):
            _required(item, key, "units[%d].%s" % (index, key))
        unit_id = item["unit_id"]
        if not isinstance(unit_id, str) or not re.fullmatch(r"U-\d+", unit_id):
            raise InputContractError("units[%d].unit_id 非法: %r" % (index, unit_id))
        if unit_id in seen_unit_ids:
            raise InputContractError("units[].unit_id 重复: %s" % unit_id)
        seen_unit_ids.add(unit_id)

        subject_refs = _validate_ref_list(
            item["subject_refs"], "units[%d].subject_refs" % index, functions_by_ref
        )
        if len(subject_refs) != len(set(subject_refs)):
            raise InputContractError("%s subject_refs 存在重复" % unit_id)
        function_count = _nonnegative_int(
            item["function_count"], "%s.function_count" % unit_id
        )
        if function_count != len(subject_refs):
            raise InputContractError(
                "%s function_count=%r 与 subject_refs=%d 不一致"
                % (unit_id, function_count, len(subject_refs))
            )
        metrics = item["function_metrics"]
        if not isinstance(metrics, list):
            raise InputContractError("%s function_metrics 必须是数组" % unit_id)
        metric_refs: list[str] = []
        normalized_metrics: list[dict[str, Any]] = []
        for metric_index, metric in enumerate(metrics):
            if not isinstance(metric, dict):
                raise InputContractError(
                    "%s function_metrics[%d] 必须是对象" % (unit_id, metric_index)
                )
            for key in ("ref", "truncated_by_function_limit"):
                _required(
                    metric,
                    key,
                    "%s.function_metrics[%d].%s" % (unit_id, metric_index, key),
                )
            metric_ref = metric["ref"]
            if not isinstance(metric_ref, str) or metric_ref not in functions_by_ref:
                raise InputContractError(
                    "%s.function_metrics[%d].ref 引用未知函数: %r"
                    % (unit_id, metric_index, metric_ref)
                )
            truncated = metric["truncated_by_function_limit"]
            if not isinstance(truncated, bool):
                raise InputContractError(
                    "%s.function_metrics[%d].truncated_by_function_limit 必须是 bool"
                    % (unit_id, metric_index)
                )
            metric_refs.append(metric_ref)
            normalized_metrics.append(
                {
                    "ref": metric_ref,
                    "truncated_by_function_limit": truncated,
                }
            )
        if tuple(metric_refs) != subject_refs:
            raise InputContractError(
                "%s function_metrics.ref 顺序必须与 subject_refs 完全一致" % unit_id
            )

        path = units_dir / (unit_id + ".txt")
        if not path.is_file():
            raise InputContractError("unit 文件不存在: %s" % path)
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise InputContractError("读取 unit 失败 %s: %s" % (path, exc)) from exc
        markers = tuple(_REF_LINE_RE.findall(text))
        if markers != subject_refs:
            raise InputContractError(
                "%s 文件中的 [F-xxxx] 顺序与 subject_refs 不一致: file=%s json=%s"
                % (unit_id, list(markers), list(subject_refs))
            )

        result.append(
            TriageUnit(
                unit_id=unit_id,
                subject_refs=subject_refs,
                function_count=len(subject_refs),
                function_metrics=tuple(normalized_metrics),
                text=text,
                path=path,
            )
        )
    return result


def _validate_unit_coverage(
    units: list[TriageUnit], functions_by_ref: dict[str, TriageFunction]
) -> None:
    expected = [
        ref for ref, fn in functions_by_ref.items() if fn.decision == "UNDECIDED"
    ]
    actual = [ref for unit in units for ref in unit.subject_refs]
    counts = Counter(actual)
    duplicates = sorted(ref for ref, count in counts.items() if count > 1)
    missing = sorted(set(expected) - set(actual))
    extra = sorted(set(actual) - set(expected))
    if duplicates or missing or extra:
        raise InputContractError(
            "units 与 UNDECIDED 集合不一致: missing=%s extra=%s duplicate=%s"
            % (missing, extra, duplicates)
        )


def _validate_complete_code(
    code_dir: Path, functions_by_ref: dict[str, TriageFunction]
) -> None:
    missing: list[str] = []
    empty: list[str] = []
    for ref, fn in functions_by_ref.items():
        if fn.decision != "UNDECIDED":
            continue
        path = code_dir / (ref + ".txt")
        if not path.is_file():
            missing.append(ref)
            continue
        if fn.code_chars > 0 and path.stat().st_size == 0:
            empty.append(ref)
    if missing or empty:
        raise InputContractError(
            "第二跳完整代码不可用: missing=%s empty=%s" % (missing, empty)
        )


def _validate_ref_list(
    value: Any, path: str, functions_by_ref: dict[str, TriageFunction]
) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise InputContractError("%s 必须是数组" % path)
    refs: list[str] = []
    for index, ref in enumerate(value):
        if not isinstance(ref, str) or ref not in functions_by_ref:
            raise InputContractError("%s[%d] 引用未知函数: %r" % (path, index, ref))
        refs.append(ref)
    return tuple(refs)


def _build_call_indexes(
    items: list[Any], functions_by_ref: dict[str, TriageFunction]
) -> tuple[dict[str, tuple[str, ...]], dict[str, tuple[str, ...]]]:
    edges = _load_edges(items, "call_edges", functions_by_ref)
    callers: dict[str, list[str]] = {ref: [] for ref in functions_by_ref}
    callees: dict[str, list[str]] = {ref: [] for ref in functions_by_ref}
    for source, target in edges:
        if target not in callees[source]:
            callees[source].append(target)
        if source not in callers[target]:
            callers[target].append(source)
    for ref, fn in functions_by_ref.items():
        if fn.in_degree != len(callers[ref]) or fn.out_degree != len(callees[ref]):
            raise InputContractError(
                "%s degree 与 call_edges 重建结果不一致: in=%d/%d out=%d/%d"
                % (
                    ref,
                    fn.in_degree,
                    len(callers[ref]),
                    fn.out_degree,
                    len(callees[ref]),
                )
            )
    return (
        {ref: tuple(values) for ref, values in callers.items()},
        {ref: tuple(values) for ref, values in callees.items()},
    )


def _load_edges(
    items: list[Any], label: str, functions_by_ref: dict[str, TriageFunction]
) -> tuple[tuple[str, str], ...]:
    result: list[tuple[str, str]] = []
    for index, edge in enumerate(items):
        if not isinstance(edge, (list, tuple)) or len(edge) != 2:
            raise InputContractError("%s[%d] 必须是二元边" % (label, index))
        source, target = edge
        if (
            not isinstance(source, str)
            or not isinstance(target, str)
            or source not in functions_by_ref
            or target not in functions_by_ref
        ):
            raise InputContractError(
                "%s[%d] 引用未知函数: %r" % (label, index, edge)
            )
        result.append((source, target))
    return tuple(result)


def _load_vtable_groups(
    groups: dict[str, Any], functions_by_ref: dict[str, TriageFunction]
) -> dict[str, tuple[str, ...]]:
    result: dict[str, tuple[str, ...]] = {}
    for key, value in groups.items():
        result[str(key)] = _validate_ref_list(
            value, "vtable_groups[%r]" % key, functions_by_ref
        )
    return result


def _load_global_edges(
    items: list[Any], functions_by_ref: dict[str, TriageFunction]
) -> tuple[dict[str, Any], ...]:
    result: list[dict[str, Any]] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise InputContractError("global_edges[%d] 必须是对象" % index)
        for key in ("source", "address", "name", "access_type"):
            _required(item, key, "global_edges[%d].%s" % (index, key))
        if not isinstance(item["source"], str) or item["source"] not in functions_by_ref:
            raise InputContractError(
                "global_edges[%d].source 引用未知函数: %r"
                % (index, item["source"])
            )
        result.append(
            {
                "source": item["source"],
                "address": item["address"],
                "name": item["name"],
                "access_type": item["access_type"],
            }
        )
    return tuple(result)


def _build_edge_indexes(
    edges: tuple[tuple[str, str], ...],
    functions_by_ref: dict[str, TriageFunction],
) -> tuple[dict[str, tuple[str, ...]], dict[str, tuple[str, ...]]]:
    outgoing: dict[str, list[str]] = {ref: [] for ref in functions_by_ref}
    incoming: dict[str, list[str]] = {ref: [] for ref in functions_by_ref}
    for source, target in edges:
        if target not in outgoing[source]:
            outgoing[source].append(target)
        if source not in incoming[target]:
            incoming[target].append(source)
    return (
        {ref: tuple(values) for ref, values in outgoing.items()},
        {ref: tuple(values) for ref, values in incoming.items()},
    )


def _build_vtable_reverse_index(
    groups: dict[str, tuple[str, ...]],
    functions_by_ref: dict[str, TriageFunction],
) -> dict[str, tuple[str, ...]]:
    result: dict[str, list[str]] = {ref: [] for ref in functions_by_ref}
    for key, members in groups.items():
        for ref in members:
            if key not in result[ref]:
                result[ref].append(key)
    return {ref: tuple(keys) for ref, keys in result.items()}


def _build_global_indexes(
    edges: tuple[dict[str, Any], ...],
    functions_by_ref: dict[str, TriageFunction],
) -> tuple[
    dict[str, tuple[dict[str, Any], ...]],
    dict[str, tuple[dict[str, Any], ...]],
    dict[str, tuple[dict[str, Any], ...]],
]:
    by_ref: dict[str, list[dict[str, Any]]] = {ref: [] for ref in functions_by_ref}
    by_address: dict[str, list[dict[str, Any]]] = {}
    by_name: dict[str, list[dict[str, Any]]] = {}
    for edge in edges:
        by_ref[edge["source"]].append(edge)
        address = _relation_key(edge.get("address"))
        name = _relation_key(edge.get("name"))
        if address is not None:
            by_address.setdefault(address, []).append(edge)
        if name is not None:
            by_name.setdefault(name, []).append(edge)
    return (
        {ref: tuple(values) for ref, values in by_ref.items()},
        {key: tuple(values) for key, values in by_address.items()},
        {key: tuple(values) for key, values in by_name.items()},
    )


def _build_string_users(
    functions_by_ref: dict[str, TriageFunction],
) -> dict[str, tuple[str, ...]]:
    result: dict[str, list[str]] = {}
    for ref, fn in functions_by_ref.items():
        for value in fn.string_refs:
            users = result.setdefault(value, [])
            if ref not in users:
                users.append(ref)
    return {value: tuple(refs) for value, refs in result.items()}


def _build_unit_indexes(
    units: list[TriageUnit],
) -> tuple[dict[str, TriageUnit], dict[str, dict[str, Any]]]:
    unit_by_ref: dict[str, TriageUnit] = {}
    metric_by_ref: dict[str, dict[str, Any]] = {}
    for unit in units:
        for metric in unit.function_metrics:
            ref = metric["ref"]
            unit_by_ref[ref] = unit
            metric_by_ref[ref] = dict(metric)
    return unit_by_ref, metric_by_ref


def _relation_key(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value).strip().lower()
