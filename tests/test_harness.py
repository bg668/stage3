import importlib.util
import json
import shutil
from pathlib import Path

import pytest

from bounded_investigator.prompts import analyzer_instructions, reviewer_instructions, reviser_instructions

LEGACY = json.loads((Path(__file__).parent / 'fixtures/legacy_role_instructions.json').read_text(encoding='utf-8'))
CURRENT = json.loads((Path(__file__).parent / 'fixtures/role_instructions_20261027.json').read_text(encoding='utf-8'))
BEFORE = json.loads((Path(__file__).parent / 'fixtures/harness_before_20261027.json').read_text(encoding='utf-8'))


def test_harness_module_available():
    assert importlib.util.find_spec('bounded_investigator.harness') is not None


def copy_base(tmp_path):
    from bounded_investigator.harness import builtin_harness_dir
    target = tmp_path / 'candidate'
    shutil.copytree(builtin_harness_dir(), target)
    return target


def test_builtin_matches_current_role_instructions():
    from bounded_investigator.harness import load_harness
    harness = load_harness()
    for role, expected in CURRENT.items():
        assert harness.instructions(role) == expected


def historical_base(tmp_path, monkeypatch):
    from bounded_investigator.harness import loader
    root = tmp_path / 'historical'
    for name, text in BEFORE['files'].items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode('utf-8'))
    trusted = tmp_path / 'trusted'
    (trusted / 'prompts').mkdir(parents=True)
    (trusted / 'prompts/investigation_result.rules.md').write_text(BEFORE['rules'], encoding='utf-8')
    monkeypatch.setattr(loader, 'files', lambda package: trusted)
    return root


def test_legacy_role_fixture_is_still_reproducible(tmp_path, monkeypatch):
    from bounded_investigator.harness import load_harness
    root = historical_base(tmp_path, monkeypatch)
    old_role = Path(__file__).parent / 'fixtures/revision_20261026/reviser_role_before.md'
    (root / 'agents/reviser.md').write_bytes(old_role.read_bytes())
    historical = load_harness(root)
    for role, expected in LEGACY.items():
        assert historical.instructions(role) == expected
    assert historical.version == 'baseline-v1'


def test_new_instructions_cannot_reuse_historical_output_snapshot(tmp_path, monkeypatch):
    from bounded_investigator.harness import load_harness
    updated = load_harness()
    root = historical_base(tmp_path, monkeypatch)
    historical = load_harness(root)
    assert {role: historical.instructions(role) for role in CURRENT} == BEFORE['instructions']
    assert historical.fingerprint == BEFORE['fingerprint']
    historical.save(tmp_path / 'old_run')
    assert historical.fingerprint != updated.fingerprint
    with pytest.raises(ValueError, match='different harness'):
        updated.save(tmp_path / 'old_run')


def test_builtin_skill_is_expanded_in_place_and_recorded(tmp_path):
    from bounded_investigator.harness import load_harness
    root = copy_base(tmp_path)
    skill_path = 'skills/packet-fact-boundary/SKILL.md'
    raw = (root / skill_path).read_text(encoding='utf-8')
    body = raw.split('---', 2)[2].strip()
    assert len(body.splitlines()) == 2
    assert body.startswith('最重要的职责边界：')
    role_path = root / 'agents/reviewer.md'
    role_text = role_path.read_text(encoding='utf-8')
    assert '{{skill:packet-fact-boundary}}' in role_text
    assert body not in role_text

    harness = load_harness(root)
    for role, expected in CURRENT.items():
        assert harness.instructions(role) == expected
    assert harness.instructions('reviewer').count(body) == 1
    assert all(body not in harness.instructions(role) for role in ('analyzer', 'reviser'))
    harness.save(tmp_path / 'saved')
    saved = json.loads((tmp_path / 'saved/harness_snapshot.json').read_text(encoding='utf-8'))
    assert saved['files'][skill_path] == raw
    assert saved['instructions']['reviewer'] == CURRENT['reviewer']
    assert '{{skill:' not in saved['instructions']['reviewer']

    # Reconstruct the old layout: instructions match, but the source snapshot differs.
    role_path.write_text(role_text.replace('{{skill:packet-fact-boundary}}', body), encoding='utf-8')
    (root / skill_path).unlink()
    old_layout = load_harness(root)
    assert old_layout.version == harness.version == 'baseline-v1'
    assert old_layout.fingerprint != harness.fingerprint
    assert old_layout.instructions('reviewer') == harness.instructions('reviewer')
    with pytest.raises(ValueError, match='different harness'):
        old_layout.save(tmp_path / 'saved')


def test_changing_skill_changes_only_assigned_role(tmp_path):
    from bounded_investigator.harness import load_harness
    root = copy_base(tmp_path)
    path = root / 'skills/packet-fact-boundary/SKILL.md'
    text = path.read_text(encoding='utf-8')
    path.write_text(text.replace('最重要的职责边界：', '测试中的职责边界：'), encoding='utf-8')
    harness = load_harness(root)
    assert '测试中的职责边界：' in harness.instructions('reviewer')
    for role in ('analyzer', 'reviser'):
        assert harness.instructions(role) == CURRENT[role]


def test_explicit_skill_keeps_position_with_sorted_unreferenced_skills(tmp_path):
    from bounded_investigator.harness import load_harness
    root = copy_base(tmp_path)
    for name in ('z-last', 'm-inline', 'a-first'):
        path = root / 'skills' / name / 'SKILL.md'
        path.parent.mkdir(parents=True)
        path.write_text(f'---\nname: {name}\ndescription: test\nroles: [analyzer]\n---\n{name}', encoding='utf-8')
    (root / 'agents/analyzer.md').write_text(
        '---\nname: analyzer\n---\nbefore\n\n{{skill:m-inline}}\n\nafter', encoding='utf-8')
    harness = load_harness(root)
    rules = CURRENT['analyzer'][CURRENT['analyzer'].index('InvestigationResult 公共质量规则：'):]
    control = (root / 'skills/control-flow-conditions/SKILL.md').read_text(encoding='utf-8').split('---', 2)[2].strip()
    assert harness.instructions('analyzer') == f'before\n\nm-inline\n\nafter\n\na-first\n\n{control}\n\nz-last\n\n{rules}'
    assert harness.instructions('reviewer') == CURRENT['reviewer']
    assert harness.instructions('reviser') == CURRENT['reviser']


@pytest.mark.parametrize(('case', 'message'), [
    ('missing', 'Unknown skill'),
    ('cross-role', 'not assigned'),
    ('duplicate', 'Duplicate skill'),
    ('nested', 'Nested skill'),
    ('inline', 'standalone line'),
    ('shared', 'only supported in role files'),
])
def test_invalid_skill_references_rejected_at_load(tmp_path, case, message):
    from bounded_investigator.harness import load_harness
    root = copy_base(tmp_path)
    path = root / 'skills/x/SKILL.md'
    path.parent.mkdir(parents=True)
    roles = 'reviewer' if case == 'cross-role' else 'analyzer'
    body = '{{skill:other}}' if case == 'nested' else 'skill body'
    path.write_text(f'---\nname: x\ndescription: test\nroles: [{roles}]\n---\n{body}', encoding='utf-8')
    reference = '{{skill:missing}}' if case == 'missing' else '{{skill:x}}'
    if case == 'duplicate':
        reference += '\n{{skill:x}}'
    elif case == 'inline':
        reference = 'prefix ' + reference
    (root / 'agents/analyzer.md').write_text(f'---\nname: analyzer\n---\n{reference}', encoding='utf-8')
    if case == 'shared':
        (root / 'systemprompt.md').write_text(reference, encoding='utf-8')
    with pytest.raises(ValueError, match=message):
        load_harness(root)


def test_skills_are_sorted_role_scoped_and_snapshot_is_immutable(tmp_path):
    from bounded_investigator.harness import load_harness
    root = copy_base(tmp_path)
    for name in ('z-last', 'a-first'):
        path = root / 'skills' / name / 'SKILL.md'
        path.parent.mkdir(parents=True)
        path.write_text(f'---\nname: {name}\ndescription: test knowledge\nroles: [analyzer]\n---\n{name}', encoding='utf-8')
    harness = load_harness(root)
    assert harness.instructions('analyzer').index('a-first') < harness.instructions('analyzer').index('z-last')
    assert 'a-first' not in harness.instructions('reviewer')
    fingerprint = harness.fingerprint
    (root / 'agents/analyzer.md').write_text('---\nname: analyzer\n---\nchanged', encoding='utf-8')
    assert harness.fingerprint == fingerprint
    assert 'changed' not in harness.instructions('analyzer')
    assert load_harness(root).fingerprint != fingerprint
    harness.save(tmp_path / 'saved')
    saved = json.loads((tmp_path / 'saved/harness_snapshot.json').read_text(encoding='utf-8'))
    assert saved['fingerprint'] == fingerprint
    assert saved['instructions']['analyzer'] == harness.instructions('analyzer')


@pytest.mark.parametrize('mutation', ['unsupported', 'trusted-override', 'bad-role', 'missing-agent', 'oversize', 'bad-version'])
def test_invalid_harness_rejected_before_models(tmp_path, mutation):
    from bounded_investigator.harness import load_harness
    root = copy_base(tmp_path)
    if mutation == 'unsupported':
        (root / 'plugin.py').write_text('print(1)', encoding='utf-8')
    elif mutation == 'trusted-override':
        p = root / 'prompts/investigation_result.rules.md'
        p.parent.mkdir()
        p.write_text('replace trusted rules', encoding='utf-8')
    elif mutation == 'bad-role':
        p = root / 'skills/x/SKILL.md'; p.parent.mkdir(parents=True)
        p.write_text('---\nname: x\ndescription: x\nroles: [shell]\n---\nx', encoding='utf-8')
    elif mutation == 'missing-agent':
        (root / 'agents/reviewer.md').unlink()
    elif mutation == 'oversize':
        (root / 'systemprompt.md').write_text('x' * 200_000, encoding='utf-8')
    else:
        (root / 'harness.yaml').write_text('format_version: 99\nversion: bad\n', encoding='utf-8')
    with pytest.raises(ValueError):
        load_harness(root)


def test_workflow_uses_explicit_snapshot_and_keeps_schema(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from bounded_investigator import workflow
    from bounded_investigator.harness import load_harness
    root = copy_base(tmp_path)
    (root / 'systemprompt.md').write_text('shared custom strategy', encoding='utf-8')
    captured = {}
    def build(**kwargs):
        captured[kwargs['stage']] = kwargs
        return object()
    monkeypatch.setattr(workflow, 'build_stage_agent', build)
    config = SimpleNamespace(format_repair=SimpleNamespace(enabled=False), global_reconciliation=SimpleNamespace(enabled=False))
    snapshot = load_harness(root)
    runner = workflow.BoundedInvestigationWorkflow(config, harness=snapshot)
    assert runner.harness is snapshot
    assert all('shared custom strategy' in stage['instructions'] for stage in captured.values())
    for role in ('analyzer', 'reviewer', 'reviser'):
        assert captured[role]['instructions'] == snapshot.instructions(role)
    assert '{{skill:' not in captured['reviewer']['instructions']
    assert 'InvestigationResult' == captured['analyzer']['schema'].__name__
    assert 'CoverageReview' == captured['reviewer']['schema'].__name__


def test_config_harness_path_relative_to_config_file(tmp_path):
    from bounded_investigator.config import load_config
    text = Path('config.example.yaml').read_text(encoding='utf-8')
    config = tmp_path / 'config.yaml'
    config.write_text(text + '\nharness_dir: candidate\n', encoding='utf-8')
    assert load_config(config).harness_dir == (tmp_path / 'candidate').resolve()


def test_batch_shares_snapshot_even_if_source_changes(monkeypatch, tmp_path):
    import asyncio
    import logging
    from types import SimpleNamespace
    from bounded_investigator import workflow
    from bounded_investigator.orchestration import batch
    from bounded_investigator.schemas import RunEnvelope, UsageSummary, InvestigationResult
    root = copy_base(tmp_path)
    seen = []
    class Worker:
        def __init__(self, config, *, harness):
            seen.append(harness)
        async def run(self, **kwargs):
            (root / 'systemprompt.md').write_text('mutated during batch', encoding='utf-8')
            return RunEnvelope(packet_name=kwargs['packet_name'], elapsed_seconds=0,
                               revision_performed=False, usage=UsageSummary(),
                               result=InvestigationResult(status='complete', summary='fixture'))
    monkeypatch.setattr(workflow, 'BoundedInvestigationWorkflow', Worker)
    packets = [tmp_path / f'J{i:02}.md' for i in range(2)]
    for p in packets:
        p.write_text('fixture', encoding='utf-8')
    completed, failures = asyncio.run(batch.run_batch_independent(
        packets=packets, output_dir=tmp_path / 'out',
        config=SimpleNamespace(batch=SimpleNamespace(concurrency=1), harness_dir=root),
        log=logging.getLogger('test.harness')))
    assert not failures and len(completed) == 2
    assert seen[0] is seen[1]
    assert 'mutated during batch' not in seen[1].instructions('analyzer')


def test_snapshot_rejects_mixed_output_directory(tmp_path):
    from bounded_investigator.harness import load_harness
    root = copy_base(tmp_path)
    load_harness(root).save(tmp_path / 'out')
    (root / 'systemprompt.md').write_text('different policy', encoding='utf-8')
    with pytest.raises(ValueError, match='different harness'):
        load_harness(root).save(tmp_path / 'out')


def test_harness_symlink_is_rejected(tmp_path):
    from bounded_investigator.harness import load_harness
    root = copy_base(tmp_path)
    linked = root / 'skills' / 'linked'
    linked.parent.mkdir(exist_ok=True)
    try:
        linked.symlink_to(tmp_path, target_is_directory=True)
    except OSError:
        pytest.skip('OS does not grant symlink creation')
    with pytest.raises(ValueError, match='links'):
        load_harness(root)


def test_snapshot_refuses_tampered_record(tmp_path):
    from bounded_investigator.harness import load_harness
    harness = load_harness()
    harness.save(tmp_path)
    path = tmp_path / 'harness_snapshot.json'
    record = json.loads(path.read_text(encoding='utf-8'))
    record['instructions']['analyzer'] = 'forged'
    path.write_text(json.dumps(record), encoding='utf-8')
    with pytest.raises(ValueError):
        harness.save(tmp_path)


@pytest.mark.parametrize('command', ['run', 'batch'])
def test_cli_forwards_explicit_harness(monkeypatch, tmp_path, command):
    from typer.testing import CliRunner
    from bounded_investigator import cli
    root = copy_base(tmp_path)
    config = tmp_path / 'config.yaml'
    config.write_text(Path('config.example.yaml').read_text(encoding='utf-8'), encoding='utf-8')
    packet = tmp_path / 'J00.md'
    packet.write_text('fixture', encoding='utf-8')
    seen = []
    async def execute(*args, **kwargs):
        seen.append(args[2].harness_dir)
        if command == 'batch':
            assert kwargs == {'generate_html': True, 'context_file': None}
    monkeypatch.setattr(cli, 'run_one' if command == 'run' else 'run_batch', execute)
    monkeypatch.setattr(cli, 'configure_logging', lambda *_: None)
    result = CliRunner().invoke(cli.app, [command, str(packet if command == 'run' else tmp_path),
                                          '--config', str(config), '--harness-dir', str(root)])
    assert result.exit_code == 0, result.output
    assert seen == [root]
