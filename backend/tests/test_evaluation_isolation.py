"""离线任务在恢复、重生成与服务重启时保持供应商隔离。"""
import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from db import database, task_repository
from evaluation.context import evaluation_context
from evaluation.offline import OfflineDependencies, execute_case
from evaluation.suite import load_suite
from services.task_service import TaskService, TaskControlError
from tests.auth_support import ADMIN_HEADERS


def offline_task(task_id='offline', status='failed', legacy=False):
    task_repository.create_task(task_id, '[离线协议] 预算案例' if legacy else '合成案例',
        initial_status='queued', metadata={} if legacy else {
            'execution_mode': 'offline', 'evaluation_id': 'eval_owner', 'evaluation_case_id': 'C01'})
    if status != 'queued':
        task_repository.update_task(task_id, status=status, quality_status='not_run')


@pytest.mark.parametrize('legacy', [False, True])
@pytest.mark.parametrize('action', ['resume', 'regenerate'])
def test_service_rejects_offline_actions_without_mutation(legacy, action):
    database.init_db()
    offline_task(legacy=legacy)
    async def scenario():
        runner = AsyncMock()
        service = TaskService(runner=runner)
        before = task_repository.get_task('offline')
        with pytest.raises(TaskControlError) as exc:
            await getattr(service, action)('offline')
        assert (exc.value.code, exc.value.status) == ('OFFLINE_TASK_NOT_EXECUTABLE', 409)
        assert task_repository.get_task('offline') == before
        assert task_repository.list_tasks()[1] == 1
        runner.assert_not_called()
        await service.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('legacy', [False, True])
@pytest.mark.parametrize('path,status', [('/api/v1/tasks/offline/resume', 400),
    ('/api/v2/tasks/offline/resume', 409), ('/api/v2/tasks/offline/regenerate', 409)])
def test_http_cannot_restart_offline_task(legacy, path, status):
    database.init_db()
    offline_task(legacy=legacy)
    from main import app
    response = TestClient(app).post(path, headers=ADMIN_HEADERS, json={})
    assert response.status_code == status, response.text
    if path.startswith('/api/v2/'):
        assert response.json()['error']['code'] == 'OFFLINE_TASK_NOT_EXECUTABLE'
    assert task_repository.get_task('offline')['status'] == 'failed'
    assert task_repository.list_tasks()[1] == 1


@pytest.mark.parametrize('legacy', [False, True])
def test_restarted_worker_never_runs_offline_queue(legacy):
    database.init_db()
    offline_task(status='queued', legacy=legacy)
    async def scenario():
        runner = AsyncMock()
        service = TaskService(runner=runner)
        service.start()
        task = await service.wait('offline')
        runner.assert_not_called()
        assert task['status'] == 'failed' and task['quality_status'] == 'not_run'
        assert task['error_code'] == 'OFFLINE_CONTEXT_REQUIRED'
        await service.close()
    asyncio.run(scenario())


def test_real_case_persists_offline_origin(tmp_path):
    database.init_db()
    case = next(c for c in load_suite()[0] if c['scenario'] == 'budget')
    async def scenario():
        dependencies = OfflineDependencies(case, tmp_path / 'protocol', evaluation_id='eval_actual')
        task_id = await execute_case(dependencies)
        meta = task_repository.get_task(task_id)['metadata']
        assert meta['execution_mode'] == 'offline'
        assert meta['evaluation_id'] == 'eval_actual'
        assert meta['evaluation_case_id'] == case['case_id']
        service = TaskService(runner=AsyncMock())
        with pytest.raises(TaskControlError, match='评测中心'):
            await service.resume(task_id)
        await service.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('owner,case_id', [('eval_other','C01'), ('eval_owner','C02')])
def test_wrong_offline_context_cannot_execute(tmp_path, owner, case_id):
    database.init_db()
    offline_task(status='queued')
    async def scenario():
        context = OfflineDependencies({'case_id':case_id}, tmp_path, evaluation_id=owner)
        token = evaluation_context.set(context)
        runner = AsyncMock()
        service = TaskService(runner=runner)
        try:
            service.start()
            assert (await service.wait('offline'))['error_code'] == 'OFFLINE_CONTEXT_REQUIRED'
            runner.assert_not_called()
        finally:
            await service.close()
            evaluation_context.reset(token)
    asyncio.run(scenario())


def test_matching_context_can_execute_and_offline_cancel_still_works(tmp_path):
    database.init_db()
    offline_task(status='queued')
    async def scenario():
        dependencies = OfflineDependencies({'case_id':'C01'}, tmp_path, evaluation_id='eval_owner')
        token = dependencies.activate()
        async def runner(task_id, topic, resume=False):
            task_repository.update_task(task_id, status='completed', quality_status='not_run')
        service = TaskService(runner=runner)
        try:
            service.start()
            assert (await service.wait('offline'))['status'] == 'completed'
            pending = await service.submit('[离线协议] 待取消')
            assert task_repository.get_task(pending['task_id'])['metadata']['evaluation_case_id'] == 'C01'
            assert (await service.cancel(pending['task_id']))['status'] == 'cancelled'
        finally:
            await service.close()
            evaluation_context.reset(token)
    asyncio.run(scenario())


def test_user_cannot_supply_execution_origin():
    database.init_db()
    from main import app
    response = TestClient(app).post('/api/v2/tasks', headers=ADMIN_HEADERS,
        json={'topic':'普通任务', 'metadata':{'execution_mode':'offline', 'evaluation_case_id':'C01'}})
    assert response.status_code == 422
    assert task_repository.list_tasks()[1] == 0


def test_online_regeneration_keeps_explicit_mode_even_with_reserved_topic():
    database.init_db()
    task_repository.create_task('online', '[离线协议] 用户输入的普通主题',
        metadata={'execution_mode':'online'})
    task_repository.update_task('online', status='failed', quality_status='not_run')
    async def scenario():
        async def runner(task_id, topic, resume=False):
            task_repository.update_task(task_id, status='completed', quality_status='not_run')
        service = TaskService(runner=runner)
        result = await service.regenerate('online')
        task = await service.wait(result['task_id'])
        assert task['metadata']['execution_mode'] == 'online'
        assert task['status'] == 'completed'
        await service.close()
    asyncio.run(scenario())


def test_metadata_validation_and_contract():
    from jsonschema import Draft202012Validator
    from services.task_metadata import TaskMetadata, is_offline_task
    schema = json.loads((Path(__file__).resolve().parents[2] / 'docs/schemas/contracts.json').read_text('utf-8'))
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator({'$ref':'#/$defs/TaskMetadata', '$defs':schema['$defs']})
    valid = TaskMetadata(execution_mode='offline', evaluation_id='eval_owner', evaluation_case_id='C01').model_dump()
    validator.validate(valid)
    for body in ({'execution_mode':'offline'}, {'evaluation_case_id':'C01'},
                 {'execution_mode':'online','evaluation_id':'eval_owner'},
                 {'execution_mode':'offline','evaluation_case_id':' '}):
        with pytest.raises(ValueError):
            TaskMetadata.model_validate(body)
    assert not is_offline_task(None)
    assert not is_offline_task({'topic':'[离线协议] 用户主题','metadata':{'execution_mode':'online'}})
    assert not is_offline_task({'topic':'旧普通任务','metadata':{}})
