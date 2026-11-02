from pathlib import Path
import pytest
from typer.testing import CliRunner
from bounded_investigator.cli import app
from bounded_investigator.config import AppConfig, load_config

@pytest.fixture(autouse=True)
def no_external_models():
    from pydantic_ai.models import override_allow_model_requests
    with override_allow_model_requests(False):
        yield

def test_explore_command_exposes_bounded_inputs():
    result = CliRunner().invoke(app, ['explore', '--help'])
    assert result.exit_code == 0
    for option in ['--preprocess', '--triage', '--config', '--output-dir', '--total-tokens']:
        assert option in result.stdout

def test_legacy_config_does_not_require_planner():
    data = load_config('config.example.yaml').model_dump(mode='json')
    data['stages'].pop('planner', None)
    assert AppConfig.model_validate(data).exploration.max_rounds == 6

def test_optional_planner_config_validates_profile():
    data = load_config('config.example.yaml').model_dump(mode='json')
    data['stages']['planner'] = {'model': 'investigator', 'max_output_tokens': 1024}
    assert AppConfig.model_validate(data).stages['planner'].max_output_tokens == 1024
    data['stages']['planner']['model'] = 'missing'
    with pytest.raises(ValueError, match='missing'):
        AppConfig.model_validate(data)
