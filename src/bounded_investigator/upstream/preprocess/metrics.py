"""Deterministic metrics derived after rule decisions are complete."""

from __future__ import annotations

from .model import Function, PROGRAM_ANALYZE, PROGRAM_SKIP, UNDECIDED


def compute_skip_reachability(
    functions: list[Function], vtable_groups: dict | None = None
) -> dict:
    """Measure whether skipped functions remain reachable during later deep analysis."""
    by_ref = {fn.ref: fn for fn in functions}
    groups = list((vtable_groups or {}).values())

    def bfs(seed_refs: set[str], use_refs: bool, use_vtable: bool) -> dict[str, int]:
        depth = {ref: 0 for ref in seed_refs if ref in by_ref}
        frontier, current = list(depth), 0
        while frontier:
            current += 1
            next_frontier: list[str] = []
            for ref in frontier:
                fn = by_ref[ref]
                targets = list(fn.callees)
                if use_refs:
                    targets += fn.referenced
                for target in targets:
                    if target in by_ref and target not in depth:
                        depth[target] = current
                        next_frontier.append(target)
            if use_vtable:
                for members in groups:
                    if any(member in depth for member in members):
                        for member in members:
                            if member in by_ref and member not in depth:
                                depth[member] = current
                                next_frontier.append(member)
            frontier = next_frontier
        return depth

    analyze = {fn.ref for fn in functions if fn.decision == PROGRAM_ANALYZE}
    undecided = {fn.ref for fn in functions if fn.decision == UNDECIDED}
    seeds = analyze | undecided
    skipped = [fn for fn in functions if fn.decision == PROGRAM_SKIP]

    layers = {
        "L1_calls_only": bfs(analyze, False, False),
        "L2_with_refs": bfs(seeds, True, False),
        "L3_with_vtable": bfs(seeds, True, True),
    }
    best = layers["L3_with_vtable"]
    unreachable = [fn for fn in skipped if fn.ref not in best]
    libraries = [fn for fn in unreachable if fn.is_library is True]
    stubs = [fn for fn in unreachable if fn.is_library is not True and fn.is_stub]
    substantive = [
        fn for fn in unreachable
        if fn.is_library is not True and not fn.is_stub
    ]
    depths = [best[fn.ref] for fn in skipped if fn.ref in best]

    per_rule: dict[str, dict] = {}
    for fn in skipped:
        entry = per_rule.setdefault(
            fn.decision_reason or "?", {"total": 0, "unreachable": 0, "substantive": 0}
        )
        entry["total"] += 1
        if fn.ref not in best:
            entry["unreachable"] += 1
            if fn.is_library is not True and not fn.is_stub:
                entry["substantive"] += 1

    return {
        "skipped_total": len(skipped),
        "reachable_by_layer": {
            name: sum(1 for fn in skipped if fn.ref in depth)
            for name, depth in layers.items()
        },
        "unreachable_total": len(unreachable),
        "unreachable_libraries": len(libraries),
        "unreachable_stubs": len(stubs),
        "unreachable_substantive": len(substantive),
        "substantive_samples": [
            "%s %s (%s, %d 字符)"
            % (fn.hex_address, fn.name, fn.decision_reason, len(fn.code))
            for fn in substantive[:8]
        ],
        "max_depth": max(depths) if depths else 0,
        "mean_depth": round(sum(depths) / len(depths), 1) if depths else 0.0,
        "per_rule": per_rule,
    }
