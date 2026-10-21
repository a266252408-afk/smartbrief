"""受控创建入口、权限和跨入口同一个并发限额。"""
import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient

from db import database, task_repository
from tests.auth_support import ADMIN_HEADERS, READ_HEADERS


@pytest.mark.parametrize("body", [{}, {"topic":" "}, {"topic":"x","document_ids":["bad"]}, {"topic":"x","extra":1}])
def test_invalid_create_request_uses_v2_error_envelope(body):
    from main import app
    database.init_db()
    response = TestClient(app).post("/api/v2/tasks",json=body,headers=ADMIN_HEADERS)
    assert response.status_code == 422 and response.json()["error"]["code"] == "INVALID_TASK_REQUEST"
    assert task_repository.list_tasks()[1] == 0


def test_create_auth_and_database_failure(monkeypatch):
    from main import app
    database.init_db()
    for headers, status in [({},401),(READ_HEADERS,403)]:
        assert TestClient(app).post("/api/v2/tasks",json={"topic":"主题"},headers=headers).status_code == status
    def fail(*args, **kwargs):
        raise RuntimeError("SECRET_DB")
    monkeypatch.setattr(task_repository,"create_task",fail)
    response = TestClient(app).post("/api/v2/tasks",json={"topic":"主题"},headers=ADMIN_HEADERS)
    assert response.status_code == 503 and "SECRET_DB" not in response.text


def test_generate_regenerate_and_evaluation_share_queue(monkeypatch):
    from graph import workflow
    from main import app
    from evaluation import runner as evaluation
    from services.task_service import get_task_service
    database.init_db()
    task_repository.create_task("old","重生成")
    task_repository.update_task("old",status="completed")
    async def scenario():
        release = asyncio.Event()
        active, maximum, started = 0, 0, []
        async def run(task_id, topic, resume=False):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum,active)
            started.append(task_id)
            try:
                await release.wait()
                task_repository.update_task(task_id,status="completed",quality_status="passed",final_report="离线夹具")
            finally:
                active -= 1
        monkeypatch.setattr(workflow,"run_task",run)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url="http://test",headers=ADMIN_HEADERS) as client:
            one = await client.post("/api/v2/tasks",json={"topic":"第一"})
            two = await client.post("/api/v1/generate",json={"topic":"第二"})
            assert one.status_code == 202 and one.json()["data"]["status"] == "queued"
            assert one.json()["meta"]["request_id"] and two.json()["status"] == "queued"
            third = await client.post("/api/v2/tasks/old/regenerate")
            tid = third.json()["data"]["task_id"]
            evaluation_job = asyncio.create_task(evaluation._run_single_case({"topic":"队列评测","keywords":[]},1))
            await asyncio.sleep(0.03)
            assert len(started) == 2
            assert task_repository.get_task(tid)["status"] == "queued"
            assert task_repository.list_tasks(status="queued")[1] == 2
            release.set()
            evaluated = await evaluation_job
            await asyncio.gather(*list(get_task_service().handles.values()))
            assert evaluated["execution_success"] and maximum == 2 and len(started) == 4
            assert task_repository.get_task(tid)["status"] == "completed"
            await get_task_service().close()
    asyncio.run(scenario())


def test_bad_concurrency_configuration_does_not_create_regeneration(monkeypatch):
    from main import app
    database.init_db()
    task_repository.create_task("old","原任务")
    task_repository.update_task("old",status="completed")
    monkeypatch.setenv("MAX_CONCURRENT_TASKS","0")
    response = TestClient(app).post("/api/v2/tasks/old/regenerate",headers=ADMIN_HEADERS)
    assert response.status_code == 503 and task_repository.list_tasks()[1] == 1


def test_startup_pause_failure_is_not_ignored(monkeypatch):
    from main import app, lifespan, mcp_manager
    from services.instance_lock import InstanceLock
    monkeypatch.setattr(task_repository,"mark_interrupted_tasks_as_paused",lambda: (_ for _ in ()).throw(RuntimeError("SECRET_DB")))
    monkeypatch.setattr(mcp_manager,"close",AsyncMock())
    async def scenario():
        with pytest.raises(RuntimeError,match="启动已停止"):
            async with lifespan(app):
                pass
        lock = InstanceLock(database.DB_PATH.with_suffix(".runner.lock"))
        lock.acquire()
        lock.release()
    asyncio.run(scenario())
