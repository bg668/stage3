from __future__ import annotations

import asyncio
import importlib
import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from bounded_investigator.artifact_normalization import (
    normalize_investigation_result, split_artifact,
)
from bounded_investigator.json_compat import ensure_json_keyword
from bounded_investigator.material_inventory import build_automatic_scope, build_input_inventory
from bounded_investigator.reconciliation import project_reconciliation_input
from bounded_investigator.review_guard import constrain_reviewer_scope
from bounded_investigator.schemas import (
    ArtifactFinding, CoverageReview, DeliveryStatus, EvidenceRef, InvestigationResult,
    InvestigationStatus, MaterialNeed, MaterialTarget, ReviewIssue, RunEnvelope,
    UsageSummary,
)


def packet(tmp_path: Path, name: str, ident: str, symbol: str, a: int, b: int, total: int) -> Path:
    p = tmp_path / f"{name}.md"
    p.write_text(
        f"## {ident} {symbol}，原文件 L{a:04d}–L{b:04d} / 共 {total} 行\n"
        "```text\n" + "\n".join(f"L{i:04d}: return 0;" for i in range(a,b+1)) + "\n```\n",
        encoding="utf-8",
    )
    return p


def _evidence() -> list[EvidenceRef]:
    return [EvidenceRef(location="F-0483 L275-L522", observation="read and decrypt")]


def _env(name="J00", needs=None, artifacts=None) -> RunEnvelope:
    return RunEnvelope(
        packet_name=name, elapsed_seconds=1, revision_performed=False,
        delivery_status=DeliveryStatus.SUCCESS, usage=UsageSummary(),
        result=InvestigationResult(status=InvestigationStatus.COMPLETE_WITH_GAPS,
                                   summary="analysis", material_needs=needs or [], artifacts=artifacts or []),
    )


def _need(target="", targets=None) -> MaterialNeed:
    return MaterialNeed(kind="function_code", target=target, targets=targets or [],
                        reason="Need source", evidence=_evidence())


def _artifact(value: str, kind="file") -> ArtifactFinding:
    return ArtifactFinding(
        kind=kind, value=value, role="created_output", detection_specificity="high",
        meaning="file output", evidence=_evidence(), confidence="high",
    )


def test_function_targets_are_structured_and_required():
    need = _need(targets=[
        MaterialTarget(identifier="sub_409EF8", start_line=275, end_line=522),
        MaterialTarget(identifier="F-0307"),
    ])
    assert len(need.targets) == 2
    assert need.target == ""
    with pytest.raises(ValidationError):
        MaterialTarget(identifier="F-0010", start_line=3)
    with pytest.raises(ValidationError):
        MaterialNeed(kind="function_code", reason="unknown", evidence=_evidence())


def test_legacy_comma_multi_function_request_is_exploded(tmp_path):
    code = packet(tmp_path, "J06", "F-0480", "sub_409AD4", 1, 33, 33)
    scope, requests = build_automatic_scope(build_input_inventory([code]), {"J00": _env(needs=[
        _need("sub_409AD4, sub_409D28; sub_409EF8")
    ])})
    assert len(requests) == 3
    assert len(scope.materials) == 3
    matches = {m.target: m.status for m in scope.materials}
    assert matches["sub_409ad4"] == "available"
    assert matches["sub_409d28"] == "not_in_input"


def test_function_range_can_be_satisfied_across_packets(tmp_path):
    p1 = packet(tmp_path, "J11", "F-0483", "sub_409EF8", 1, 274, 522)
    p2 = packet(tmp_path, "J12", "F-0483", "sub_409EF8", 275, 522, 522)
    scope, needs = build_automatic_scope(build_input_inventory([p1,p2]), {"J11": _env(needs=[
        _need("sub_409EF8 L0275-L0522")
    ])})
    assert len(needs) == 1
    assert (needs[0].requested_start_line, needs[0].requested_end_line) == (275,522)
    assert scope.materials[0].status == "available"
    assert "248/248" in scope.materials[0].details


def test_missing_source_range_is_partial_not_a_missing_function(tmp_path):
    p1 = packet(tmp_path, "J11", "F-0483", "sub_409EF8", 1, 274, 522)
    scope, _ = build_automatic_scope(build_input_inventory([p1]), {"J11": _env(needs=[
        _need(targets=[MaterialTarget(identifier="F-0483", start_line=275,end_line=522)])
    ])})
    assert scope.materials[0].status == "partial"
    assert "0/248" in scope.materials[0].details


def test_structured_grouped_need_creates_one_material_per_target(tmp_path):
    packet_path = packet(tmp_path, "J06", "F-0480", "sub_409AD4",1,33,33)
    scope, requests = build_automatic_scope(build_input_inventory([packet_path]), {"J00": _env(needs=[
        _need(targets=[MaterialTarget(identifier="F-0480"),MaterialTarget(identifier="F-0481")])
    ])})
    assert len(scope.materials) == len(requests) == 2
    assert {m.status for m in scope.materials} == {"available", "not_in_input"}


def test_atomic_file_artifact_list_split_without_rewriting_evidence():
    original = _artifact("NOIP.abc; MSN.abc; FIREFOX.abc")
    parts = split_artifact(original)
    assert [x.value for x in parts] == ["NOIP.abc", "MSN.abc", "FIREFOX.abc"]
    assert all(x.evidence == original.evidence for x in parts)
    assert [x.value for x in split_artifact(_artifact("one.abc, two.abc"))] == ["one.abc","two.abc"]
    assert split_artifact(_artifact("http://a.example/x?a=1;b=2"))[0].value.endswith("b=2")
    assert split_artifact(_artifact(r"C:\some, dir\only.abc"))[0].value == r"C:\some, dir\only.abc"


def test_runtime_and_legacy_reconciliation_both_normalize_atomic_values():
    original = _env(artifacts=[_artifact("NOIP.abc; MSN.abc")]).result
    updated = normalize_investigation_result(original)
    assert [a.value for a in updated.artifacts] == ["NOIP.abc", "MSN.abc"]
    assert original.artifacts[0].value == "NOIP.abc; MSN.abc"
    projection = project_reconciliation_input({"J00": _env(artifacts=[original.artifacts[0]])}, {})
    assert sorted(projection.artifacts) == ["J00:A001:P01", "J00:A001:P02"]
    assert sorted(a.value for a in projection.artifacts.values()) == ["MSN.abc", "NOIP.abc"]


def _review(issue: ReviewIssue) -> CoverageReview:
    return CoverageReview(requires_revision=True, issues=[issue], review_summary="issue")


def test_upstream_only_reviewer_omission_cannot_force_revision(tmp_path):
    content = packet(tmp_path,"J01","F-0101","sub_401010",1,3,3).read_text(encoding="utf-8")
    issue = ReviewIssue(
        issue_id="R1",scope="packet_internal",severity="major",kind="omitted_claim",
        description="Did not repeat upstream Hook", required_fix="repeat J00 conclusion",
        evidence=[EvidenceRef(location="F-0500 L70",observation="upstream")],
    )
    revised=constrain_reviewer_scope(_review(issue),content)
    assert not revised.requires_revision
    assert revised.issues[0].severity=="minor"
    assert revised.issues[0].scope=="outside_packet"


def test_current_material_omission_still_triggers_reviser(tmp_path):
    content = packet(tmp_path,"J01","F-0101","sub_401010",1,3,3).read_text(encoding="utf-8")
    issue=ReviewIssue(issue_id="R1",scope="packet_internal",severity="major",kind="omitted_claim",
                      description="Missing local behavior",required_fix="add",evidence=[EvidenceRef(location="F-0101 L2",observation="call")])
    assert constrain_reviewer_scope(_review(issue),content).requires_revision
    # Don't guess boundaries for consumers that use unheaded code.
    assert constrain_reviewer_scope(_review(issue),"int main() {return 1;}").requires_revision


def test_missing_local_evidence_cannot_force_revision(tmp_path):
    content=packet(tmp_path,"J01","F-0101","sub_401010",1,3,3).read_text(encoding="utf-8")
    issue=ReviewIssue(issue_id="R1",scope="packet_internal",severity="major",kind="omitted_artifact",
                      description="unsubstantiated",required_fix="repeat",evidence=[])
    assert not constrain_reviewer_scope(_review(issue),content).requires_revision


def test_json_object_keyword_is_in_user_input_for_prompted_and_native():
    for mode in ("prompted", "native"):
        rendered = ensure_json_keyword("<task>resolve relationships</task>",mode)
        assert "JSON" in rendered
        assert rendered.endswith("<task>resolve relationships</task>")
        assert ensure_json_keyword(rendered,mode) == rendered
    assert ensure_json_keyword("<task>resolve</task>","tool") == "<task>resolve</task>"


def test_reconcile_cli_runs_from_saved_envelopes_without_worker(monkeypatch,tmp_path):
    # Stub only the uninstalled external PydanticAI runtime. The CLI and
    # entire persisted-file loading/replay boundary are exercised for real.
    seen={}
    class FakeWorkflow:
        def __init__(self, config):pass
        async def run(self, **kwargs):
            raise AssertionError("replay must not execute Worker")
        async def run_global_reconciliation(self, **kwargs):
            seen.update(kwargs)
            return SimpleNamespace(
                final_assessment=None,result=None,delivery_status=DeliveryStatus.SUCCESS,
                provenance_valid=True,usage=UsageSummary(),
                model_dump_json=lambda **kwargs:'{"delivery_status":"success"}'
            )
    stub=types.ModuleType("bounded_investigator.workflow")
    stub.BoundedInvestigationWorkflow=FakeWorkflow
    monkeypatch.setitem(sys.modules,"bounded_investigator.workflow",stub)
    sys.modules.pop("bounded_investigator.cli",None)
    try:
        cli=importlib.import_module("bounded_investigator.cli")
        src=tmp_path/"saved"
        src.mkdir()
        (src/"J00.json").write_text(_env().model_dump_json(),encoding="utf-8")
        (src/"J01.error.json").write_text('{"error":"failed"}',encoding="utf-8")
        out=tmp_path/"replayed"
        cfg=SimpleNamespace(global_reconciliation=SimpleNamespace(enabled=True))
        summary=asyncio.run(cli._reconcile_saved_results(packet_results_dir=src,output_dir=out,config=cfg))
        assert summary["completed_packets"] == ["J00"]
        assert "J01" in summary["failed_packets"]
        assert "J00" in seen["completed_results"]
        assert (out/"_global_reconciliation.json").is_file()
        assert (out/"_reconcile_summary.json").is_file()
    finally:
        sys.modules.pop("bounded_investigator.cli",None)
