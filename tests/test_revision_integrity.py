"""Replay unmodified 2026-10-26 model outputs; no new model or network calls."""

import json
from pathlib import Path
import socket

from pydantic_ai import Agent
import pytest

from bounded_investigator.evidence.snippets import build_code_index_from_text
from bounded_investigator.investigation.revision import merge_scoped_revision, render_revision_scope
from bounded_investigator.schemas import CoverageReview, InvestigationResult


FIXTURES = Path(__file__).parent / 'fixtures/revision_20261026'


@pytest.fixture(autouse=True)
def forbid_model_and_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('Revision replay cannot call a model or the network')
    monkeypatch.setattr(Agent, 'run', forbidden)
    monkeypatch.setattr(socket, 'create_connection', forbidden)
    monkeypatch.setattr(socket.socket, 'connect', forbidden)


def case(job):
    raw = json.loads((FIXTURES / f'{job}.json').read_text(encoding='utf-8'))
    return (raw, InvestigationResult.model_validate(raw['analyzer_result']),
            CoverageReview.model_validate(raw['review']),
            InvestigationResult.model_validate(raw['candidate_result']))


@pytest.mark.parametrize('job, original_count, actual_count, expected_count', [
    ('J07', 3, 4, 5), ('J11', 2, 2, 3),
])
def test_actual_failed_candidates_still_fail_closed(job, original_count, actual_count, expected_count):
    raw, draft, review, candidate = case(job)
    assert raw['original_scope_audit'] == {
        'passed': False, 'error': 'candidate list length disagrees with declared edits: claims',
    }
    assert len(draft.claims) == original_count and len(candidate.claims) == actual_count
    index = build_code_index_from_text(raw['packet_text'], f'{job}.md')
    with pytest.raises(ValueError, match='candidate list length disagrees with declared edits: claims'):
        merge_scoped_revision(draft, candidate, review, index=index)
    assert f'claims：原有 {original_count}；删除 0；新增 {expected_count-original_count}；最终必须为 {expected_count}。' in render_revision_scope(draft, review)


@pytest.mark.parametrize('job', ['J07', 'J11'])
def test_restoring_original_items_and_authorized_positions_keeps_factual_corrections(job):
    raw, draft, review, candidate = case(job)
    before = candidate.model_dump(mode='json')
    restored = candidate.model_copy(deep=True)
    if job == 'J07':
        restored.claims = [draft.claims[0].model_copy(deep=True), candidate.claims[1].model_copy(deep=True),
            draft.claims[2].model_copy(deep=True), candidate.claims[2].model_copy(deep=True),
            candidate.claims[3].model_copy(deep=True)]
    else:
        restored.claims = [draft.claims[0].model_copy(deep=True), candidate.claims[0].model_copy(deep=True),
                           candidate.claims[1].model_copy(deep=True)]
    index = build_code_index_from_text(raw['packet_text'], f'{job}.md')
    outcome = merge_scoped_revision(draft, restored, review, index=index)
    assert outcome.audit['passed']
    assert candidate.model_dump(mode='json') == before
    assert outcome.result.claims[0] == draft.claims[0]
    if job == 'J07':
        assert len(outcome.result.claims) == 5
        assert outcome.result.claims[2] == draft.claims[2]  # Retain the independent SID fact.
        assert outcome.result.claims[1].condition == candidate.claims[1].condition
        assert '不以 DialParamsUID' in outcome.result.claims[1].condition
        assert outcome.result.claims[3:] == candidate.claims[2:]
        assert outcome.result.artifacts[0].detection_specificity == candidate.artifacts[0].detection_specificity
        assert outcome.result.artifacts[0].detection_specificity.value in {'low', 'none'}
    else:
        assert len(outcome.result.claims) == 3
        assert outcome.result.claims[1].condition == candidate.claims[0].condition
        assert '未显示 NSS_Init 或 GetUserProfileDirectoryA 被调用' in outcome.result.claims[1].condition
        assert outcome.result.claims[2] == candidate.claims[1]
        assert any(issue.severity == 'minor' and any(fix.type == 'append_item' for fix in issue.fixes)
                   for issue in review.issues)


def test_fixtures_keep_original_provenance_and_validated_reviewer_plans():
    for job in ('J07', 'J11'):
        raw, _, review, _ = case(job)
        assert 'unmodified model results' in raw['provenance']['origin']
        assert review.requires_revision
        assert set(raw['provenance']['original_records']) == {'analyzer', 'reviewer', 'reviser', 'scope', 'packet'}
        assert all(len(record['sha256']) == 64 for record in raw['provenance']['original_records'].values())
