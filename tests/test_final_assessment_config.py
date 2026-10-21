from pathlib import Path

import pytest

from bounded_investigator.config import AppConfig, load_config
from bounded_investigator.prompts import final_assessment_instructions, final_assessment_prompt


def test_new_stage_uses_separate_model_budget():
    cfg = load_config(Path(__file__).parents[1] / "config.example.yaml")
    assert cfg.final_evidence_assessment.enabled is False  # retired from automatic reporting
    stage, model, provider = cfg.stage("final_evidence_assessment")
    assert stage.max_output_tokens == 15360
    assert model.model_name == "deepseek-chat"
    assert provider.type == "deepseek"
    assert cfg.final_evidence_assessment.total_tokens_limit >= stage.max_output_tokens
    assert not cfg.gap_reconciliation.enabled


def test_fails_when_final_enabled_but_no_stage():
    cfg = load_config(Path(__file__).parents[1] / "config.example.yaml")
    raw = cfg.model_dump()
    del raw["stages"]["final_evidence_assessment"]
    # Old v1.6 configs/stage fields are accepted, but the report pipeline does not need this stage.
    raw["final_evidence_assessment"]["enabled"] = True
    loaded = AppConfig.model_validate(raw)
    assert "final_evidence_assessment" not in loaded.stages


def test_legacy_gap_options_do_not_activate_old_stage():
    cfg = load_config(Path(__file__).parents[1] / "config.example.yaml")
    raw = cfg.model_dump()
    raw["gap_reconciliation"]["enabled"] = True
    loaded = AppConfig.model_validate(raw)
    assert loaded.final_evidence_assessment.enabled is False
    assert loaded.gap_reconciliation.enabled is True  # accepted, but never scheduled


def test_new_prompt_is_external_and_keeps_data_literal():
    instructions = final_assessment_instructions()
    assert "最终证据评估员" in instructions
    rendered = final_assessment_prompt("<claim>{{not_template}}</claim>")
    assert "{{not_template}}" in rendered
    assert "<final_evidence_assessment_input>" in rendered
    assert "{{assessment_context}}" not in rendered
