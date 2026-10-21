"""取消先落库、排队不执行与完成竞争保护。"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from db import database, task_repository
from services.task_service import TaskService


def test_cancel_queued_never_runs_and_running_cannot_complete_late():
    database.init_db()
    async def scenario():
        started = []
        async def runner(task_id, topic, resume=False):
            started.append(topic)
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                # 模拟供应商吞掉取消，迟到代码也不能把任务覆盖为完成。
                task_repository.update_task(task_id, status="completed")
        service = TaskService(max_concurrency=1, runner=runner)
        one = await service.submit("运行")
        two = await service.submit("排队")
        await asyncio.sleep(0.01)
        await service.cancel(two["task_id"])
        await service.cancel(one["task_id"])
        await asyncio.sleep(0.01)
        assert started == ["运行"]
        for job in (one,two):
            task = task_repository.get_task(job["task_id"])
            assert task["status"] == "cancelled" and task["quality_status"] == "not_run"
        assert (await service.cancel(one["task_id"]))["already_cancelled"]
        await service.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("state", ["completed","failed","paused"])
def test_terminal_cancel_conflict_and_unknown(state):
    from services.task_service import TaskControlError
    database.init_db()
    task_repository.create_task("terminal","终态")
    task_repository.update_task("terminal",status=state)
    async def scenario():
        service = TaskService()
        with pytest.raises(TaskControlError) as exc:
            await service.cancel("terminal")
        assert exc.value.status == 409
        with pytest.raises(TaskControlError) as exc:
            await service.cancel("absent")
        assert exc.value.status == 404
        await service.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["update","audit"])
def test_cancel_write_failure_does_not_signal_worker_or_leave_partial_state(failure):
    from services.task_service import TaskControlError
    database.init_db()
    async def scenario():
        release, entered = asyncio.Event(), asyncio.Event()
        was_cancelled = []
        async def runner(task_id, topic, resume=False):
            entered.set()
            try:
                await release.wait()
                task_repository.update_task(task_id,status="completed")
            except asyncio.CancelledError:
                was_cancelled.append(True)
                raise
        service = TaskService(runner=runner)
        task = await service.submit("取消失败")
        await entered.wait()
        with database.get_connection() as conn:
            if failure == "update":
                conn.execute("CREATE TRIGGER reject_cancel BEFORE UPDATE ON tasks WHEN NEW.status='cancelled' BEGIN SELECT RAISE(ABORT,'SECRET_DB'); END")
            else:
                conn.execute("CREATE TRIGGER reject_cancel BEFORE INSERT ON task_logs BEGIN SELECT RAISE(ABORT,'SECRET_DB'); END")
        with pytest.raises(TaskControlError,match="持久化失败"):
            await service.cancel(task["task_id"])
        assert task_repository.get_task(task["task_id"])["status"] == "running"
        assert not task_repository.get_logs(task["task_id"]) and not was_cancelled
        with database.get_connection() as conn:
            conn.execute("DROP TRIGGER reject_cancel")
        release.set()
        assert (await service.wait(task["task_id"]))["status"] == "completed"
        await service.close()
    asyncio.run(scenario())


def test_completion_wins_cancel_does_not_touch_report_or_logs():
    from services.task_service import TaskControlError
    database.init_db()
    async def scenario():
        completed, release = asyncio.Event(), asyncio.Event()
        async def runner(task_id, topic, resume=False):
            task_repository.finalize_task(task_id,dict(status="completed",quality_status="passed",final_report="已完成报告"),
                                          [{"step_id":1,"type":"Observation","content":"最终日志"}])
            completed.set()
            await release.wait()
        service = TaskService(runner=runner)
        task = await service.submit("完成先赢")
        await completed.wait()
        with pytest.raises(TaskControlError) as exc:
            await service.cancel(task["task_id"])
        assert exc.value.status == 409
        assert task_repository.get_task(task["task_id"])["final_report"] == "已完成报告"
        assert len(task_repository.get_logs(task["task_id"])) == 1
        release.set()
        await service.wait(task["task_id"])
        await service.close()
    asyncio.run(scenario())


def test_finalization_transaction_rolls_back_report_and_logs():
    from services.execution_context import ExecutionPersistenceError, ExecutionStopped
    database.init_db()
    task_repository.create_task("atomic","原任务")
    task_repository.append_log("atomic",1,"Observation","原日志")
    with database.get_connection() as conn:
        conn.execute("CREATE TRIGGER reject_log BEFORE INSERT ON task_logs BEGIN SELECT RAISE(ABORT,'SECRET_LOG'); END")
    fields = dict(status="completed",quality_status="passed",final_report="新报告")
    logs = [{"step_id":1,"type":"Observation","content":"新日志"}]
    with pytest.raises(ExecutionPersistenceError,match="持久化失败"):
        task_repository.finalize_task("atomic",fields,logs)
    assert task_repository.get_task("atomic")["status"] == "running"
    assert task_repository.get_task("atomic")["final_report"] is None
    assert task_repository.get_logs("atomic")[0]["content"] == "原日志"
    with database.get_connection() as conn:
        conn.execute("DROP TRIGGER reject_log")
    task_repository.finalize_task("atomic",fields,logs)
    with pytest.raises(ExecutionStopped):
        task_repository.finalize_task("atomic",fields,logs)
    assert [r["content"] for r in task_repository.get_logs("atomic")] == ["新日志"]


@pytest.mark.parametrize("kind", ["different_token","missing","cancelled"])
def test_old_execution_lease_cannot_write_or_run_next_stage(kind):
    from db import task_control_repository as controls
    from services.execution_context import execution_lease, assert_active, ExecutionStopped
    database.init_db()
    task_repository.create_task("lease","主题",initial_status="queued")
    token = controls.claim("lease")
    with database.get_connection() as conn:
        if kind == "different_token":
            conn.execute("UPDATE tasks SET run_token='new' WHERE task_id='lease'")
        elif kind == "missing":
            conn.execute("DELETE FROM tasks WHERE task_id='lease'")
        else:
            conn.execute("UPDATE tasks SET execution_status='cancelled',status='cancelled' WHERE task_id='lease'")
    context = execution_lease.set(("lease",token))
    try:
        assert_active("other")  # 当前上下文不授权跨任务写入。
        with pytest.raises(ExecutionStopped):
            assert_active()
        with pytest.raises(ExecutionStopped):
            task_repository.update_task("lease",status="completed")
    finally:
        execution_lease.reset(context)
    assert_active()


def test_real_graph_sdk_cancellation_stops_reviewer_and_memory(monkeypatch):
    from agents import react_researcher
    from llm import client
    from memory import long_term_memory
    from services.usage_service import usage_context
    from db import usage_repository
    from tests.fakes import fixture_search
    database.init_db()
    monkeypatch.setattr(long_term_memory,"_MEMORY_ENABLED",False)
    remember = Mock()
    monkeypatch.setattr(long_term_memory,"remember",remember)
    monkeypatch.setattr(react_researcher.mcp_manager,"call_tool",AsyncMock(return_value=fixture_search("测试")))
    async def scenario():
        writer = asyncio.Event()
        calls = []
        research = iter(['{"action":"web_search","query":"测试"}','{"action":"done"}'])
        async def create(**kwargs):
            agent = usage_context.get()[1]
            calls.append(agent)
            if agent == "planner":
                text = '[{"id":1,"agent":"researcher","task":"搜","depends_on":[]},{"id":2,"agent":"writer","task":"写","depends_on":[1]}]'
            elif agent == "researcher":
                text = next(research)
            elif agent == "writer":
                writer.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    text = "## 迟到报告[S1]"
            else:
                pytest.fail("取消后仍调用后续Agent")
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))],usage=None)
        monkeypatch.setattr(client,"get_client",lambda:SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
        service = TaskService()
        task = await service.submit("真实图取消")
        await asyncio.wait_for(writer.wait(),5)
        await service.cancel(task["task_id"])
        handle = service.handles.get(task["task_id"])
        if handle:
            await handle
        record = task_repository.get_task(task["task_id"])
        assert record["status"] == "cancelled" and record["final_report"] is None
        assert "reviewer" not in calls and "rewriter" not in calls
        remember.assert_not_called()
        rows = usage_repository.list_usage(task["task_id"])
        assert rows[-1]["status"] == "cancelled" and rows[-1]["agent"] == "writer"
        assert len(task_repository.get_logs(task["task_id"])) == 1
        await service.close()
    asyncio.run(scenario())


def test_cancel_http_auth_errors_and_idempotence():
    from main import app
    from tests.auth_support import ADMIN_HEADERS, READ_HEADERS
    database.init_db()
    task_repository.create_task("queued","等待",initial_status="queued")
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url="http://test") as client:
            for headers,status in [({},401),(READ_HEADERS,403)]:
                assert (await client.post("/api/v2/tasks/queued/cancel",headers=headers)).status_code == status
            one = await client.post("/api/v2/tasks/queued/cancel",headers=ADMIN_HEADERS)
            two = await client.post("/api/v2/tasks/queued/cancel",headers=ADMIN_HEADERS)
            assert one.status_code == two.status_code == 200
            assert not one.json()["data"]["already_cancelled"] and two.json()["data"]["already_cancelled"]
            assert len(task_repository.get_logs("queued")) == 1
            assert (await client.post("/api/v2/tasks/absent/cancel",headers=ADMIN_HEADERS)).status_code == 404
            task_repository.create_task("done","完成")
            task_repository.update_task("done",status="completed")
            response = await client.post("/api/v2/tasks/done/cancel",headers=ADMIN_HEADERS)
            assert response.status_code == 409 and response.json()["error"]["request_id"]
            assert (await client.post("/api/v1/tasks/queued/resume",headers=ADMIN_HEADERS)).status_code == 400
    asyncio.run(scenario())


def test_state_read_failure_is_fatal_and_sanitized(monkeypatch):
    from services.execution_context import execution_lease, assert_active, ExecutionPersistenceError
    database.init_db()
    token = execution_lease.set(("task","lease"))
    try:
        monkeypatch.setattr(database,"get_connection",lambda: (_ for _ in ()).throw(RuntimeError("SECRET_DB")))
        with pytest.raises(ExecutionPersistenceError,match="运行状态不可读取"):
            assert_active()
    finally:
        execution_lease.reset(token)


def test_sdk_guard_storage_failure_does_not_become_agent_fallback(monkeypatch):
    from services import execution_context
    from services.usage_service import usage_context
    from services.execution_context import ExecutionPersistenceError
    from llm import client
    from db import usage_repository
    database.init_db()
    create = AsyncMock()
    monkeypatch.setattr(client,"get_client",lambda:SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    monkeypatch.setattr(execution_context,"assert_active",lambda: (_ for _ in ()).throw(ExecutionPersistenceError("固定状态故障")))
    token = usage_context.set(("guard","writer"))
    try:
        with pytest.raises(ExecutionPersistenceError):
            asyncio.run(client.chat([]))
    finally:
        usage_context.reset(token)
    assert create.call_count == 0 and usage_repository.list_usage("guard")[0]["status"] == "failed"
