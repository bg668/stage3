import json
from pathlib import Path

from typer.testing import CliRunner

from bounded_investigator.cli import app, _save_final_outputs
from bounded_investigator.config import AppConfig, load_config
from bounded_investigator.knowledge_report import project_knowledge, render_knowledge_markdown
from bounded_investigator.schemas import (
    CanonicalArtifact, CanonicalClaim, Confidence, DeliveryStatus,
    EvidenceLevel, EvidenceRef, GlobalGap, GlobalReconciliationEnvelope,
    GlobalReconciliationResult, ReconciliationStatus, UnresolvedConflict,
)


def claim(cid, level, sid, *, superseded=None):
    return CanonicalClaim(
        canonical_id=cid,
        behavior={
            "G:C001": "远程线程注入",
            "G:C002": "凭据传输",
            "G:C003": "远程执行线索",
            "G:C004": "配置处理",
        }[cid],
        condition="仅当模式符合", source="内存", action="CreateRemoteThread",
        sink="新线程", evidence=[EvidenceRef(location="F-0304 L15-L22", observation="创建线程")],
        confidence=Confidence.HIGH,
        evidence_level=level,
        limitations=["静态分析不能证明运行时必然成功", "关键调用者尚未恢复"] if cid == "G:C002" else [],
        source_claim_ids=[sid], superseded_claim_ids=superseded or [],
        resolution="superseded_weaker_claim" if superseded else "unchanged",
        resolution_reason="original source",
    )


def artifact(aid, source, value, role, spec):
    return CanonicalArtifact(
        canonical_id=aid, kind="file", value=value, role=role,
        detection_specificity=spec, meaning="source artifact",
        evidence=[EvidenceRef(location="F-0500 L18", observation="literal")],
        confidence=Confidence.HIGH, source_artifact_ids=[source],
    )


def sample():
    return GlobalReconciliationResult(
        status=ReconciliationStatus.COMPLETE_WITH_CONFLICTS,
        summary="Static evidence",
        canonical_claims=[
            claim("G:C001", EvidenceLevel.CONFIRMED, "J14:C001", superseded=["J13:C001"]),
            claim("G:C002", EvidenceLevel.SUSPECTED, "J02:C001"),
            claim("G:C003", EvidenceLevel.LEAD, "J03:C001"),
            claim("G:C004", EvidenceLevel.UNCLASSIFIED, "J04:C001"),
        ],
        canonical_artifacts=[
            artifact("G:A001", "J01:A001", "%TEMP%\\NOIP.abc", "created_output", "high"),
            artifact("G:A002", "J02:A001", "C:\\Windows\\System32", "system_path", "none"),
            artifact("G:A003", "J03:A001", "generic.exe", "read_source", "high"),
        ],
        gaps=[GlobalGap(
            gap_id="R:G001", question="后续如何解密", reason="当时只读取前半段",
            gap_type="investigation", source_packets=["J11"], impact="major",
        )],
        unresolved_conflicts=[UnresolvedConflict(
            claim_ids=["J15:C001"], description="Conflicting execution mode",
            reason="not comparable", impact="major",
        )],
    )


def test_three_levels_preserve_all_sources_and_no_auto_upgrade_from_high_confidence():
    report, audit = project_knowledge(sample(), source_claim_count=6, source_artifact_count=3)
    assert report["schema"] == "report_ready_v2"
    assert audit["passed"] is True
    assert [x["canonical_id"] for x in report["core_behaviors"]] == ["G:C001"]
    assert [x["canonical_id"] for x in report["suspected_behaviors"]] == ["G:C002"]
    lead_kinds = {x["kind"] for x in report["investigation_leads"]}
    assert {"bounded_claim", "legacy_unclassified_claim", "historical_gap", "unresolved_conflict"} <= lead_kinds
    assert audit["claim_dispositions"]["J13:C001"] == "superseded_audit"
    assert audit["claim_dispositions"]["J15:C001"] == "conflict_audit"
    assert audit["source_claim_count"] == 6
    assert audit["source_artifact_count"] == 3
    assert audit["source_gap_count"] == 1
    assert len(report["iocs"]) == 1
    assert len(report["detection_features"]) == 1
    assert len(report["context_artifacts"]) == 1
    assert "remaining_uncertainties" not in report


def test_markdown_has_distinct_locations_and_does_not_repeat_generic_caveats():
    report, audit = project_knowledge(sample())
    md = render_knowledge_markdown(report)
    assert "## 一、核心已确认行为" in md
    assert "## 二、疑似行为" in md
    assert "疑似凭据传输" in md
    assert "## 附录 A：待调查与历史线索" in md
    assert "后续如何解密" in md
    assert "关键调用者尚未恢复" in md
    assert "静态分析不能证明运行时必然成功" not in md
    assert "## 三、IOC 与检测特征" in md
    assert "%TEMP%\\NOIP.abc" in md
    assert "历史未分级" in md



def test_report_renders_real_code_for_confirmed_and_suspected_behaviors(tmp_path):
    packets = tmp_path / "packets"
    packets.mkdir()
    (packets / "J00.md").write_text(
        "## F-0304 sub_remote\n"
        "```c\n"
        "L0015: VirtualAllocEx(h, 0, n, 0x3000, 0x40);\n"
        "L0016: WriteProcessMemory(h, p, buf, n, 0);\n"
        "L0017: CreateRemoteThread(h, 0, 0, start, 0, 0, 0);\n"
        "L0018: return 1;\n"
        "L0019: int rc = 0;\n"
        "L0020: rc = GetLastError();\n"
        "L0021: return rc;\n"
        "L0022: return 0;\n"
        "```\n",
        encoding="utf-8",
    )
    result = GlobalReconciliationResult(
        status=ReconciliationStatus.COMPLETE,
        summary="x",
        canonical_claims=[
            claim("G:C001", EvidenceLevel.CONFIRMED, "J00:C001"),
            claim("G:C002", EvidenceLevel.SUSPECTED, "J00:C002"),
        ],
    )
    report, audit = project_knowledge(result, packet_source_dir=packets)
    assert audit["passed"] is True
    assert set(report["code_excerpts"]) == {"G:C001", "G:C002"}
    assert all(report["code_excerpts"][cid] for cid in ("G:C001", "G:C002"))
    md = render_knowledge_markdown(report)
    assert md.count("关键源码") == 2
    assert "VirtualAllocEx" in md
    assert "CreateRemoteThread" in md


def test_leads_do_not_receive_code_excerpts(tmp_path):
    packets = tmp_path / "packets"
    packets.mkdir()
    (packets / "J00.md").write_text(
        "## F-0304 sub_remote\n```c\nL0015: CreateRemoteThread(...);\n```\n",
        encoding="utf-8",
    )
    result = GlobalReconciliationResult(
        status=ReconciliationStatus.COMPLETE,
        summary="x",
        canonical_claims=[
            claim("G:C003", EvidenceLevel.LEAD, "J00:C003"),
        ],
    )
    report, _ = project_knowledge(result, packet_source_dir=packets)
    assert report["code_excerpts"] == {}

def test_audit_catches_missing_or_duplicate_provenance():
    result = sample()
    _, audit = project_knowledge(result, source_claim_count=7, source_artifact_count=3)
    assert audit["passed"] is False
    assert any("数量不匹配" in x for x in audit["issues"])
    result.canonical_claims[1].source_claim_ids = ["J14:C001"]  # reused by another canonical claim
    _, audit = project_knowledge(result)
    assert audit["passed"] is False
    assert audit["destination_collisions"]


def test_reports_keep_unaccounted_and_missing_inputs_visible():
    result = sample()
    result.unaccounted_input_claim_ids.append("J99:C001")
    result.unavailable_packets.append("J08")
    report, audit = project_knowledge(result)
    assert report["status"] == "incomplete_provenance"
    assert any(x["kind"] == "unaccounted_claim" for x in report["investigation_leads"])
    assert any(x.get("packet_id") == "J08" for x in report["input_coverage_warnings"])
    assert audit["passed"] is False


def test_legacy_final_assessment_config_does_not_require_model_stage(tmp_path):
    base = load_config(Path(__file__).parents[1] / "config.example.yaml")
    raw = base.model_dump()
    raw["final_evidence_assessment"]["enabled"] = True
    raw["stages"].pop("final_evidence_assessment", None)
    old_cfg = AppConfig.model_validate(raw)
    assert old_cfg.final_evidence_assessment.enabled  # can parse old config but not used


def test_cli_report_is_offline_without_model_calls(tmp_path, monkeypatch):
    global_path = tmp_path / "_global_reconciliation.json"
    result = sample()
    env = GlobalReconciliationEnvelope(
        delivery_status=DeliveryStatus.SUCCESS, elapsed_seconds=1, result=result,
        provenance_valid=True, source_claim_count=6, source_artifact_count=3,
    )
    global_path.write_text(env.model_dump_json(indent=2), encoding="utf-8")
    cfg = Path(__file__).parents[1] / "config.yaml"
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    output = tmp_path / "report"
    reply = CliRunner().invoke(app, ["report", str(global_path), "--config", str(cfg), "--output-dir", str(output)])
    assert reply.exit_code == 0, reply.output or repr(reply.exception)
    summary = json.loads((output / "_report_summary.json").read_text(encoding="utf-8"))
    assert summary["completed"] and summary["model_requests"] == 0
    assert (output / "_knowledge_audit.json").exists()
    assert not (output / "_final_assessment.json").exists()
    doc = json.loads((output / "_report_ready.json").read_text(encoding="utf-8"))
    assert len(doc["core_behaviors"]) == 1
    assert len(doc["suspected_behaviors"]) == 1


def test_reconciliation_prefers_conservative_explicit_epistemic_level():
    from bounded_investigator.reconciliation import (
        assemble_reconciliation_result, project_reconciliation_input,
        validate_and_finalize_reconciliation,
    )
    from bounded_investigator.schemas import (
        BehaviorClaim, InvestigationResult, InvestigationStatus, RunEnvelope,
        UsageSummary, GlobalReconciliationDecisionResult,
    )

    def envelope(pid, level):
        ev = EvidenceRef(location=f"F-{pid} L1", observation="confirmed function API")
        return RunEnvelope(
            packet_name=pid, elapsed_seconds=1, revision_performed=False, usage=UsageSummary(),
            result=InvestigationResult(
                status=InvestigationStatus.COMPLETE, summary="same", claims=[BehaviorClaim(
                    behavior="same behavior", condition="if X", source="source", action="action", sink="sink",
                    evidence=[ev], confidence=Confidence.HIGH, evidence_level=level,
                )],
            ),
        )

    projection = project_reconciliation_input({
        "J01": envelope("J01", EvidenceLevel.CONFIRMED),
        "J02": envelope("J02", EvidenceLevel.SUSPECTED),
    }, {})
    assert len(projection.reduced_claims) == 1
    key = next(iter(projection.reduced_claims))
    assert projection.reduced_claims[key].claim.evidence_level == EvidenceLevel.SUSPECTED
    final = validate_and_finalize_reconciliation(
        assemble_reconciliation_result(
            GlobalReconciliationDecisionResult(keep_claim_ids=[key]), projection,
        ), projection,
    )
    assert final.canonical_claims[0].evidence_level == EvidenceLevel.SUSPECTED


def test_network_ioc_is_not_forced_into_a_generic_detection_feature():
    result = sample()
    result.canonical_artifacts.append(CanonicalArtifact(
        canonical_id="G:A004", kind="network", value="c2.example.net",
        role="other", detection_specificity="high", meaning="independently evidenced network endpoint",
        evidence=[EvidenceRef(location="F-0001 L7", observation="literal endpoint")],
        confidence=Confidence.HIGH, source_artifact_ids=["J05:A001"],
    ))
    report, audit = project_knowledge(result, source_artifact_count=4)
    assert audit["passed"]
    assert "c2.example.net" in {x["value"] for x in report["iocs"]}
