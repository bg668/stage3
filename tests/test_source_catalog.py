import hashlib
import json
from pathlib import Path

import pytest

from bounded_investigator.upstream.catalog import load_catalog
from test_upstream_pipeline import fixture_inputs, SAVED


def rewrite_preprocess(pp, tp, mutate):
    pre = json.loads(pp.read_text(encoding='utf-8'))
    mutate(pre)
    pp.write_text(json.dumps(pre), encoding='utf-8')
    tri = json.loads(tp.read_text(encoding='utf-8'))
    tri['preprocess_ref']['sha256'] = hashlib.sha256(pp.read_bytes()).hexdigest()
    tp.write_text(json.dumps(tri), encoding='utf-8')


def test_catalog_exact_identity_entry_and_snapshot(tmp_path):
    pp, tp = fixture_inputs(tmp_path, edges=[['F-0001', 'F-0002'], ['F-0002', 'F-0001']])
    rewrite_preprocess(pp, tp, lambda pre: pre['functions'][0].update(is_entry=True, is_export=True))
    catalog = load_catalog(pp, tp)
    assert catalog.selected_refs == {'F-0001', 'F-0002'}
    assert catalog.functions['F-0001']['is_entry'] is True
    assert catalog.resolve('sub_0001') == 'F-0001'
    assert catalog.resolve('0x0001') == 'F-0001'
    assert catalog.resolve('F-0001') == 'F-0001'
    assert catalog.components('F-0001') == ['F-0001', 'F-0002']
    assert catalog.components('F-0003') == ['F-0003']
    before = catalog.code('F-0003')
    (pp.parent / 'code/F-0003.txt').write_text('changed', encoding='utf-8')
    assert catalog.code('F-0003') == before
    snapshot = catalog.snapshot()
    assert snapshot['selected_count'] == 2
    assert snapshot['functions']['F-0003']['available'] is True
    assert snapshot['functions']['F-0003']['code_sha256']
    json.dumps(snapshot)
    with pytest.raises(ValueError, match='unknown'):
        catalog.resolve('sub_0001 extra')
    snapshot['functions']['F-0001']['is_entry'] = False
    assert catalog.functions['F-0001']['is_entry'] is True


def test_duplicate_symbols_are_rejected_as_ambiguous_targets(tmp_path):
    pp, tp = fixture_inputs(tmp_path)
    rewrite_preprocess(pp, tp, lambda pre: pre['functions'][1].update(name='sub_0001'))
    catalog = load_catalog(pp, tp)
    with pytest.raises(ValueError, match='ambiguous'):
        catalog.resolve('sub_0001')
    assert catalog.resolve('F-0001') == 'F-0001'


def test_missing_support_is_unavailable_selected_missing_fails(tmp_path):
    pp, tp = fixture_inputs(tmp_path)
    (pp.parent / 'code/F-0003.txt').unlink()
    catalog = load_catalog(pp, tp)
    assert not catalog.functions['F-0003']['available']
    assert catalog.functions['F-0003']['unavailable_reason']
    with pytest.raises(ValueError, match='unavailable'):
        catalog.code('F-0003')
    (pp.parent / 'code/F-0003.txt').write_text('void support() {}\n', encoding='utf-8')
    with pytest.raises(ValueError, match='unavailable'):
        catalog.code('F-0003')
    (pp.parent / 'code/F-0001.txt').unlink()
    with pytest.raises(FileNotFoundError, match='code'):
        load_catalog(pp, tp)


@pytest.mark.parametrize('kind', ['duplicate_function', 'sample_mismatch', 'missing_verdict'])
def test_source_catalog_reuses_strict_stage_validation(tmp_path, kind):
    pp, tp = fixture_inputs(tmp_path)
    if kind == 'duplicate_function':
        rewrite_preprocess(pp, tp, lambda pre: pre['functions'].append(dict(pre['functions'][0])))
    else:
        tri = json.loads(tp.read_text(encoding='utf-8'))
        if kind == 'sample_mismatch':
            tri['sample']['sample_sha256'] = 'c' * 64
        else:
            tri['final_verdicts'] = []
        tp.write_text(json.dumps(tri), encoding='utf-8')
    with pytest.raises(ValueError):
        load_catalog(pp, tp)


def test_sample3_catalog_keeps_100_candidates_and_metadata_entry():
    if not SAVED.exists():
        pytest.skip('user supplied sample not distributed')
    catalog = load_catalog(SAVED / 'preprocess/preprocess.json', SAVED / 'triage/triage.json')
    assert len(catalog.selected_refs) == 100
    entries = [r for r, fn in catalog.functions.items() if fn.get('is_entry')]
    pre = json.loads((SAVED / 'preprocess/preprocess.json').read_text(encoding='utf-8'))
    assert entries == [fn['ref'] for fn in pre['functions'] if fn['is_entry']]
    assert catalog.components('F-0143') == ['F-0143', 'F-0144']


def test_unavailable_existing_support_keeps_original_file_fingerprint(tmp_path):
    pp, tp = fixture_inputs(tmp_path)
    path = pp.parent / 'code/F-0003.txt'
    path.write_text('incomplete source', encoding='utf-8')
    catalog = load_catalog(pp, tp)
    assert catalog.functions['F-0003']['available'] is False
    assert catalog.functions['F-0003']['code_sha256'] == hashlib.sha256(path.read_bytes()).hexdigest()
