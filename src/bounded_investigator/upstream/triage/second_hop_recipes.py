"""Pure V4 recipes: map one UNCERTAIN reason to deterministic relation facts."""

from __future__ import annotations

from collections import OrderedDict

from .domain import UNCERTAIN_REASONS
from .input import TriageInput
from .second_hop_domain import (
    FactSupplementSpec,
    RecipePlan,
    RelatedFunctionSpec,
    RelationTrace,
)


class SecondHopRecipeError(ValueError):
    pass


def build_recipe(snapshot: TriageInput, subject_ref: str, reason: str) -> RecipePlan:
    if subject_ref not in snapshot.functions_by_ref:
        raise SecondHopRecipeError("未知 subject ref: %s" % subject_ref)
    if reason not in UNCERTAIN_REASONS:
        raise SecondHopRecipeError("不是 UNCERTAIN reason: %s" % reason)

    builders = {
        "U_TRUNCATED": _truncated,
        "U_CALLEE_OPAQUE": _callee_opaque,
        "U_CALLER_CONTEXT": _caller_context,
        "U_INDIRECT": _indirect,
        "U_DATA_OPAQUE": _data_opaque,
    }
    plan = builders[reason](snapshot, subject_ref)
    capability_facts = tuple(
        FactSupplementSpec(
            kind="capability_hit",
            source="preprocess.capability_hits",
            relation="subject_capability_hint",
            status="FOUND",
            fields=(
                ("hit_id", str(hit.get("hit_id", ""))),
                ("rule_id", str(hit.get("rule_id", ""))),
                ("capability", str(hit.get("capability", ""))),
                ("confidence", str(hit.get("confidence", ""))),
                ("owner_eligible", str(bool(hit.get("owner_eligible", False))).lower()),
            ),
            detail="程序规则线索,不是恶意性结论",
        )
        for hit in snapshot.capability_hits_by_ref.get(subject_ref, ())
    )
    return RecipePlan(
        facts=(*capability_facts, *plan.facts),
        related_functions=plan.related_functions,
        missing_sections=plan.missing_sections,
    )


def _truncated(snapshot: TriageInput, ref: str) -> RecipePlan:
    unit = snapshot.unit_by_ref.get(ref)
    metric = snapshot.function_metric_by_ref.get(ref, {})
    fields = (
        ("unit_id", unit.unit_id if unit else "NOT_FOUND"),
        ("truncated_by_function_limit", str(bool(metric.get("truncated_by_function_limit"))).lower()),
        ("stage1_function_code_max_chars", str(snapshot.config["function_code_max_chars"])),
    )
    return RecipePlan(
        facts=(
            FactSupplementSpec(
                kind="render_context",
                source="units.function_metrics",
                relation="stage1_subject_render",
                status="FOUND" if unit is not None else "NOT_FOUND",
                fields=fields,
                detail="Round2 SUBJECT 始终改用 code/ 中完整代码",
            ),
        ),
        missing_sections=() if unit is not None else ("units.function_metrics",),
    )


def _callee_opaque(snapshot: TriageInput, ref: str) -> RecipePlan:
    callees = snapshot.callees_by_ref.get(ref, ())
    if not callees:
        return RecipePlan(
            facts=(_not_found("call_edges", "direct_callee", "SUBJECT 没有直接 callee"),),
            missing_sections=("direct_callee",),
        )
    related = tuple(
        RelatedFunctionSpec(
            ref=callee,
            priority=index,
            traces=(RelationTrace("call_edges", "callee", "%s -> %s" % (ref, callee)),),
        )
        for index, callee in enumerate(callees)
    )
    return RecipePlan(related_functions=related)


def _caller_context(snapshot: TriageInput, ref: str) -> RecipePlan:
    callers = snapshot.callers_by_ref.get(ref, ())
    if not callers:
        return RecipePlan(
            facts=(_not_found("call_edges", "caller", "SUBJECT 没有直接 caller"),),
            missing_sections=("caller",),
        )
    related = tuple(
        RelatedFunctionSpec(
            ref=caller,
            priority=index,
            traces=(RelationTrace("call_edges", "caller", "%s -> %s" % (caller, ref)),),
        )
        for index, caller in enumerate(callers)
    )
    return RecipePlan(related_functions=related)


def _indirect(snapshot: TriageInput, ref: str) -> RecipePlan:
    facts: list[FactSupplementSpec] = []
    missing: list[str] = []
    related = _RelatedAccumulator()

    subject_globals = snapshot.global_edges_by_ref.get(ref, ())
    if subject_globals:
        for edge in subject_globals:
            facts.append(_global_fact(edge))
    else:
        facts.append(_not_found("global_edges", "subject_global_access", "SUBJECT 未检出全局变量访问"))
        missing.append("subject_global_access")

    writers: list[tuple[str, dict]] = []
    seen_writer_refs: set[str] = set()
    for edge in subject_globals:
        for candidate in _matching_global_edges(snapshot, edge):
            writer_ref = candidate["source"]
            if writer_ref == ref or writer_ref in seen_writer_refs or not _is_write(candidate.get("access_type")):
                continue
            seen_writer_refs.add(writer_ref)
            writers.append((writer_ref, candidate))
    if writers:
        for index, (writer_ref, edge) in enumerate(writers):
            related.add(
                writer_ref,
                10 + index,
                RelationTrace(
                    "global_edges",
                    "global_writer",
                    "%s writes %s (%s) used by %s"
                    % (writer_ref, _global_label(edge), edge.get("access_type"), ref),
                ),
            )
    else:
        facts.append(_not_found("global_edges", "global_writer", "未找到 SUBJECT 所访问全局对象的外部写入者"))
        missing.append("global_writer")

    callees = snapshot.callees_by_ref.get(ref, ())
    selected_callees = callees[:2]
    if selected_callees:
        for index, callee in enumerate(selected_callees):
            related.add(
                callee,
                20 + index,
                RelationTrace("call_edges", "callee", "%s -> %s" % (ref, callee)),
            )
        if len(callees) > 2:
            facts.append(
                FactSupplementSpec(
                    kind="recipe_limit",
                    source="call_edges",
                    relation="callee",
                    status="OMITTED_BY_RECIPE",
                    fields=(("selected", "2"), ("total", str(len(callees)))),
                    detail="U_INDIRECT 固定只补前两个直接 callee；其余关系不静默丢失",
                )
            )
    else:
        facts.append(_not_found("call_edges", "callee", "SUBJECT 没有直接 callee"))
        missing.append("callee")

    reference_neighbors: list[tuple[str, str]] = []
    for target in snapshot.reference_out_by_ref.get(ref, ()):
        reference_neighbors.append((target, "%s -> %s" % (ref, target)))
    for source in snapshot.reference_in_by_ref.get(ref, ()):
        reference_neighbors.append((source, "%s -> %s" % (source, ref)))
    if reference_neighbors:
        for index, (neighbor, detail) in enumerate(reference_neighbors):
            related.add(neighbor, 30 + index, RelationTrace("reference_edges", "reference_neighbor", detail))
    else:
        facts.append(_not_found("reference_edges", "reference_neighbor", "SUBJECT 未检出 reference edge"))
        missing.append("reference_neighbor")

    vtable_keys = snapshot.vtable_keys_by_ref.get(ref, ())
    vtable_members: list[tuple[str, str]] = []
    for key in vtable_keys:
        facts.append(
            FactSupplementSpec(
                kind="vtable_group",
                source="vtable_groups",
                relation="membership",
                status="FOUND",
                fields=(("group", key),),
                detail="SUBJECT 属于该 vtable group",
            )
        )
        for member in snapshot.vtable_groups.get(key, ()):
            if member != ref:
                vtable_members.append((member, key))
    if vtable_members:
        for index, (member, key) in enumerate(vtable_members):
            related.add(
                member,
                40 + index,
                RelationTrace("vtable_groups", "same_vtable_group", "%s: %s <-> %s" % (key, ref, member)),
            )
    elif not vtable_keys:
        facts.append(_not_found("vtable_groups", "membership", "SUBJECT 不属于任何导出的 vtable group"))
        missing.append("vtable_group")

    return RecipePlan(
        facts=tuple(facts),
        related_functions=related.build(),
        missing_sections=tuple(_stable_unique(missing)),
    )


def _data_opaque(snapshot: TriageInput, ref: str) -> RecipePlan:
    fn = snapshot.functions_by_ref[ref]
    facts: list[FactSupplementSpec] = []
    missing: list[str] = []
    related = _RelatedAccumulator()

    if fn.string_refs:
        for value in fn.string_refs:
            facts.append(
                FactSupplementSpec(
                    kind="string_ref",
                    source="functions[].string_refs",
                    relation="subject_string",
                    status="FOUND",
                    fields=(("value", value),),
                )
            )
            for user in snapshot.string_users_by_value.get(value, ()):
                if user != ref:
                    related.add(
                        user,
                        20,
                        RelationTrace(
                            "functions[].string_refs",
                            "shared_string",
                            "%s and %s share %r" % (ref, user, value),
                        ),
                    )
    else:
        facts.append(_not_found("functions[].string_refs", "subject_string", "SUBJECT 未检出字符串引用"))
        missing.append("subject_string")

    subject_globals = snapshot.global_edges_by_ref.get(ref, ())
    if subject_globals:
        for edge in subject_globals:
            facts.append(_global_fact(edge))
            for candidate in _matching_global_edges(snapshot, edge):
                user = candidate["source"]
                if user == ref:
                    continue
                related.add(
                    user,
                    10,
                    RelationTrace(
                        "global_edges",
                        "shared_global",
                        "%s %s %s; %s %s %s"
                        % (
                            ref,
                            edge.get("access_type"),
                            _global_label(edge),
                            user,
                            candidate.get("access_type"),
                            _global_label(candidate),
                        ),
                    ),
                )
    else:
        facts.append(_not_found("global_edges", "subject_global_access", "SUBJECT 未检出全局变量访问"))
        missing.append("subject_global_access")

    if not related.has_items():
        facts.append(_not_found("derived_relation", "data_related_function", "没有找到共享字符串或全局对象的其他函数"))
        missing.append("data_related_function")

    return RecipePlan(
        facts=tuple(facts),
        related_functions=related.build(),
        missing_sections=tuple(_stable_unique(missing)),
    )


class _RelatedAccumulator:
    def __init__(self) -> None:
        self._items: OrderedDict[str, tuple[int, list[RelationTrace]]] = OrderedDict()

    def add(self, ref: str, priority: int, trace: RelationTrace) -> None:
        if ref not in self._items:
            self._items[ref] = (priority, [trace])
            return
        current_priority, traces = self._items[ref]
        if trace not in traces:
            traces.append(trace)
        self._items[ref] = (min(current_priority, priority), traces)

    def has_items(self) -> bool:
        return bool(self._items)

    def build(self) -> tuple[RelatedFunctionSpec, ...]:
        rows = [
            RelatedFunctionSpec(ref=ref, priority=priority, traces=tuple(traces))
            for ref, (priority, traces) in self._items.items()
        ]
        rows.sort(key=lambda item: (item.priority, _ref_number(item.ref)))
        return tuple(rows)


def _matching_global_edges(snapshot: TriageInput, edge: dict) -> tuple[dict, ...]:
    address_key = _global_key(edge.get("address"))
    if address_key:
        return snapshot.global_edges_by_address.get(address_key, ())
    name_key = _global_key(edge.get("name"))
    if name_key:
        return snapshot.global_edges_by_name.get(name_key, ())
    return ()


def _global_fact(edge: dict) -> FactSupplementSpec:
    return FactSupplementSpec(
        kind="global_access",
        source="global_edges",
        relation="subject_global_access",
        status="FOUND",
        fields=(
            ("address", _display(edge.get("address"))),
            ("name", _display(edge.get("name"))),
            ("access_type", _display(edge.get("access_type"))),
        ),
    )


def _global_label(edge: dict) -> str:
    name = _display(edge.get("name"))
    address = _display(edge.get("address"))
    return "%s@%s" % (name, address)


def _is_write(value) -> bool:
    text = str(value or "").strip().lower().replace("_", "")
    return "write" in text or text in {"w", "rw", "wr"}


def _global_key(value) -> str | None:
    if value in (None, ""):
        return None
    return str(value).strip().lower()


def _display(value) -> str:
    return "null" if value is None else str(value)


def _not_found(source: str, relation: str, detail: str) -> FactSupplementSpec:
    return FactSupplementSpec(
        kind="missing_relation",
        source=source,
        relation=relation,
        status="NOT_FOUND",
        detail=detail,
    )


def _stable_unique(items: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _ref_number(ref: str) -> int:
    try:
        return int(ref.split("-", 1)[1])
    except Exception:
        return 2**31 - 1
