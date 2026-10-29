from pathlib import Path

import pytest
from pydantic import ValidationError

from bounded_investigator.config import AppConfig, load_config


@pytest.mark.parametrize("option", ["final_evidence_assessment", "gap_reconciliation", "unknown_option"])
def test_removed_or_unknown_top_level_configuration_is_rejected(option):
    config = load_config(Path(__file__).parents[1] / "config.example.yaml").model_dump()
    config[option] = {"enabled": True}
    with pytest.raises(ValidationError, match=option):
        AppConfig.model_validate(config)


@pytest.mark.parametrize("stage", ["final_evidence_assessment", "gap_reconciliation"])
def test_removed_stage_configuration_is_rejected(stage):
    config = load_config(Path(__file__).parents[1] / "config.example.yaml").model_dump()
    config["stages"][stage] = {"model": "investigator", "max_output_tokens": 1024}
    with pytest.raises(ValidationError, match="literal_error"):
        AppConfig.model_validate(config)
