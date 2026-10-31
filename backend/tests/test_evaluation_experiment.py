"""有限实验不以隐藏答案制造改进，初始失败不可被覆盖。"""
import asyncio
from copy import deepcopy
import time

import pytest
from fastapi.testclient import TestClient
from tests.auth_support import ADMIN_HEADERS, READ_HEADERS


def result(cid, passed, calls=1):
    return dict(case_id=cid, check_status='pass' if passed else 'fail', task_execution_status='completed' if passed else 'paused',
        business_quality='passed' if passed else 'unknown', report_quality='unknown', report='资料说明' if passed else '',
        report_sha256='', evidence=[], trace=[], calls=calls, provider_calls=calls, input_tokens=None,
        output_tokens=None, duration_seconds=0.1, error=None if passed else '公开故障类别')


def test_bounded_arms_preserve_failure_and_public_feedback(tmp_path):
    from evaluation.experiment import run_case
    calls, updates = [], []
    async def execute(cid, group, directory, **kwargs):
        calls.append(deepcopy(kwargs))
        return result(cid, len(calls) > 1)
    async def update(data):
        updates.append(deepcopy(data))
    row = asyncio.run(run_case('G002', tmp_path, executor=execute, on_update=update))
    assert row['baseline']['check_status'] == 'fail' and len(row['arms']) == 2
    assert all(a['ever_passed'] and not a['always_passed'] for a in row['arms'])
    assert len(calls) == len(updates) == 3
    assert calls[0]['fault'] == 'both_unavailable'
    assert not calls[1]['feedback'] and calls[2]['feedback']
    assert all('oracle' not in str(c) and 'required_fact_ids' not in str(c) for c in calls)


def test_attempt_limit_unknown_and_over_budget(tmp_path):
    from evaluation.experiment import run_case
    async def failed(cid, group, directory, **kwargs):
        return result(cid, False)
    row = asyncio.run(run_case('G002', tmp_path, executor=failed, max_attempts=2, call_limit=1))
    assert len(row['arms'][0]['attempts']) == len(row['arms'][1]['attempts']) == 2
    assert row['check_status'] == 'fail'
    async def unknown(cid, group, directory, **kwargs):
        return result(cid, False) | dict(check_status='unknown', provider_calls=None)
    row = asyncio.run(run_case('G002', tmp_path / 'unknown', executor=unknown))
    assert row['check_status'] == 'unknown' and not row['arms']
    count = 0
    async def missing_after_baseline(cid, group, directory, **kwargs):
        nonlocal count
        count += 1
        return result(cid, False) if count == 1 else result(cid, False) | dict(check_status='unknown', provider_calls=None)
    row = asyncio.run(run_case('G002', tmp_path / 'partial-unknown', executor=missing_after_baseline))
    assert row['check_status'] == 'unknown' and all(a['provider_calls'] is None for a in row['arms'])
    async def over_budget(cid, group, directory, **kwargs):
        return result(cid, False, 11)
    with pytest.raises(ValueError):
        asyncio.run(run_case('G002', tmp_path / 'bad', executor=over_budget))


def test_development_only_and_request_limits():
    from evaluation.experiment import ExperimentRequest, validate_sources
    for ids in (['G017'], ['G006'], ['G002', 'G002']):
        with pytest.raises(ValueError):
            ExperimentRequest(case_ids=ids)
    for change in ({'max_attempts': 3}, {'call_limit': 0}, {'authorized': True}):
        with pytest.raises(ValueError):
            ExperimentRequest.model_validate(dict(case_ids=['G002']) | change)
    with pytest.raises(ValueError):
        validate_sources(['G006'])


def test_real_experiment_http_archive_and_progress():
    from main import app
    with TestClient(app, headers=ADMIN_HEADERS) as client:
        path = '/api/v2/evaluation-center/runs'
        body = dict(profile='bounded_experiment', case_ids=['G002'], max_attempts=1, call_limit=10)
        assert client.post(path, headers=READ_HEADERS, json=body).status_code == 403
        assert client.post(path, json=body | dict(case_ids=['G017'])).status_code == 422
        response = client.post(path, json=body)
        assert response.status_code == 202
        rid = response.json()['data']['run_id']
        end = time.monotonic() + 60
        while time.monotonic() < end:
            row = client.get(path + '/' + rid).json()['data']
            if row['execution_status'] != 'running':
                break
            time.sleep(.05)
        assert row['execution_status'] == 'completed'
        item = row['results'][0]
        assert item['baseline']['check_status'] == 'fail' and item['check_status'] == 'pass'
        assert item['baseline']['trace'][0]['status'] == 'failed'
        assert len(row['metadata']['experiment_progress']) == 3
        assert item['report_quality'] == 'unknown'


@pytest.mark.parametrize('mode', ['fault', 'budget', 'feedback'])
def test_worker_attempt_cap_and_ephemeral_feedback(tmp_path, mode):
    import os
    import json
    from unittest.mock import patch
    from evaluation.full_task_worker import execute
    with patch.dict(os.environ):
        os.environ.pop('SMARTBRIEF_EVALUATION_GRANT', None)
        os.environ.update(SMARTBRIEF_EVAL_FAULT='both_unavailable' if mode == 'fault' else 'none',
            SMARTBRIEF_LOCAL_MODEL_CALL_LIMIT='1' if mode == 'budget' else '10',
            SMARTBRIEF_EVAL_PUBLIC_FEEDBACK='仅公开工作流诊断，不含标准答案' if mode == 'feedback' else '')
        asyncio.run(execute(tmp_path, 'G002', 'primary'))
    row = json.loads((tmp_path / 'result.json').read_text('utf8'))
    assert row['provider_calls'] <= (1 if mode == 'budget' else 10)
    assert row['calls'] >= row['provider_calls']
    assert row['task_execution_status'] == 'completed' if mode == 'feedback' else row['task_execution_status'] != 'completed'


def test_initial_fault_not_triggered_is_unknown(tmp_path):
    from evaluation.experiment import run_case
    async def supplied(cid, group, directory, **kwargs):
        return result(cid, True)
    row = asyncio.run(run_case('G002', tmp_path, executor=supplied))
    assert row['check_status'] == 'unknown' and row['error'] and not row['arms']


def test_runtime_split_change_denied_and_shutdown_keeps_progress(monkeypatch):
    from main import app
    from evaluation import catalog
    from services import evaluation_run_service
    from db import evaluation_run_repository
    body = dict(profile='bounded_experiment', case_ids=['G002'])
    with TestClient(app, headers=ADMIN_HEADERS) as client:
        with monkeypatch.context() as ctx:
            ctx.setattr(catalog, 'detail', lambda cid: dict(case=dict(split='holdout')))
            assert client.post('/api/v2/evaluation-center/runs', json=body).status_code == 422
        async def blocked(cid, directory, *, on_update, **kwargs):
            await on_update(dict(case_id=cid, phase='baseline', attempt=0, result=result(cid, False)))
            await asyncio.Event().wait()
        monkeypatch.setattr(evaluation_run_service, 'experiment_case', blocked)
        rid = client.post('/api/v2/evaluation-center/runs', json=body).json()['data']['run_id']
        for _ in range(200):
            row = client.get('/api/v2/evaluation-center/runs/' + rid).json()['data']
            if row['metadata']['experiment_progress']:
                break
            time.sleep(.01)
        assert row['metadata']['experiment_progress'][0]['result']['check_status'] == 'fail'
    saved = evaluation_run_repository.get(rid)
    assert saved['execution_status'] == 'failed' and len(saved['metadata']['experiment_progress']) == 1
