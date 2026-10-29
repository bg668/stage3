from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from bounded_investigator.reporting.material_suggestions import build_material_suggestions
from bounded_investigator.evidence.inventory import (
    _function_lookup, build_input_inventory, build_automatic_scope,
)
from bounded_investigator.schemas import (
    DeliveryStatus, EvidenceRef,
    HandoffItem, InvestigationResult, InvestigationStatus, MaterialNeed,
    RunEnvelope, UsageSummary,
)


def packet(tmp_path: Path, name: str, pieces: list[tuple[str, str, int, int, int]]) -> Path:
    text = [f"# packet {name}", "Not evidence: F-0777 is only mentioned here."]
    for identifier, symbol, start, end, total in pieces:
        text.append(f"## {identifier} {symbol}，原文件 L{start:04d}–L{end:04d} / 共 {total} 行")
        text.append("```text\n" + "\n".join(f"L{i:04d}: x();" for i in range(start, end+1)) + "\n```")
    path = tmp_path / f"{name}.md"
    path.write_text("\n".join(text), encoding="utf-8")
    return path


def result(packet_id: str, needs=None, handoffs=None):
    return RunEnvelope(
        packet_name=packet_id, elapsed_seconds=1.0, revision_performed=False,
        delivery_status=DeliveryStatus.SUCCESS, usage=UsageSummary(),
        result=InvestigationResult(
            status=InvestigationStatus.COMPLETE_WITH_GAPS,
            summary="Unresolved material", material_needs=needs or [], handoffs=handoffs or [],
        ),
    )


def need(kind: str, target: str, impact="major") -> MaterialNeed:
    return MaterialNeed(kind=kind, target=target, reason="Needed to resolve behavior",
                        impact=impact, evidence=[EvidenceRef(location="F-0101 L1", observation="call")])


def test_inventory_merges_split_code_but_not_reference_mentions(tmp_path):
    p1 = packet(tmp_path, "J00", [("F-0101", "sub_4ABCD", 1, 3, 6)])
    p2 = packet(tmp_path, "J01", [("F-0101", "sub_4ABCD", 4, 6, 6)])
    inventory = build_input_inventory([p1, p2])
    assert _function_lookup("F101", inventory)[0] == "available"
    assert _function_lookup("sub_4ABCD", inventory)[0] == "available"
    assert _function_lookup("F-0777", inventory)[0] == "not_in_input"
    inventory2 = build_input_inventory([p1])
    assert _function_lookup("F-0101", inventory2)[0] == "partial"
    assert inventory2.code_sections[0].end_line == 3


def test_material_needs_are_sample_discovered_not_preseeded(tmp_path):
    p = packet(tmp_path, "J00", [("F-0101", "sub_4ABCD", 1, 2, 2)])
    inventory = build_input_inventory([p])
    scope, requests = build_automatic_scope(inventory, {"J00": result("J00")})
    assert requests == []
    assert scope.materials == []
    assert scope.inventory_summary["indexed_function_count"] == 1
    scope, requests = build_automatic_scope(inventory, {"J00": result("J00", [
        need("function_code", "F-0101"),
        need("function_code", "sub_40499"),
        need("resource_bytes", "s01.bin"),
        need("runtime_trace", "activity.etl"),
        need("other", "not-specified"),
    ])})
    assert len(requests) == 5
    by_target = {m.target: m for m in scope.materials}
    assert by_target["F-0101"].status == "available"
    assert by_target["F-0101"].submission_status == "submitted_to_worker"
    assert by_target["sub_40499"].status == "not_in_input"
    assert by_target["s01.bin"].status == "unknown"  # no complete raw-material corpus
    assert by_target["activity.etl"].status == "unknown"
    assert by_target["not-specified"].status == "unknown"
    assert all(x.requested_by_packets == ["J00"] for x in scope.materials)


def test_sidecar_files_matched_only_by_actual_filename(tmp_path):
    packet_file = packet(tmp_path, "J00", [("F-0101", "sub_4ABCD", 1, 2, 2)])
    (tmp_path / "s01.bin").write_bytes(b"\x01\x02\x03")
    inv = build_input_inventory([packet_file])
    scope, _ = build_automatic_scope(inv, {"J00": result("J00", [
        need("resource_bytes", "s01.bin"),
        need("resource_bytes", "not-here.bin"),
    ])})
    actual = {x.target: x for x in scope.materials}
    assert actual["s01.bin"].status == "available"
    assert actual["s01.bin"].submission_status == "not_submitted"
    # Code references aren't resource bytes; no claim that original sample lacks material.
    assert actual["not-here.bin"].status == "unknown"


def test_evidence_directory_scans_explicit_supplied_bytes(tmp_path):
    packet_file = packet(tmp_path, "J00", [("F-0101", "sub_4ABCD", 1, 2, 2)])
    extra = tmp_path / "evidence"
    extra.mkdir()
    (extra / "trace-01.etl").write_bytes(b"\x01\x02")
    inv = build_input_inventory([packet_file], additional_root=extra)
    scope, _ = build_automatic_scope(inv, {"J00": result("J00", [
        need("runtime_trace", "trace-01.etl"),
        need("resource_bytes", "not-provided.bin"),
    ])})
    by_target = {m.target: m for m in scope.materials}
    assert by_target["trace-01.etl"].status == "available"
    assert by_target["not-provided.bin"].status == "not_in_input"
    assert "original sample" in by_target["not-provided.bin"].details


def test_fallback_handoffs_are_conservative_and_raw_only_not_trusted(tmp_path):
    p = packet(tmp_path, "J00", [("F-0101", "sub_4ABCD", 1, 2, 2)])
    inv = build_input_inventory([p])
    env = result("J00", handoffs=[
        HandoffItem(target="F-0150", reason="need callee", expected_value="code", priority="high"),
        HandoffItem(target="F-0151 and F-0152", reason="multi", expected_value="code", priority="high"),
        HandoffItem(target="F-0153", reason="low value", expected_value="code", priority="low"),
    ])
    no_result = RunEnvelope(packet_name="J01", elapsed_seconds=1, revision_performed=False,
                            delivery_status=DeliveryStatus.RAW_ONLY, usage=UsageSummary())
    scope, reqs = build_automatic_scope(inv, {"J00": env, "J01": no_result})
    assert [x.target for x in scope.materials] == ["F-0150"]
    assert reqs[0].source_field == "handoff"


def test_material_hint_does_not_claim_original_sample_absence(tmp_path):
    p = packet(tmp_path, "J00", [("F-0101", "sub_4ABCD", 1, 2, 2)])
    inv = build_input_inventory([p])
    scope, _ = build_automatic_scope(inv, {"J00": result("J00", [need("function_code", "F-0150")])})
    target_scope_id = scope.materials[0].scope_id
    suggestions = build_material_suggestions(scope)
    assert [s.scope_id for s in suggestions] == [target_scope_id]
    assert "如后续能提供" in suggestions[0].recommendation
    assert "样本缺失" not in suggestions[0].recommendation
    assert "this run's scoped input" in scope.materials[0].details
    assert scope.inventory_summary["indexed_function_count"] == 1


def test_material_needs_remain_optional_for_current_model_results():
    old = result("J00").model_dump(mode="json")
    del old["result"]["material_needs"]
    loaded = RunEnvelope.model_validate(old)
    assert loaded.result.material_needs == []


def test_dag_saves_automatic_inventory_without_manual_scope(tmp_path, monkeypatch):
    import importlib
    import sys
    import types

    packet_file = packet(tmp_path, "J00", [("F-0101", "sub_4ABCD", 1, 2, 2)])
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"jobs": [{"job_id": "J00", "depends_on": []}]}), encoding="utf-8")
    calls = []

    class FakeWorkflow:
        def __init__(self, cfg, *, harness):
            assert harness.instructions('analyzer')

        async def run(self, **kwargs):
            return result("J00", [need("function_code", "F-0202")])

        async def run_global_reconciliation(self, **kwargs):
            calls.append(kwargs)
            assert "evidence_scope" not in kwargs and "packet_coverage" not in kwargs
            return SimpleNamespace(
                result=None, delivery_status=DeliveryStatus.SUCCESS,
                elapsed_seconds=0, provenance_valid=True, source_claim_count=0,
                source_artifact_count=0, reduced_claim_count=0, reduced_artifact_count=0,
                artifact_conflict_count=0, source_gap_count=0,
                usage=UsageSummary(), model_dump_json=lambda **kw: '{}',
            )

    # Stub model execution while exercising the scheduler, indexer and serialization.
    stub = types.ModuleType("bounded_investigator.workflow")
    stub.BoundedInvestigationWorkflow = FakeWorkflow
    monkeypatch.setitem(sys.modules, "bounded_investigator.workflow", stub)
    sys.modules.pop("bounded_investigator.orchestration.batch", None)
    batch = importlib.import_module("bounded_investigator.orchestration.batch")
    from bounded_investigator.logging_utils import get_logger
    settings = SimpleNamespace(harness_dir=None, batch=SimpleNamespace(concurrency=1),
                               global_reconciliation=SimpleNamespace(enabled=True))
    output_dir = tmp_path / "result"
    output_dir.mkdir()
    asyncio.run(batch.run_batch_dag(
        packets=[packet_file], output_dir=output_dir, manifest_path=manifest,
        config=settings, log=get_logger("test"), evidence_scope=None,
    ))
    saved = json.loads((output_dir / "_evidence_scope.json").read_text(encoding="utf-8"))
    assert len(saved["materials"]) == 1
    assert saved["materials"][0]["status"] == "not_in_input"
    assert calls[0]["completed_results"]["J00"].result.material_needs[0].target == "F-0202"
    assert (output_dir / "_evidence_inventory.json").exists()
    assert (output_dir / "_material_requests.json").exists()
    sys.modules.pop("bounded_investigator.orchestration.batch", None)


def test_additional_exported_function_file_counts_as_available_not_submitted(tmp_path):
    packet_file = packet(tmp_path, "J00", [("F-0101", "sub_4ABCD", 1, 2, 2)])
    extras = tmp_path / "exports"
    extras.mkdir()
    (extras / "F-0150.txt").write_text("int sub_400000() { return 1; }", encoding="utf-8")
    inventory = build_input_inventory([packet_file], additional_root=extras)
    scope, _ = build_automatic_scope(inventory, {"J00": result("J00", [need("function_code", "F-0150")])})
    assert scope.materials[0].status == "available"
    assert scope.materials[0].submission_status == "not_submitted"
    assert "not independently verified" in scope.materials[0].details
