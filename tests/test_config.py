from pathlib import Path

import pytest

from bounded_investigator.config import load_config


def _config_text(reasoning: str = "omit") -> str:
    return f"""
providers:
  ds:
    type: deepseek
    base_url: https://api.deepseek.com
    api_key_env: DS_KEY
models:
  strong:
    provider: ds
    model_name: deepseek-chat
    api_mode: chat
    output_mode: tool
    reasoning: {reasoning}
    stream_output: true
  fast:
    provider: ds
    model_name: deepseek-chat
    reasoning: omit
    stream_output: false
  repair:
    provider: ds
    model_name: deepseek-chat
    reasoning: omit
    stream_output: false
stages:
  analyzer:
    model: strong
    max_output_tokens: 4096
  reviewer:
    model: fast
    max_output_tokens: 1536
  reviser:
    model: strong
    max_output_tokens: 2048
  format_repair:
    model: repair
    max_output_tokens: 4096
workflow:
  max_model_requests: 3
"""


def test_load_provider_model_stage_config(tmp_path: Path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(_config_text("medium"), encoding="utf-8")
    monkeypatch.setenv("DS_KEY", "test-key")
    loaded = load_config(cfg)

    stage, model, provider = loaded.stage("reviewer")
    assert stage.max_output_tokens == 1536
    assert stage.model == "fast"
    assert model.model_name == "deepseek-chat"
    assert model.stream_output is False
    assert provider.type == "deepseek"
    assert loaded.provider_api_key("ds") == "test-key"


def test_reasoning_off_means_omit(tmp_path: Path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(_config_text("off"), encoding="utf-8")
    monkeypatch.setenv("DS_KEY", "test-key")
    loaded = load_config(cfg)
    assert loaded.models["strong"].reasoning == "omit"


def test_reasoning_disabled_is_explicit(tmp_path: Path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(_config_text("disabled"), encoding="utf-8")
    monkeypatch.setenv("DS_KEY", "test-key")
    loaded = load_config(cfg)
    assert loaded.models["strong"].reasoning == "disabled"


def test_missing_stage_model_is_rejected(tmp_path: Path):
    text = _config_text().replace("model: fast\n    max_output_tokens: 1536", "model: missing\n    max_output_tokens: 1536")
    cfg = tmp_path / "config.yaml"
    cfg.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError):
        load_config(cfg)


def test_global_reconciliation_stage_can_be_enabled(tmp_path: Path, monkeypatch):
    text = _config_text() + """
models:
"""
    # Build a complete config explicitly because YAML duplicate keys are not portable.
    cfg_text = _config_text().replace(
        "  repair:\n    provider: ds\n    model_name: deepseek-chat\n    reasoning: omit\n    stream_output: false\n",
        "  repair:\n    provider: ds\n    model_name: deepseek-chat\n    reasoning: omit\n    stream_output: false\n"
        "  global:\n    provider: ds\n    model_name: deepseek-chat\n    reasoning: omit\n    stream_output: false\n"
    ).replace(
        "  format_repair:\n    model: repair\n    max_output_tokens: 4096\n",
        "  format_repair:\n    model: repair\n    max_output_tokens: 4096\n"
        "  global_reconciliation:\n    model: global\n    max_output_tokens: 4096\n"
    ) + """
global_reconciliation:
  enabled: true
  max_model_requests: 1
  total_tokens_limit: 16000
  wall_clock_seconds: 30
"""
    cfg = tmp_path / "config.yaml"
    cfg.write_text(cfg_text, encoding="utf-8")
    monkeypatch.setenv("DS_KEY", "test-key")
    loaded = load_config(cfg)
    assert loaded.global_reconciliation.enabled is True
    stage, model, _ = loaded.stage("global_reconciliation")
    assert stage.model == "global"
    assert model.model_name == "deepseek-chat"


def test_gap_reconciliation_stage_can_be_enabled(tmp_path: Path, monkeypatch):
    cfg_text = _config_text().replace(
        "  repair:\n    provider: ds\n    model_name: deepseek-chat\n    reasoning: omit\n    stream_output: false\n",
        "  repair:\n    provider: ds\n    model_name: deepseek-chat\n    reasoning: omit\n    stream_output: false\n"
        "  global:\n    provider: ds\n    model_name: deepseek-chat\n    reasoning: omit\n    stream_output: false\n"
    ).replace(
        "  format_repair:\n    model: repair\n    max_output_tokens: 4096\n",
        "  format_repair:\n    model: repair\n    max_output_tokens: 4096\n"
        "  global_reconciliation:\n    model: global\n    max_output_tokens: 4096\n"
        "  gap_reconciliation:\n    model: global\n    max_output_tokens: 2048\n"
    ) + """
global_reconciliation:
  enabled: true
  max_model_requests: 1
  total_tokens_limit: 16000
  wall_clock_seconds: 30
gap_reconciliation:
  enabled: true
  max_model_requests: 1
  total_tokens_limit: 8000
  wall_clock_seconds: 30
"""
    cfg = tmp_path / "config.yaml"
    cfg.write_text(cfg_text, encoding="utf-8")
    monkeypatch.setenv("DS_KEY", "test-key")
    loaded = load_config(cfg)
    assert loaded.gap_reconciliation.enabled is True
    stage, model, _ = loaded.stage("gap_reconciliation")
    assert stage.model == "global"
    assert stage.max_output_tokens == 2048
    assert model.model_name == "deepseek-chat"
