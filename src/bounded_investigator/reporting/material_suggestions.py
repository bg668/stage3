from __future__ import annotations

from ..evidence.scope import EvidenceScope
from ..schemas import MaterialSuggestion


_BENEFIT_BY_KIND = {
    "function_code": "核对具体分支、调用条件和数据流向",
    "resource_bytes": "检查资源正文、解码结果及相关行为条件",
    "data_bytes": "解析所引用的数据结构或载荷内容",
    "runtime_trace": "验证条件分支是否被触发，以及操作是否实际成功",
    "other": "进一步核实未闭合的证据关系",
}


def build_material_suggestions(scope: EvidenceScope) -> list[MaterialSuggestion]:
    """Produce conditional, non-assertive hints from real Worker material needs.

    No LLM call, no fabricated material requirement, no claim of sample absence.
    Available materials already submitted to a Worker never become suggestions.
    """
    output: list[MaterialSuggestion] = []
    for item in sorted(scope.materials, key=lambda x: (
        {"critical": 0, "major": 1, "minor": 2}[x.impact], x.scope_id,
    )):
        if item.status == "available" and item.submission_status != "not_submitted":
            continue
        if item.status == "unknown" and not item.requested_by_packets and not item.request_reason:
            # Unverified expert inventory entry, not an actual requested follow-up.
            continue
        if item.status == "available":
            action = "submit_for_analysis"
            intro = f"如将本次输入中已有的「{item.label}」提交给后续调查"
        elif item.status == "unknown":
            action = "confirm_or_provide"
            intro = f"如能确认或补充「{item.label}」的实际内容"
        elif item.status == "partial":
            action = "provide"
            intro = f"如补充「{item.label}」尚未覆盖的部分"
        else:
            # Even an attested unavailable item is a suggestion, not a report fact.
            action = "provide"
            intro = f"如后续能提供「{item.label}」"
        benefit = item.request_reason or _BENEFIT_BY_KIND.get(item.kind or "", _BENEFIT_BY_KIND["other"])
        output.append(MaterialSuggestion(
            scope_id=item.scope_id,
            target=item.target or item.label,
            kind=item.kind or "other",
            availability=item.status,
            action=action,
            recommendation=f"{intro}，可进一步分析：{benefit}。",
            expected_benefit=benefit,
            impact=item.impact,
            requested_by_packets=list(item.requested_by_packets),
            request_evidence=list(item.request_evidence),
        ))
    return output
