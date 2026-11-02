import hashlib
import json
from pathlib import Path

import pytest

from bounded_investigator.upstream import prepare_packets
from bounded_investigator.orchestration.dag import load_manifest
from bounded_investigator.evidence.inventory import build_input_inventory


ROOT = Path(__file__).resolve().parents[1]
SAVED = ROOT / 'reference/examples/out/debug_03'


def fixture_inputs(tmp_path, *, edges=(), codes=None):
    codes = codes or {'F-0001': 'void one() { two(); }\n', 'F-0002': 'void two() { one(); }\n', 'F-0003': 'void support() {}\n'}
    source = tmp_path / 'source'
    (source / 'code').mkdir(parents=True)
    functions = []
    for ref, code in codes.items():
        (source / 'code' / f'{ref}.txt').write_text(code, encoding='utf-8')
        functions.append(dict(ref=ref, name=f'sub_{ref[2:]}', address=f'0x{ref[2:]}', decision='UNDECIDED' if ref == 'F-0002' else 'PROGRAM_ANALYZE' if ref == 'F-0001' else 'PROGRAM_SKIP', reason='rule', code_chars=len(code)))
    pre = {'sample': {'name': 'synthetic', 'sqlite_sha256': 'a' * 64, 'sample_sha256': 'b' * 64}, 'config': {'max_functions_per_unit': 1, 'max_chars_per_unit': 30}, 'functions': functions, 'call_edges': list(edges)}
    pp = source / 'preprocess.json'
    pp.write_text(json.dumps(pre), encoding='utf-8')
    tri = {'sample': pre['sample'], 'preprocess_ref': {'sha256': hashlib.sha256(pp.read_bytes()).hexdigest()}, 'final_verdicts': [{'ref': 'F-0002', 'final_decision': 'ANALYZE', 'reason': 'other', 'source_round': 1}]}
    tp = source / 'triage.json'
    tp.write_text(json.dumps(tri), encoding='utf-8')
    return pp, tp


def test_scc_preserves_full_code_mapping_and_support(tmp_path):
    pp, tp = fixture_inputs(tmp_path, edges=[['F-0001', 'F-0002'], ['F-0002', 'F-0001'], ['F-0001', 'F-0003']])
    result = prepare_packets(pp, tp, tmp_path / 'out')
    assert result['selected_count'] == 2
    manifest = load_manifest(tmp_path / 'out/manifest.json')
    assert len(manifest.jobs) == 1
    mapping = json.loads((tmp_path / 'out/material_mapping.json').read_text(encoding='utf-8'))
    job = mapping['packets'][0]
    assert job['primary_refs'] == ['F-0001', 'F-0002']
    assert job['support_refs'] == ['F-0003']
    assert job['oversized_component']
    inventory = build_input_inventory(list((tmp_path / 'out/packets').glob('J*.md')))
    assert len(inventory.code_sections) == 3
    text = next((tmp_path / 'out/packets').glob('J*.md')).read_text(encoding='utf-8')
    assert 'L0001: void one() { two(); }' in text
    assert mapping['functions']['F-0001']['code_sha256'] == hashlib.sha256((pp.parent / 'code/F-0001.txt').read_bytes()).hexdigest()


@pytest.mark.parametrize('mutation, match', [('duplicate', 'duplicate'), ('mismatch', 'sample'), ('missing_verdict', 'complete'), ('missing_code', 'code'), ('wrong_provenance', 'fingerprint')])
def test_rejects_invalid_sources(tmp_path, mutation, match):
    pp, tp = fixture_inputs(tmp_path)
    tri = json.loads(tp.read_text())
    if mutation == 'duplicate':
        tri['final_verdicts'] *= 2
    elif mutation == 'mismatch':
        tri['sample']['sqlite_sha256'] = 'c' * 64
    elif mutation == 'missing_verdict':
        tri['final_verdicts'] = []
    elif mutation == 'missing_code':
        (pp.parent / 'code/F-0001.txt').unlink()
    else:
        tri['preprocess_ref']['sha256'] = 'c' * 64
    tp.write_text(json.dumps(tri), encoding='utf-8')
    with pytest.raises((ValueError, FileNotFoundError), match=match):
        prepare_packets(pp, tp, tmp_path / 'out')
    assert not (tmp_path / 'out').exists()


def test_callee_first_stable_and_existing_output_refused(tmp_path):
    pp, tp = fixture_inputs(tmp_path, edges=[['F-0001', 'F-0002']])
    prepare_packets(pp, tp, tmp_path / 'out')
    mapping = json.loads((tmp_path / 'out/material_mapping.json').read_text(encoding='utf-8'))
    assert mapping['packets'][0]['primary_refs'] == ['F-0002']
    assert mapping['packets'][1]['depends_on'] == ['J00']
    with pytest.raises(FileExistsError):
        prepare_packets(pp, tp, tmp_path / 'out')


def test_saved_sample3_has_exactly_100_primary_functions(tmp_path):
    if not SAVED.exists():
        pytest.skip('user supplied saved sample is not distributed')
    result = prepare_packets(SAVED / 'preprocess/preprocess.json', SAVED / 'triage/triage.json', tmp_path / 'out')
    assert result['selected_count'] == 100
    mapping = json.loads((tmp_path / 'out/material_mapping.json').read_text(encoding='utf-8'))
    refs = [ref for p in mapping['packets'] for ref in p['primary_refs']]
    assert len(refs) == len(set(refs)) == 100
    assert sum(p['primary_code_chars'] for p in mapping['packets']) == 144816
    assert any(p['oversized_component'] and 'F-0483' in p['primary_refs'] for p in mapping['packets'])
    assert len(load_manifest(tmp_path / 'out/manifest.json').jobs) == len(mapping['packets'])


def test_upstream_fixed_response_chain_and_prompt_resources(tmp_path):
    from importlib.resources import files
    from bounded_investigator.upstream import run_preprocess_pipeline
    from bounded_investigator.upstream.triage.application import TriageOptions, run_triage
    from bounded_investigator.upstream.triage.providers.scripted import ScriptedProvider
    from bounded_investigator.upstream.triage.domain import TriageConfig

    sqlite = ROOT / 'reference/examples/input/test_sample3.db'
    if not sqlite.exists():
        pytest.skip('user supplied SQLite is not distributed')
    pre_run = run_preprocess_pipeline(sqlite, tmp_path / 'preprocess')
    assert pre_run.exit_code == 0
    scripts = {unit['unit_id']: [dict(ref=ref, decision='SKIP', reason='S_RUNTIME_HELPER') for ref in unit['subject_refs']] for unit in pre_run.payload['units']}
    tri_run = run_triage(TriageOptions(preprocess=tmp_path / 'preprocess/preprocess.json', out=tmp_path / 'triage', config=TriageConfig()), ScriptedProvider(scripts))
    assert tri_run.exit_code == 0
    result = prepare_packets(tmp_path / 'preprocess/preprocess.json', tmp_path / 'triage/triage.json', tmp_path / 'packets')
    assert result['selected_count'] == 43
    resource = files('bounded_investigator.upstream.triage').joinpath('prompts/round1.txt')
    assert 'verdicts' in resource.read_text(encoding='utf-8').lower()


def test_support_budget_omits_whole_functions_and_counts_duplicates(tmp_path):
    codes = {'F-0001': 'a();', 'F-0002': 'b();', 'F-0003': 'c' * 12001, 'F-0004': 'helper();'}
    pp, tp = fixture_inputs(tmp_path, codes=codes, edges=[['F-0001', 'F-0003'], ['F-0001', 'F-0004'], ['F-0002', 'F-0004']])
    result = prepare_packets(pp, tp, tmp_path / 'out')
    assert result['support_unique_count'] == 1
    assert result['support_occurrence_count'] == 2
    assert result['support_repeated_chars'] == len('helper();')
    assert result['packets'][0]['material_boundaries'][0]['ref'] == 'F-0003'
    assert all('F-0003' not in p['support_refs'] for p in result['packets'])


def test_original_backticks_remain_indexable(tmp_path):
    from bounded_investigator.evidence.snippets import build_code_index_from_text
    pp, tp = fixture_inputs(tmp_path, codes={'F-0001': '// ``` original comment', 'F-0002': 'return; '})
    result = prepare_packets(pp, tp, tmp_path / 'out')
    job = next(p['job_id'] for p in result['packets'] if 'F-0001' in p['primary_refs'])
    text = (tmp_path / 'out/packets' / f'{job}.md').read_text(encoding='utf-8')
    assert 'L0001: // ``` original comment' in text
    assert 'F-0001' in build_code_index_from_text(text, f'{job}.md')
