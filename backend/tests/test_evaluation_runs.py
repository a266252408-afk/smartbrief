"""专项应用服务/API生命周期和归档，不用预期数据填成绩。"""
import asyncio
import time
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient
import pytest
from tests.auth_support import ADMIN_HEADERS, READ_HEADERS


def outcome(case_id,*args,**kwargs):
    return dict(case_id=case_id,check_status='pass',test_count=1,passed=1,failed=0,skipped=0,
                execution_status='completed',report_quality='unknown',exit_code=0,duration_seconds=.1,
                engine='pytest_mechanism_regression',test_names=['fixture'],error=None,scope='合成单测结果')


def test_service_persists_partial_and_reopens(monkeypatch):
    from services import evaluation_run_service as service
    monkeypatch.setattr(service,'run_case',AsyncMock(side_effect=outcome))
    async def scenario():
        body=service.MechanismRequest(case_ids=['R009','G023'])
        record=service.start(body)
        with pytest.raises(service.EvaluationError) as exc:
            service.start(body)
        assert exc.value.status==409
        await service._handle
        saved=service.get(record['run_id'])
        assert saved['execution_status']=='completed' and saved['summary']['passed']==2
        assert saved['report_quality']=='unknown' and saved['metadata']['online_validated'] is False
        assert service.list_runs()['items'][0]['run_id']==record['run_id']
        await service.close()
    asyncio.run(scenario())


def test_close_before_execution_and_orphan_recovery():
    from services import evaluation_run_service as service
    from db import evaluation_run_repository as repo
    async def scenario():
        record=service.start(service.MechanismRequest(case_ids=['R009']))
        await service.close()
        assert service.get(record['run_id'])['execution_status']=='failed'
    asyncio.run(scenario())
    repo.save(dict(run_id='er_'+'b'*32,execution_status='running',results=[]))
    service.interrupt_orphans()
    assert repo.get('er_'+'b'*32)['execution_status']=='failed'


def test_execution_error_preserves_completed_part(monkeypatch):
    from services import evaluation_run_service as service
    monkeypatch.setattr(service,'run_case',AsyncMock(side_effect=[outcome('R009'),RuntimeError('SECRET_INTERNAL')]))
    async def scenario():
        record=service.start(service.MechanismRequest(case_ids=['R009','R010']))
        await service._handle
        saved=service.get(record['run_id'])
        assert saved['execution_status']=='failed' and saved['summary']['processed']==1
        assert 'SECRET_INTERNAL' not in str(saved)
        await service.close()
    asyncio.run(scenario())


def test_http_auth_body_and_unknown(monkeypatch):
    from main import app
    from services import evaluation_run_service as service
    monkeypatch.setattr(service,'run_case',AsyncMock(side_effect=outcome))
    with TestClient(app,headers=ADMIN_HEADERS) as client:
        assert client.get('/api/v2/evaluation-center/capabilities').json()['data']['supported_case_ids']
        assert client.post('/api/v2/evaluation-center/runs',headers=READ_HEADERS,json={'case_ids':['R009']}).status_code==403
        for body in ({'case_ids':['C026']},{'case_ids':['R009','R009']},{'case_ids':['R009'],'command':'secret'}):
            assert client.post('/api/v2/evaluation-center/runs',json=body).status_code==422
        response=client.post('/api/v2/evaluation-center/runs',json={'case_ids':['R009']})
        assert response.status_code==202
        rid=response.json()['data']['run_id']
        assert client.get('/api/v2/evaluation-center/runs/'+rid).status_code==200
        assert client.get('/api/v2/evaluation-center/runs').json()['data']['items']
        assert client.get('/api/v2/evaluation-center/runs/er_'+'0'*32).status_code==404


def test_junit_error_and_environment_redaction(tmp_path,monkeypatch):
    from evaluation.mechanism import parse_junit, child_environment
    p=tmp_path/'error.xml'
    p.write_text('<testsuites><testsuite><testcase name="error"><error/></testcase></testsuite></testsuites>')
    assert parse_junit(p,1)['check_status']=='fail'
    p.write_text('<testsuites><testsuite><testcase name="good"/></testsuite></testsuites>')
    assert parse_junit(p,2)['check_status']=='unknown'
    monkeypatch.setenv('OPENAI_API_KEY','PRIVATE')
    monkeypatch.setenv('SMARTBRIEF_ADMIN_TOKEN','PRIVATE')
    monkeypatch.setenv('COV_CORE_SOURCE','PRIVATE')
    env=child_environment(tmp_path)
    assert 'PRIVATE' not in str(env) and env['DATA_DIR'].startswith(str(tmp_path))


def test_launch_failure_and_timeout_are_unknown(tmp_path,monkeypatch):
    from evaluation import mechanism
    monkeypatch.setattr(mechanism.asyncio,'create_subprocess_exec',AsyncMock(side_effect=OSError('SECRET')))
    result=asyncio.run(mechanism.run_case('R009',tmp_path/'failure'))
    assert result['check_status']=='unknown' and result['execution_status']=='failed' and 'SECRET' not in str(result)
    class Process:
        returncode=None
        wait=AsyncMock(side_effect=[TimeoutError(),-9])
        def kill(self):
            self.returncode=-9
    process=Process()
    monkeypatch.setattr(mechanism.asyncio,'create_subprocess_exec',AsyncMock(return_value=process))
    result=asyncio.run(mechanism.run_case('R009',tmp_path/'timeout',timeout=.01))
    assert result['check_status']=='unknown' and '超时' in result['error'] and process.returncode==-9


def test_service_storage_and_engine_failures(monkeypatch):
    from services import evaluation_run_service as service
    from db import evaluation_run_repository as repo
    original=service.capabilities
    body=service.MechanismRequest(case_ids=['R009'])
    monkeypatch.setattr(service,'capabilities',lambda: {'available':False})
    with pytest.raises(service.EvaluationError,match='pytest'):
        service.start(body)
    monkeypatch.setattr(service,'capabilities',original)
    def broken(*args):
        raise OSError('SECRET')
    monkeypatch.setattr(repo,'save',broken)
    with pytest.raises(service.EvaluationError,match='不可写'):
        service.start(body)
    monkeypatch.setattr(repo,'get',broken)
    with pytest.raises(service.EvaluationError,match='不可读'):
        service.get('er_'+'1'*32)
    monkeypatch.setattr(repo,'list_runs',broken)
    with pytest.raises(service.EvaluationError,match='不可读'):
        service.list_runs()


def test_cancel_running_process_preserves_archive(monkeypatch):
    from services import evaluation_run_service as service
    started=asyncio.Event()
    async def waiting(*args):
        started.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(service,'run_case',waiting)
    async def scenario():
        record=service.start(service.MechanismRequest(case_ids=['R009']))
        await started.wait()
        await service.close()
        saved=service.get(record['run_id'])
        assert saved['execution_status']=='failed' and saved['summary']['processed']==0
    asyncio.run(scenario())


def test_atomic_repository_failure_cleans_temporary(tmp_path,monkeypatch):
    from db import evaluation_run_repository as repo
    def broken(*args):
        raise OSError('disk')
    monkeypatch.setattr(repo.os,'replace',broken)
    with pytest.raises(OSError):
        repo.save(dict(run_id='er_'+'2'*32,results=[]))
    assert not list(repo.root().rglob('*.tmp'))


def test_http_service_failures_are_enveloped(monkeypatch):
    from main import app
    from services import evaluation_run_service as service
    def broken(*args):
        raise service.EvaluationError('RUN_STORAGE_FAILED','专项归档不可用')
    with TestClient(app,headers=ADMIN_HEADERS) as client:
        monkeypatch.setattr(service,'start',broken)
        response=client.post('/api/v2/evaluation-center/runs',json={'case_ids':['R009']})
        assert response.status_code==503 and response.json()['error']['code']=='RUN_STORAGE_FAILED'
        monkeypatch.setattr(service,'list_runs',broken)
        assert client.get('/api/v2/evaluation-center/runs').status_code==503


def test_real_http_to_child_and_persistent_result():
    from main import app
    from db import evaluation_run_repository as repo
    with TestClient(app,headers=ADMIN_HEADERS) as client:
        response=client.post('/api/v2/evaluation-center/runs',json={'case_ids':['R009']})
        assert response.status_code==202
        rid=response.json()['data']['run_id']
        deadline=time.monotonic()+60
        while time.monotonic()<deadline:
            record=client.get('/api/v2/evaluation-center/runs/'+rid).json()['data']
            if record['execution_status']!='running':
                break
            time.sleep(.05)
        assert record['execution_status']=='completed'
        assert record['summary']['passed']==1 and record['results'][0]['test_count']==1
        assert repo.get(rid)['results'][0]['check_status']=='pass'
        assert record['report_quality']=='unknown'
