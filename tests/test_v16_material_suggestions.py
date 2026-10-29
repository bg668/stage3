"""Material availability is a follow-up suggestion, never a malware fact."""
from __future__ import annotations

import json

from bounded_investigator.evidence.scope import EvidenceScope, MaterialScopeItem, PacketCoverage
from bounded_investigator.reporting.material_suggestions import build_material_suggestions
from bounded_investigator.reporting.knowledge_report import (
    project_knowledge, render_knowledge_markdown, save_knowledge_outputs,
)
from bounded_investigator.evidence.inventory import build_automatic_scope, build_input_inventory
from bounded_investigator.schemas import (
    DeliveryStatus, EvidenceRef,
    InvestigationResult, InvestigationStatus, MaterialNeed, RunEnvelope, UsageSummary,
)
from factories import canonical


def entry(scope_id, status, *, submit="unknown", kind="function_code", reason="检查远程调用入口", packets=None):
    return MaterialScopeItem(
        scope_id=scope_id, label=f"{kind}: sample_target", kind=kind, target="sample_target",
        status=status, impact="major", requested_by_packets=packets or ["J01"],
        request_evidence=["F-0100 L5"], request_reason=reason, submission_status=submit,
        source="indexed input", details="Observed input scope verified by index",
    )


def test_no_preseeded_material_suggestions():
    assert build_material_suggestions(EvidenceScope()) == []


def test_not_in_input_generates_only_a_conditional_hint():
    scope = EvidenceScope(materials=[entry("requested_1", "not_in_input")])
    suggestions = build_material_suggestions(scope)
    assert len(suggestions) == 1
    hint = suggestions[0]
    assert hint.scope_id == "requested_1"
    assert hint.target == "sample_target"
    assert hint.action == "provide"
    assert "如后续能提供" in hint.recommendation
    assert "检查远程调用入口" in hint.recommendation
    assert all(word not in hint.recommendation for word in ("样本缺失", "样本不存在", "未提供源码"))
    assert hint.requested_by_packets == ["J01"]
    assert hint.request_evidence == ["F-0100 L5"]


def test_unknown_partial_and_available_are_not_treated_as_absent():
    scope = EvidenceScope(materials=[
        entry("known", "available", submit="submitted_to_worker"),
        entry("unread", "available", submit="not_submitted"),
        entry("partial", "partial", submit="submitted_to_worker"),
        entry("unknown", "unknown"),
    ])
    suggestions = {x.scope_id: x for x in build_material_suggestions(scope)}
    assert "known" not in suggestions
    assert suggestions["unread"].action == "submit_for_analysis"
    assert "本次输入中已有" in suggestions["unread"].recommendation
    assert "尚未覆盖的部分" in suggestions["partial"].recommendation
    assert suggestions["unknown"].action == "confirm_or_provide"
    assert "确认或补充" in suggestions["unknown"].recommendation


def test_expert_unknown_without_worker_request_is_not_a_material_hint():
    item = entry("unknown", "unknown", packets=[])
    item.requested_by_packets = []
    item.request_reason = ""
    assert build_material_suggestions(EvidenceScope(materials=[item])) == []


def test_report_material_hint_is_independent_from_behavior_and_delivery_warning():
    scope = EvidenceScope(materials=[entry("requested_1", "not_in_input")])
    warnings = [PacketCoverage(packet_id="J08", delivery_status="raw_only").model_dump()]
    suggestions = build_material_suggestions(scope)
    report, _ = project_knowledge(canonical(), input_coverage_warnings=warnings,
                                  material_suggestions=suggestions)
    assert "remaining_uncertainties" not in report
    assert report["input_coverage_warnings"][0]["packet_id"] == "J08"
    assert [s["scope_id"] for s in report["material_suggestions"]] == ["requested_1"]
    rendered = render_knowledge_markdown(report)
    assert "可补充材料建议" in rendered
    assert "检查远程调用入口" in rendered
    assert "raw_only" in rendered
    assert "已确认样本缺失" not in rendered


def test_real_worker_material_need_remains_actionable_without_gold(tmp_path):
    packet = tmp_path / "J01.md"
    packet.write_text("## F-0101 sub_401010，原文件 L0001–L0002 / 共 2 行\n```text\nL0001: ret;\nL0002: ret;\n```", encoding="utf-8")
    actual = RunEnvelope(packet_name="J01", elapsed_seconds=1, delivery_status=DeliveryStatus.SUCCESS,
                         revision_performed=False, usage=UsageSummary(), result=InvestigationResult(
                             status=InvestigationStatus.COMPLETE_WITH_GAPS, summary="Observed unresolved call",
                             material_needs=[MaterialNeed(
                                 kind="function_code", target="sub_401234",
                                 reason="查明远程线程创建条件", evidence=[EvidenceRef(location="F-0101 L2", observation="call sub_401234")]
                             )],
                         ))
    scope, requests = build_automatic_scope(build_input_inventory([packet]), {"J01": actual})
    assert len(requests) == 1
    assert scope.materials[0].status == "not_in_input"
    suggestions = build_material_suggestions(scope)
    assert len(suggestions) == 1
    assert "查明远程线程创建条件" in suggestions[0].recommendation
    assert "sub_401234" in suggestions[0].recommendation
    report, _ = project_knowledge(canonical(), material_suggestions=suggestions)
    assert "sub_401234" not in json.dumps(report["core_behaviors"] + report["suspected_behaviors"], ensure_ascii=False)


def test_report_files_serialize_suggestions_without_promoting_material_to_behavior(tmp_path):
    scope = EvidenceScope(materials=[entry("requested_1", "partial")])
    suggestions = build_material_suggestions(scope)
    doc, md, _, _ = save_knowledge_outputs(tmp_path, canonical(), material_suggestions=suggestions)
    loaded = json.loads(doc.read_text(encoding="utf-8"))
    assert "remaining_uncertainties" not in loaded
    assert loaded["material_suggestions"][0]["availability"] == "partial"
    assert "如补充" in md.read_text(encoding="utf-8")
    assert "sample_target" in md.read_text(encoding="utf-8")


def test_attested_unavailable_material_remains_a_conditional_hint():
    scope = EvidenceScope(materials=[
        MaterialScopeItem(scope_id="resource_body", label="RCDATA content", status="unavailable",
                          source="operator-checked resource export", details="No resource bytes available"),
        MaterialScopeItem(scope_id="inner_pe", label="Inner PE bytes", status="unavailable",
                          source="operator-checked PE data export", details="Inner PE not present"),
        MaterialScopeItem(scope_id="runtime_logs", label="Runtime log events", status="unknown"),
    ])
    suggestions = build_material_suggestions(scope)
    assert {x.scope_id for x in suggestions} == {"resource_body", "inner_pe"}
    assert all("如后续能提供" in x.recommendation for x in suggestions)
    assert all("样本中没有" not in x.recommendation for x in suggestions)
    report, _ = project_knowledge(canonical(), material_suggestions=suggestions)
    assert not report["core_behaviors"] and not report["suspected_behaviors"]
    assert "runtime_logs" not in {x["scope_id"] for x in report["material_suggestions"]}


def test_delivery_failure_does_not_invent_material_requests_or_behavior_gaps():
    from bounded_investigator.evidence.scope import coverage_warnings

    coverage = [PacketCoverage(packet_id="J02", delivery_status="raw_only")]
    warnings = [item.model_dump() for item in coverage_warnings(coverage)]
    facts = canonical()
    report, audit = project_knowledge(facts, input_coverage_warnings=warnings)
    assert report["material_suggestions"] == []
    assert report["input_coverage_warnings"][0]["packet_id"] == "J02"
    assert audit["source_gap_count"] == len(facts.gaps)
    assert not any(x["kind"] == "historical_gap" and "J02" in x.get("source_packets", [])
                   for x in report["investigation_leads"])
