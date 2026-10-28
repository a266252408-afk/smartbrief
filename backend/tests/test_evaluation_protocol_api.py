"""v2协议入口、角色、异常、开关效果与同源对照。"""
import asyncio
import time
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from db import database, evaluation_repository as repo
from tests.auth_support import ADMIN_HEADERS, READ_HEADERS

BODY = dict(suite_version='protocol-1',mode='offline',feature_flags=dict(rag=True,memory=True,review=True))


def test_v2_auth_validation_and_no_online_scores(tmp_path):
    from main import app
    with TestClient(app,headers=ADMIN_HEADERS) as client:
        for headers,status in [({},401),(READ_HEADERS,403)]:
            assert client.post('/api/v2/evaluations',headers={'Authorization':''}|headers,json=BODY).status_code == status
        assert client.post('/api/v2/evaluations',json=BODY|{'mode':'online'}).status_code == 403
        for body in (BODY|{'suite_version':'unknown'},BODY|{'feature_flags':{'rag':1,'memory':True,'review':True}},BODY|{'extra':'bad'},{}):
            assert client.post('/api/v2/evaluations',json=body).status_code == 422
        assert repo.list_reports()[1] == 0
        assert client.get('/api/v2/evaluations/missing').status_code == 404
        for path in ('/api/v2/evaluations?page=0','/api/v2/evaluations?page_size=101','/api/v2/evaluations?page=bad','/api/v2/evaluations/a/comparison'):
            invalid = client.get(path)
            assert invalid.status_code == 422 and 'error' in invalid.json()
        with patch.object(repo,'list_reports',side_effect=ValueError('SECRET_INTERNAL')):
            response = client.get('/api/v2/evaluations')
        assert response.status_code == 503 and 'SECRET_INTERNAL' not in response.text
        with patch.object(repo,'get',side_effect=RuntimeError('SECRET_INTERNAL')):
            response = client.get('/api/v2/evaluations/bad')
        assert response.status_code == 503 and 'SECRET_INTERNAL' not in response.text


def test_real_v2_suite_progress_and_reopen(tmp_path):
    from main import app
    with TestClient(app,headers=ADMIN_HEADERS) as client:
        response = client.post('/api/v2/evaluations',json=BODY)
        assert response.status_code == 202
        eval_id = response.json()['data']['eval_id']
        assert client.post('/api/v2/evaluations',json=BODY).status_code == 409
        deadline = time.monotonic()+40
        while time.monotonic() < deadline:
            record = client.get('/api/v2/evaluations/'+eval_id).json()['data']
            if record['status'] != 'running':
                break
            time.sleep(.03)
        assert record['status'] == 'completed' and len(record['results']) == 20
        assert record['metadata']['mode'] == 'offline'
        summary = record['metrics']['summary']
        assert summary['normal_total_cases'] == 16 and summary['fault_total_cases'] == 4
        assert summary['normal_quality_pass_rate'] == 0.9375
        assert summary['fault_handling_rate'] == 1.0
        assert record['metrics']['met']['normal_quality_pass_rate'] is True
        assert client.get('/api/v2/evaluations',headers=READ_HEADERS).json()['data']['total'] == 1
        assert client.get('/api/v2/evaluations/'+eval_id,headers=READ_HEADERS).status_code == 200
        assert client.get('/api/v1/evaluation/reports/'+eval_id).json()['eval_id'] == eval_id
        database.init_db()
        assert repo.get(eval_id)['results'] == record['results']


def test_flags_affect_real_nodes_without_global_changes(tmp_path):
    from evaluation.offline import OfflineDependencies,execute_case
    from evaluation.suite import load_suite
    from db import task_repository
    from memory import long_term_memory
    from evaluation.context import evaluation_context
    database.init_db(tmp_path/'flags.db')
    enabled_before = long_term_memory.is_enabled()
    async def scenario():
        dependencies = OfflineDependencies(load_suite()[0][0],tmp_path/'flags',dict(rag=False,memory=False,review=False))
        task_id = await execute_case(dependencies)
        result = task_repository.get_task(task_id)
        assert result['execution_status'] == 'completed' and result['quality_status'] == 'not_run'
        assert not result['passed'] and not dependencies.calls['reviewer']
        assert result['memory_hits'] == []
        logs = task_repository.get_logs(task_id)
        assert not any('已注入 RAG' in log['content'] for log in logs)
        assert evaluation_context.get() is None
    asyncio.run(scenario())
    assert long_term_memory.is_enabled() == enabled_before


def test_real_single_variable_comparison_and_rejections(tmp_path):
    from evaluation.archive import run_offline
    from services import evaluation_service as service
    database.init_db(tmp_path/'compare.db')
    baseline = asyncio.run(run_offline())
    current = asyncio.run(run_offline(dict(rag=True,memory=True,review=False)))
    result = service.compare(current['eval_id'],baseline['eval_id'])
    assert result['changed_feature'] == 'review' and result['quality_pass_rate_delta'] == -0.75
    assert current['metrics']['quality']['quality_pass_rate'] == 0
    assert baseline['results'][0]['memory_hits'] == 0 and baseline['results'][1]['memory_hits'] > 0
    from main import app
    with TestClient(app,headers=ADMIN_HEADERS) as client:
        path = '/api/v2/evaluations/'+current['eval_id']+'/comparison'
        assert client.get(path,params={'baseline':baseline['eval_id']}).json()['data']['changed_feature'] == 'review'
        assert client.get(path,params={'baseline':current['eval_id']}).status_code == 409
    with pytest.raises(service.EvaluationError) as exc:
        service.compare(baseline['eval_id'],baseline['eval_id'])
    assert exc.value.status == 409
    with pytest.raises(service.EvaluationError) as exc:
        service.compare('missing',baseline['eval_id'])
    assert exc.value.status == 404
    for key,value in [('source_hash','different'),('model','different'),('feature_flags',dict(rag=False,memory=False,review=False))]:
        changed = dict(current,metadata=current['metadata']|{key:value})
        with patch.object(service,'get',side_effect=[changed,baseline]):
            with pytest.raises(service.EvaluationError) as exc:
                service.compare('current','baseline')
        assert exc.value.status == 409


def test_storage_failure_cancelled_execution_and_validation(tmp_path):
    from services import evaluation_service as service
    database.init_db(tmp_path/'failure.db')
    body = service.EvaluationRequest.model_validate(BODY)
    with patch.object(repo,'create',side_effect=RuntimeError('SECRET_INTERNAL')):
        with pytest.raises(service.EvaluationError) as exc:
            asyncio.run(_start(body))
    assert 'SECRET_INTERNAL' not in str(exc.value)
    with patch.object(repo,'list_reports',side_effect=ValueError):
        with pytest.raises(service.EvaluationError):
            service.list_reports(1,20)
    repo.create('bad',20,metadata={})
    with database.get_connection() as conn:
        conn.execute("UPDATE evaluation_reports SET results='{}' WHERE eval_id='bad'")
    with pytest.raises(service.EvaluationError):
        service.get('bad')
    async def scenario():
        immediate = service.start(body)
        await service.close()
        assert repo.get(immediate['eval_id'])['status'] == 'failed'
        assert not service.runner._running
        entered = asyncio.Event()
        async def wait(*args):
            entered.set()
            await asyncio.Event().wait()
        with patch.object(service.archive,'execute_suite',wait):
            result = service.start(body)
            await entered.wait()
            await service.close()
            assert repo.get(result['eval_id'])['status'] == 'failed'
            assert not service.runner._running
        async def fail(*args):
            repo.fail(args[0],'固定失败')
            raise ValueError('内部错误')
        with patch.object(service.archive,'execute_suite',fail):
            result = service.start(body)
            await asyncio.get_running_loop()._smartbrief_evaluation_handle
            assert repo.get(result['eval_id'])['status'] == 'failed'
    asyncio.run(scenario())


async def _start(body):
    from services.evaluation_service import start
    return start(body)
