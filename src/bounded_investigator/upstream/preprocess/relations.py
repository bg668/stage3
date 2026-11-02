"""Build function relationships from normalized DeepExtract relation facts."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .model import Function


@dataclass(frozen=True)
class RelationInput:
    outbound_xrefs: list[Any] = field(default_factory=list)
    inbound_xrefs: list[Any] = field(default_factory=list)
    vtable_contexts: list[Any] = field(default_factory=list)
    global_var_accesses: list[Any] = field(default_factory=list)


@dataclass(frozen=True)
class GlobalEdge:
    """Raw function -> global-variable access reported by DeepExtract."""

    source: str
    address: Any
    name: Any
    access_type: Any

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "address": self.address,
            "name": self.name,
            "access_type": self.access_type,
        }


@dataclass
class Relationships:
    call_edges: list[tuple[str, str]] = field(default_factory=list)
    reference_edges: list[tuple[str, str]] = field(default_factory=list)
    vtable_groups: dict[str, list[str]] = field(default_factory=dict)
    # Internal safety fact: includes functions with even a single vtable context.
    # The exported vtable_groups contract still keeps only multi-member groups.
    vtable_members: set[str] = field(default_factory=set)
    global_edges: list[GlobalEdge] = field(default_factory=list)


def build_relationships(
    functions: list[Function], relation_inputs: dict[str, RelationInput]
) -> Relationships:
    """Populate per-function adjacency and return top-level relationship data."""
    by_source_id = {fn.source_id: fn for fn in functions}
    graph = Relationships()
    vtable_groups_all: dict[str, list[str]] = {}

    for fn in functions:
        relation = relation_inputs.get(fn.ref, RelationInput())
        for item in relation.outbound_xrefs:
            if not isinstance(item, dict):
                continue
            target_id = item.get("function_id")
            if target_id is None:
                continue
            callee = by_source_id.get(str(target_id))
            if callee is None or callee.ref == fn.ref:
                continue
            if callee.ref not in fn.callees:
                fn.callees.append(callee.ref)
                graph.call_edges.append((fn.ref, callee.ref))
            if fn.ref not in callee.callers:
                callee.callers.append(fn.ref)

    # Inbound xrefs provide supplementary data-reference edges such as callbacks/vtables.
    for fn in functions:
        relation = relation_inputs.get(fn.ref, RelationInput())
        for item in relation.inbound_xrefs:
            if not isinstance(item, dict):
                continue
            source_id = item.get("function_id")
            if source_id is None:
                continue
            caller = by_source_id.get(str(source_id))
            if caller is None or caller.ref == fn.ref:
                continue
            if fn.ref not in caller.referenced:
                caller.referenced.append(fn.ref)
                graph.reference_edges.append((caller.ref, fn.ref))

    for fn in functions:
        relation = relation_inputs.get(fn.ref, RelationInput())
        for item in relation.vtable_contexts:
            key = _vtable_key(item)
            if key:
                graph.vtable_members.add(fn.ref)
                # Preserve source ordering/duplicates to match existing behavior.
                vtable_groups_all.setdefault(key, []).append(fn.ref)
        for item in relation.global_var_accesses:
            if not isinstance(item, dict):
                continue
            graph.global_edges.append(
                GlobalEdge(
                    source=fn.ref,
                    address=item.get("address"),
                    name=item.get("name"),
                    access_type=item.get("access_type"),
                )
            )

    graph.vtable_groups = {
        key: members for key, members in vtable_groups_all.items() if len(members) > 1
    }
    return graph


def _vtable_key(item: Any) -> str | None:
    if isinstance(item, dict):
        for candidate in (
            "vtable_address",
            "vtable_ea",
            "vtable",
            "class_name",
            "class",
            "owner",
            "name",
        ):
            value = item.get(candidate)
            if value not in (None, ""):
                return "%s=%s" % (candidate, value)
        return None
    if item not in (None, ""):
        return str(item)
    return None
