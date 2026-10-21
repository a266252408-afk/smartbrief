"""受控队列、持久化领取及并发限额。"""
import asyncio

import pytest

from db import database, task_repository
from services.task_service import TaskService


def test_third_task_stays_queued_until_a_slot_is_free():
    database.init_db()
    async def scenario():
        events = [asyncio.Event(), asyncio.Event(), asyncio.Event()]
        started = []
        async def runner(task_id, topic, resume=False):
            started.append(topic)
            await events[int(topic)].wait()
            task_repository.update_task(task_id, status="completed")
        service = TaskService(max_concurrency=2, runner=runner)
        try:
            jobs = [await service.submit(str(i)) for i in range(3)]
            await asyncio.sleep(0.02)
            assert started == ["0", "1"]
            assert task_repository.get_task(jobs[2]["task_id"])["execution_status"] == "queued"
            events[0].set()
            await asyncio.sleep(0.02)
            assert started == ["0", "1", "2"]
            for event in events:
                event.set()
        finally:
            await service.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("limit", [0, -1, True])
def test_invalid_concurrency_rejected(limit):
    with pytest.raises(ValueError):
        TaskService(max_concurrency=limit)


@pytest.mark.parametrize("env", [{"MAX_CONCURRENT_TASKS":"0"}, {"MAX_CONCURRENT_TASKS":"bad"},
    {"WEB_CONCURRENCY":"2"}, {"UVICORN_WORKERS":"2"}])
def test_configuration_refuses_invalid_or_multiworker(monkeypatch, env):
    from services.task_service import concurrency_limit, TaskControlError
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    with pytest.raises(TaskControlError, match="单worker"):
        concurrency_limit()


def test_default_and_custom_limit(monkeypatch):
    from services.task_service import concurrency_limit
    monkeypatch.delenv("MAX_CONCURRENT_TASKS", raising=False)
    assert concurrency_limit() == 2
    monkeypatch.setenv("MAX_CONCURRENT_TASKS", "3")
    assert concurrency_limit() == 3


def test_duplicate_schedule_and_shutdown_preserve_queued():
    database.init_db()
    async def scenario():
        started = []
        async def runner(task_id, topic, resume=False):
            started.append(task_id)
            await asyncio.Event().wait()
        service = TaskService(max_concurrency=1, runner=runner)
        one = await service.submit("运行")
        two = await service.submit("等待")
        first = service.handles[one["task_id"]]
        assert service.schedule(one["task_id"]) is first
        await asyncio.sleep(0.02)
        await service.close()
        assert started == [one["task_id"]]
        assert task_repository.get_task(one["task_id"])["status"] == "paused"
        assert task_repository.get_task(two["task_id"])["status"] == "queued"
        from services.task_service import TaskControlError
        with pytest.raises(TaskControlError):
            await service.submit("不能提交")
        with pytest.raises(TaskControlError):
            service.schedule(two["task_id"])
    asyncio.run(scenario())


def test_start_claims_persisted_queue_once_and_wait_missing():
    from services.task_service import TaskControlError
    database.init_db()
    task_repository.create_task("restored", "排队", initial_status="queued")
    async def scenario():
        calls = []
        async def runner(task_id, topic, resume=False):
            calls.append(task_id)
            task_repository.update_task(task_id, status="completed")
        service = TaskService(runner=runner)
        service.start()
        service.start()
        assert (await service.wait("restored"))["status"] == "completed"
        service.schedule("restored")
        await asyncio.sleep(0.01)
        assert calls == ["restored"]
        with pytest.raises(TaskControlError) as exc:
            await service.wait("missing")
        assert exc.value.status == 404
        await service.close()
    asyncio.run(scenario())


def test_creation_and_claim_failure_are_not_success(monkeypatch):
    from services.task_service import TaskControlError
    from db import task_control_repository as controls
    database.init_db()
    async def scenario():
        from unittest.mock import AsyncMock
        runner = AsyncMock()
        service = TaskService(runner=runner)
        with monkeypatch.context() as scoped:
            def broken(*args, **kwargs):
                raise RuntimeError("SECRET_DB")
            scoped.setattr(task_repository,"create_task",broken)
            with pytest.raises(TaskControlError, match="任务创建失败"):
                await service.submit("失败")
        with pytest.raises(TaskControlError) as exc:
            await service.submit("   ")
        assert exc.value.status == 422
        task = await service.submit("领取失败")
        with monkeypatch.context() as scoped:
            scoped.setattr(controls,"claim",lambda _: (_ for _ in ()).throw(RuntimeError("SECRET_DB")))
            await service.handles[task["task_id"]]
        assert task_repository.get_task(task["task_id"])["status"] == "queued"
        assert runner.call_count == 0
        await service.close()
    asyncio.run(scenario())


def test_instance_lock_prevents_second_owner_and_releases(tmp_path):
    from services.instance_lock import InstanceLock
    first = InstanceLock(tmp_path / "instance.lock")
    second = InstanceLock(first.path)
    first.acquire()
    try:
        with pytest.raises(RuntimeError, match="单worker"):
            second.acquire()
    finally:
        first.release()
    second.acquire()
    second.release()
    second.release()


def test_worker_failure_and_unavailable_failure_write(monkeypatch):
    database.init_db()
    async def scenario():
        async def runner(*args, **kwargs):
            raise RuntimeError("SECRET_PROMPT")
        service = TaskService(runner=runner)
        task = await service.submit("失败")
        assert (await service.wait(task["task_id"]))["status"] == "failed"
        second = await service.submit("数据库故障")
        with monkeypatch.context() as scoped:
            scoped.setattr(task_repository,"update_task",lambda *a,**k: (_ for _ in ()).throw(RuntimeError("SECRET_DB")))
            await service.handles[second["task_id"]]
        assert task_repository.get_task(second["task_id"])["status"] == "running"
        await service.close()
    asyncio.run(scenario())


def test_failed_pause_and_service_cache_are_explicit(monkeypatch):
    from db import task_control_repository as controls
    from services.task_service import get_task_service
    database.init_db()
    async def scenario():
        service = get_task_service()
        assert get_task_service() is service
        async def runner(*args, **kwargs):
            await asyncio.Event().wait()
        service.runner = runner
        task = await service.submit("暂停写入失败")
        await asyncio.sleep(0.01)
        with monkeypatch.context() as scoped:
            scoped.setattr(controls,"pause",lambda *a: (_ for _ in ()).throw(RuntimeError("SECRET")))
            await service.close()
        assert task_repository.get_task(task["task_id"])["status"] == "running"
        assert get_task_service() is not service
        await get_task_service().close()
    asyncio.run(scenario())


def test_startup_failure_releases_instance_lock(monkeypatch):
    from main import app, lifespan, mcp_manager
    from unittest.mock import AsyncMock
    from services.instance_lock import InstanceLock
    monkeypatch.setattr(mcp_manager, "start", AsyncMock(side_effect=RuntimeError("固定启动失败")))
    monkeypatch.setattr(mcp_manager, "close", AsyncMock())
    async def scenario():
        with pytest.raises(RuntimeError, match="固定启动失败"):
            async with lifespan(app):
                pass
        lock = InstanceLock(database.DB_PATH.with_suffix(".runner.lock"))
        lock.acquire()
        lock.release()
    asyncio.run(scenario())
    mcp_manager.close.assert_awaited_once()


def test_claimed_corrupt_payload_does_not_leave_dead_worker_running():
    from unittest.mock import AsyncMock
    database.init_db()
    async def scenario():
        runner = AsyncMock()
        service = TaskService(runner=runner)
        task = await service.submit("损坏任务")
        with database.get_connection() as conn:
            conn.execute("UPDATE tasks SET document_ids='invalid-json' WHERE task_id=?",(task["task_id"],))
        await service.handles[task["task_id"]]
        with database.get_connection() as conn:
            state = conn.execute("SELECT execution_status,error_code FROM tasks WHERE task_id=?",(task["task_id"],)).fetchone()
        assert tuple(state) == ("failed","TASK_EXECUTION_FAILED")
        assert runner.call_count == 0
        await service.close()
    asyncio.run(scenario())
