"""可比性不允许用未知元数据或筛选后的成功样本冒充增益。"""
from copy import deepcopy

from fastapi.testclient import TestClient
import pytest
from tests.auth_support import ADMIN_HEADERS, READ_HEADERS


def records():
    base = dict(run_id='er_' + 'a'*32, profile='full_task_local', execution_status='completed',
        case_ids=['G017', 'G018'], summary=dict(planned=2, processed=2),
        metadata=dict(dataset_hash='f'*64, source_hash='a'*64, model='fixture', prompt_version='p1', engine='isolated_full_workflow',
                      model_group='primary', feature_flags=dict(rag=False, memory=False, review=True),
                      execution_budget=dict(deadline_seconds=180), memory_protocol='isolated', randomness='fixed'),
        results=[dict(case_id='G017', check_status='pass', report_quality='unknown', duration_seconds=1.0),
                 dict(case_id='G018', check_status='fail', report_quality='unknown', duration_seconds=9.0)])
    current = deepcopy(base)
    current['run_id'] = 'er_'+'b'*32
    current['metadata']['source_hash'] = 'b'*64
    return base, current


def request(**kwargs):
    from evaluation.comparison import ComparisonRequest
    return ComparisonRequest(baseline_id='er_'+'a'*32, current_id='er_'+'b'*32, changed_dimension='code', **kwargs)


def test_declared_change_regression_and_unknown_quality():
    from evaluation.comparison import compare
    base, current = records()
    current['results'][0]['check_status'] = 'unknown'
    result = compare(request(), base, current)
    assert result['attribution_allowed'] and result['execution_pass_rate_delta'] == -.5
    assert result['quality_pass_rate_delta'] is None
    assert result['case_changes'][0]['transition'] == 'regression'
    assert result['all_attempt_seconds_delta'] == 0


@pytest.mark.parametrize('change', ['unknown', 'budget', 'model', 'order', 'partial', 'live', 'none', 'duplicate', 'unknown_flags'])
def test_extra_changes_and_missing_evidence_refuse_attribution(change):
    from evaluation.comparison import compare
    base, current = records()
    if change == 'unknown':
        current['metadata'].pop('memory_protocol')
    elif change == 'budget':
        current['metadata']['execution_budget'] = dict(deadline_seconds=200)
    elif change == 'model':
        current['metadata']['model'] = 'other'
    elif change == 'order':
        current['case_ids'].reverse()
    elif change == 'partial':
        current['results'].pop()
    elif change == 'live':
        current['profile'] = 'full_task_live'
    elif change == 'none':
        current['metadata']['source_hash'] = base['metadata']['source_hash']
    elif change == 'duplicate':
        current['results'][1]['case_id'] = 'G017'
    else:
        current['metadata']['feature_flags'] = {}
    result = compare(request(), base, current)
    assert not result['attribution_allowed'] and result['reasons']
    assert result['execution_pass_rate_delta'] is None


def test_comparison_http_archive_and_old_409_rule(monkeypatch):
    from main import app
    from db import evaluation_run_repository
    base, current = records()
    with TestClient(app, headers=ADMIN_HEADERS) as client:
        evaluation_run_repository.save(base)
        evaluation_run_repository.save(current)
        body = request().model_dump()
        assert client.post('/api/v2/evaluation-center/comparisons', headers=READ_HEADERS, json=body).status_code == 403
        assert client.post('/api/v2/evaluation-center/comparisons', json=body | {'current_id': '../x'}).status_code == 422
        response = client.post('/api/v2/evaluation-center/comparisons', json=body)
        assert response.status_code == 201
        saved = client.get('/api/v2/evaluation-center/comparisons', headers=READ_HEADERS).json()['data']['items']
        assert saved[0]['comparison_id'] == response.json()['data']['comparison_id']
        assert evaluation_run_repository.get(base['run_id']) == base
        assert evaluation_run_repository.get(current['run_id']) == current


def test_same_run_rejected():
    from evaluation.comparison import ComparisonRequest
    with pytest.raises(ValueError):
        ComparisonRequest(baseline_id='er_'+'a'*32, current_id='er_'+'a'*32, changed_dimension='code')


def test_model_group_known_quality_and_all_attempt_durations():
    from evaluation.comparison import compare
    base, current = records()
    for row in (base, current):
        row['metadata'].update(grader_version='independent-v1', rubric_hash='r1')
        for result in row['results']:
            result['report_quality'] = 'rejected'
    current['metadata'].update(source_hash=base['metadata']['source_hash'], model_group='backup')
    current['results'][1].update(check_status='pass', report_quality='passed')
    body = request().model_copy(update={'changed_dimension': 'model_group'})
    result = compare(body, base, current)
    assert result['attribution_allowed'] and result['quality_pass_rate_delta'] == .5
    assert result['case_changes'][1]['transition'] == 'improvement'
    base['results'][1]['check_status'] = 'unknown'
    current['results'][1]['duration_seconds'] = None
    result = compare(body, base, current)
    assert result['case_changes'][1]['transition'] == 'unknown' and result['all_attempt_seconds_delta'] is None


def test_comparison_corruption_and_storage_are_errors(monkeypatch):
    from main import app
    from db import evaluation_run_repository, evaluation_comparison_repository
    base, current = records()
    with TestClient(app, headers=ADMIN_HEADERS) as client:
        assert client.post('/api/v2/evaluation-center/comparisons', json=request().model_dump()).status_code == 404
        evaluation_run_repository.save(base)
        evaluation_run_repository.save(current)
        with monkeypatch.context() as ctx:
            def fail(*args):
                raise OSError('私人路径')
            ctx.setattr(evaluation_comparison_repository.os, 'replace', fail)
            response = client.post('/api/v2/evaluation-center/comparisons', json=request().model_dump())
            assert response.status_code == 503 and '私人路径' not in response.text
            assert not list((evaluation_comparison_repository.root() / 'comparisons').glob('*.tmp'))
        with monkeypatch.context() as ctx:
            ctx.setattr(evaluation_comparison_repository, 'listing', lambda: [{}])
            assert client.get('/api/v2/evaluation-center/comparisons').status_code == 503
        current['metadata'] = ['损坏结构']
        evaluation_run_repository.save(current)
        assert client.post('/api/v2/evaluation-center/comparisons', json=request().model_dump()).status_code == 503
