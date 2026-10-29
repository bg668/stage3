from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from bounded_investigator.evidence.scope import (
    EvidenceScope, MaterialScopeItem, PacketCoverage,
    load_evidence_scope, packet_coverage_from_directory, packet_coverage_from_results,
)
from bounded_investigator.schemas import DeliveryStatus, RunEnvelope, UsageSummary


def test_scope_is_unknown_unless_explicitly_attested(tmp_path):
    assert load_evidence_scope(None).materials == []
    with pytest.raises(ValidationError, match="source"):
        MaterialScopeItem(scope_id="inner_pe", label="Inner PE", status="unavailable")
    with pytest.raises(ValidationError, match="details"):
        MaterialScopeItem(scope_id="inner_pe", label="Inner PE", status="unavailable", source="export manifest")
    unknown = MaterialScopeItem(scope_id="inner_pe", label="Inner PE", status="unknown")
    assert unknown.source is None
    with pytest.raises(ValidationError, match="Duplicate"):
        EvidenceScope(materials=[unknown, unknown])


def test_yaml_and_json_scope_formats(tmp_path):
    content = {
        "materials": [
            {"scope_id": "resource_body", "label": "RCDATA body", "status": "unavailable",
             "source": "export manifest resource count", "details": "No body bytes in export"},
            {"scope_id": "runtime_logs", "label": "sandbox events", "status": "unknown"},
        ]
    }
    json_path = tmp_path / "scope.json"
    json_path.write_text(json.dumps(content), encoding="utf-8")
    yaml_path = tmp_path / "scope.yaml"
    yaml_path.write_text('materials:\n  - scope_id: resource_body\n    label: RCDATA body\n    status: unavailable\n    source: export manifest resource count\n    details: No body bytes in export\n', encoding="utf-8")
    assert load_evidence_scope(json_path).materials[0].status == "unavailable"
    assert load_evidence_scope(yaml_path).materials[0].scope_id == "resource_body"


def test_raw_only_packet_delivery_is_not_source_unavailable(tmp_path):
    raw = RunEnvelope(packet_name="J02", elapsed_seconds=1, revision_performed=False,
                      delivery_status=DeliveryStatus.RAW_ONLY, usage=UsageSummary(), result=None)
    (tmp_path / "J02.json").write_text(raw.model_dump_json(), encoding="utf-8")
    saved = packet_coverage_from_directory(tmp_path)
    assert saved[0].delivery_status == "raw_only"
    assert "source code" in saved[0].note
    live = packet_coverage_from_results({"J02": raw}, {"J08": "format failed"})
    assert {x.packet_id: x.delivery_status for x in live} == {
        "J02": "raw_only", "J08": "execution_failed",
    }
