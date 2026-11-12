"""审核返回异常必须可追溯；单条坏 finding 不吞掉整轮结果。"""
import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest
from agents import reviewer
from tests.test_evidence import source
from tests.test_rewriter import FakeResp


def review(raw):
    response = FakeResp(raw)
    response.choices[0].finish_reason = 'stop'
    response.id = 'response-test'
    response.model = 'test-model'
    with patch.object(reviewer, 'chat', new=AsyncMock(return_value=response)):
        return asyncio.run(reviewer.reviewer_node({
            'task_id': 't', 'draft_content': '市场规模99亿元。[S1]',
            'evidence': [source(excerpt='市场规模10亿元。')]}))


@pytest.mark.parametrize('raw,code', [('', 'REVIEW_EMPTY_RESPONSE'), ('not json', 'REVIEW_JSON_INVALID'),
    ('{"passed":"false","comments":[]}', 'REVIEW_SCHEMA_INVALID')])
def test_invalid_response_records_precise_diagnostics(raw, code):
    out = review(raw)
    assert out['quality_status'] == 'unavailable'
    diagnostic = out['review_history'][-1]['review_diagnostics']
    assert diagnostic['code'] == code
    assert diagnostic['raw_response'] == raw
    assert diagnostic['finish_reason'] == 'stop'
    assert diagnostic['response_id'] == 'response-test'
    assert any(code in log['content'] for log in out['react_logs'])


def test_invalid_finding_is_isolated_but_cannot_pass():
    valid = {'finding_id': 'REV-1', 'problem': '口径缺失', 'expected': '补充期间',
             'location': {'paragraph': 1, 'sentence': 1}, 'severity': 'soft',
             'evidence': {'source_id': 'S1', 'quote': '市场规模10亿元。'}}
    raw = json.dumps({'passed': True, 'comments': [], 'findings': [valid, {'finding_id': 'BAD'}]})
    assert reviewer._parse_response(FakeResp(raw))['passed'] is False
    out = review(raw)
    assert out['quality_status'] == 'rejected'
    assert out['passed'] is False
    assert any(f['finding_id'] == 'REV-1' for f in out['findings'])
    assert out['review_history'][-1]['review_diagnostics']['invalid_findings'][0]['index'] == 1
    assert any('不完整' in c for c in out['review_comments'])


def test_transport_failure_has_distinct_diagnostic():
    with patch.object(reviewer, 'chat', new=AsyncMock(side_effect=RuntimeError('provider failed'))):
        out = asyncio.run(reviewer.reviewer_node({'draft_content': '正文', 'evidence': []}))
    assert out['quality_status'] == 'unavailable'
    assert out['review_history'][-1]['review_diagnostics']['code'] == 'REVIEW_CALL_FAILED'


def test_truncated_output_records_finish_reason_and_bounded_raw_response():
    raw = '{"passed": false,' + ' ' * 33000
    response = FakeResp(raw)
    response.choices[0].finish_reason = 'length'
    with pytest.raises(reviewer.ReviewResponseError) as caught:
        reviewer._parse_response(response)
    d = caught.value.diagnostic
    assert d['code'] == 'REVIEW_RESPONSE_TRUNCATED'
    assert d['raw_length'] == len(raw)
    assert len(d['raw_response']) == 32000
    assert d['raw_truncated'] is True


def test_invalid_top_level_cannot_be_recovered_by_dropping_findings():
    out = review('{"passed":"true","comments":[],"findings":[{}]}')
    assert out['quality_status'] == 'unavailable'
    assert out['review_history'][-1]['review_diagnostics']['code'] == 'REVIEW_SCHEMA_INVALID'


def test_diagnostic_is_persisted_in_existing_history_json():
    from db.database import init_db
    from db import task_repository as repo
    out = review('broken json')
    init_db()
    repo.create_task('review-diagnostic', '审核诊断')
    repo.update_task('review-diagnostic', review_history=out['review_history'])
    assert repo.get_task('review-diagnostic')['review_history'] == out['review_history']


def test_malformed_soft_finding_is_dropped_without_voiding_round():
    hard = {'finding_id': 'MODEL-0001', 'problem': '缺少结论章节', 'expected': '补充结论与建议',
            'location': {'paragraph': 1, 'sentence': 1}, 'severity': 'hard',
            'evidence': {'kind': 'report', 'quote': '市场规模99亿元。[S1]'}}
    bad_soft = {'finding_id': 'MODEL-0002', 'problem': '口径说明不足', 'expected': '',
                'location': {'paragraph': 1, 'sentence': 1}, 'severity': 'soft',
                'evidence': {'source_id': 'S1', 'quote': '市场规模10亿元。'}}
    raw = json.dumps({'passed': False, 'comments': ['结构缺失'], 'findings': [hard, bad_soft]})
    parsed = reviewer._parse_response(FakeResp(raw))
    assert parsed['passed'] is False
    assert [f['finding_id'] for f in parsed['findings']] == ['MODEL-0001']
    assert parsed['dropped_findings'][0]['index'] == 1
    assert 'review_diagnostics' not in parsed
    assert any('soft' in c for c in parsed['comments'])


def test_dropping_soft_does_not_turn_a_baseless_rejection_into_a_pass():
    bad_soft = {'finding_id': 'MODEL-0002', 'problem': '口径说明不足', 'expected': '',
                'location': {'paragraph': 1, 'sentence': 1}, 'severity': 'soft',
                'evidence': {'source_id': 'S1', 'quote': '市场规模10亿元。'}}
    raw = json.dumps({'passed': False, 'comments': ['只有建议'], 'findings': [bad_soft]})
    with pytest.raises(reviewer.ReviewResponseError) as caught:
        reviewer._parse_response(FakeResp(raw))
    assert caught.value.diagnostic['code'] == 'REVIEW_SCHEMA_INVALID'
