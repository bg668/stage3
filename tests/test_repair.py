from bounded_investigator.repair import validate_candidate
from bounded_investigator.schemas import InvestigationResult


def test_deterministic_cleanup_strips_fence_and_validates():
    raw = """```json
{"status":"complete","summary":"ok","claims":[],"artifacts":[],"gaps":[],"handoffs":[]}
```"""
    result = validate_candidate(InvestigationResult, raw)
    assert result.output is not None
    assert result.output.summary == "ok"


def test_semantic_missing_required_field_is_not_invented():
    raw = '{"status":"complete"}'
    result = validate_candidate(InvestigationResult, raw)
    assert result.output is None
    assert "summary" in (result.error or "")


def test_noisy_prefix_can_extract_first_json_object():
    raw = 'Result follows: {"status":"complete","summary":"ok","claims":[],"artifacts":[],"gaps":[],"handoffs":[]} trailing'
    result = validate_candidate(InvestigationResult, raw)
    assert result.output is not None

from bounded_investigator.repair import salvage_investigation_result


def test_required_missing_is_detected_without_repair_guessing():
    raw = '{"status":"complete","claims":[],"artifacts":[],"gaps":[],"handoffs":[]}'
    result = validate_candidate(InvestigationResult, raw)
    assert result.output is None
    assert result.has_required_missing is True
    assert any(e["type"] == "missing" and e["loc"] == ["summary"] for e in result.validation_errors or [])


def test_partial_salvage_keeps_only_independently_valid_items():
    raw = '''{
      "status":"complete",
      "summary":"partial",
      "claims":[
        {
          "behavior":"file deletion",
          "condition":"none observed",
          "source":"path",
          "action":"DeleteFileA",
          "sink":"file removed",
          "evidence":[{"location":"F-1 L1","observation":"DeleteFileA(path)"}],
          "confidence":"high",
          "limitations":[]
        },
        {"behavior":"broken claim"}
      ],
      "artifacts":[
        {
          "kind":"file","value":"x.dat","role":"created_output",
          "detection_specificity":"high","meaning":"output",
          "evidence":[{"location":"F-2 L2","observation":"writes x.dat"}],
          "confidence":"high"
        },
        {"kind":"file","value":"broken"}
      ]
    }'''
    partial = salvage_investigation_result(raw)
    assert partial is not None
    assert len(partial.claims) == 1
    assert len(partial.artifacts) == 1
    assert partial.rejected_claims == 1
    assert partial.rejected_artifacts == 1
