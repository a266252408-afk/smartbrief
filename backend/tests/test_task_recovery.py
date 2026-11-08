"""持久化动作去重、恢复及重启后状态。"""
import asyncio

import httpx
import pytest

from db import database, task_repository
from tests.auth_support import ADMIN_HEADERS


def test_create_idempotency_survives_reopen_and_body_conflicts():
    from main import app
    database.init_db()
    async def scenario():
        from services.task_service import get_task_service
        service = get_task_service()
        async def runner(task_id,topic,resume=False):
            task_repository.update_task(task_id,status="completed")
        service.runner = runner
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url="http://test",headers=ADMIN_HEADERS) as client:
            headers = {"Idempotency-Key":"create-fixture"}
            one = await client.post("/api/v2/tasks",json={"topic":"相同请求"},headers=headers)
            database.init_db()
            two = await client.post("/api/v2/tasks",json={"topic":"相同请求"},headers=headers)
            assert one.json()["data"] == two.json()["data"]
            assert task_repository.list_tasks()[1] == 1
            conflict = await client.post("/api/v2/tasks",json={"topic":"不同请求"},headers=headers)
            assert conflict.status_code == 409
        await service.close()
    asyncio.run(scenario())


def test_resume_missing_checkpoint_is_explicit_restart():
    from main import app
    database.init_db()
    task_repository.create_task("paused","暂停")
    task_repository.update_task("paused",status="paused")
    async def scenario():
        from services.task_service import get_task_service
        service = get_task_service()
        async def runner(task_id,topic,resume=False):
            assert resume
            task_repository.update_task(task_id,status="completed")
        service.runner = runner
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url="http://test",headers=ADMIN_HEADERS) as client:
            response = await client.post("/api/v2/tasks/paused/resume",headers={"Idempotency-Key":"resume-fixture"})
            assert response.status_code == 202
            assert response.json()["data"]["resume_mode"] == "restart"
        await service.wait("paused")
        await service.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("status",["cancelled","completed"])
def test_resume_refuses_terminal_and_missing(status):
    from services.task_service import TaskService, TaskControlError
    database.init_db()
    task_repository.create_task("terminal","终态")
    task_repository.update_task("terminal",status=status)
    async def scenario():
        service = TaskService()
        with pytest.raises(TaskControlError) as exc:
            await service.resume("terminal")
        assert exc.value.status == 409
        with pytest.raises(TaskControlError) as exc:
            await service.resume("missing")
        assert exc.value.status == 404
        await service.close()
    asyncio.run(scenario())


def test_resume_repeated_concurrent_requests_create_one_execution():
    from services.task_service import TaskService
    database.init_db()
    task_repository.create_task("resume","暂停")
    task_repository.update_task("resume",status="paused")
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []
        async def runner(task_id,topic,resume=False):
            calls.append(task_id)
            entered.set()
            await release.wait()
            task_repository.finalize_task(task_id,dict(status="completed"),[{"type":"Observation","content":"唯一日志"}])
        service = TaskService(runner=runner)
        results = await asyncio.gather(*[service.resume("resume",key="same-resume") for _ in range(3)])
        assert results[0] == results[1] == results[2]
        await entered.wait()
        repeated = await service.resume("resume",key="other-key")
        assert repeated["resumed"] is False and repeated["status"] == "running"
        release.set()
        await service.wait("resume")
        await service.resume("resume",key="same-resume")
        await asyncio.sleep(0.01)
        assert calls == ["resume"] and len(task_repository.get_logs("resume")) == 1
        with database.get_connection() as conn:
            assert conn.execute("SELECT COUNT(*) FROM task_actions").fetchone()[0] == 2
        await service.close()
    asyncio.run(scenario())


def test_idempotency_cross_action_target_conflicts_and_cancel_replay():
    from services.task_service import TaskService, TaskControlError
    database.init_db()
    async def scenario():
        service = TaskService()
        task = await service.submit("主题",key="key-create")
        for method in (service.cancel, service.resume):
            with pytest.raises(TaskControlError) as exc:
                await method(task["task_id"],key="key-create")
            assert exc.value.code == "IDEMPOTENCY_CONFLICT"
        first = await service.cancel(task["task_id"],key="key-cancel")
        second = await service.cancel(task["task_id"],key="key-cancel")
        assert first == second and not first["already_cancelled"]
        task_repository.create_task("other","其他",initial_status="queued")
        with pytest.raises(TaskControlError) as exc:
            await service.cancel("other",key="key-cancel")
        assert exc.value.status == 409
        assert len(task_repository.get_logs(task["task_id"])) == 1
        await service.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("key",["", " ", "x"*129, "非ASCII"])
def test_invalid_action_keys_rejected_without_jobs(key):
    from services.task_service import TaskService, TaskControlError
    database.init_db()
    async def scenario():
        service = TaskService()
        with pytest.raises(TaskControlError) as exc:
            await service.submit("主题",key=key)
        assert exc.value.status == 422
        assert task_repository.list_tasks()[1] == 0
        await service.close()
    asyncio.run(scenario())


def test_action_record_failure_rolls_back_task_and_cancel():
    from services.task_service import TaskService, TaskControlError
    database.init_db()
    async def scenario():
        service = TaskService()
        with database.get_connection() as conn:
            conn.execute("CREATE TRIGGER reject_action BEFORE INSERT ON task_actions BEGIN SELECT RAISE(ABORT,'SECRET_KEY'); END")
        with pytest.raises(TaskControlError,match="任务创建失败"):
            await service.submit("不能留下任务",key="failed-create")
        assert task_repository.list_tasks()[1] == 0
        task_repository.create_task("queue","等待",initial_status="queued")
        with pytest.raises(TaskControlError,match="持久化失败"):
            await service.cancel("queue",key="failed-cancel")
        assert task_repository.get_task("queue")["status"] == "queued"
        assert task_repository.get_logs("queue") == []
        task_repository.update_task("queue",status="paused")
        with pytest.raises(TaskControlError,match="持久化失败"):
            await service.resume("queue",key="failed-resume")
        assert task_repository.get_task("queue")["status"] == "paused"
        await service.close()
    asyncio.run(scenario())


def test_restart_claims_queued_resume_once_and_pauses_only_running():
    from services.task_service import TaskService
    database.init_db()
    task_repository.create_task("running","旧执行")
    task_repository.create_task("cancelled","旧取消")
    task_repository.update_task("cancelled",status="cancelled")
    task_repository.create_task("queued","排队",initial_status="queued")
    task_repository.create_task("resume","恢复")
    task_repository.update_task("resume",status="paused")
    async def scenario():
        first = TaskService()
        await first.resume("resume",key="persist-resume")
        await first.close()  # 事件循环尚未派发，恢复请求仍可靠保存在queued。
        assert task_repository.mark_interrupted_tasks_as_paused() == 1
        calls = []
        async def runner(task_id,topic,resume=False):
            calls.append((task_id,resume))
            task_repository.update_task(task_id,status="completed")
        second = TaskService(runner=runner)
        second.start()
        second.start()
        await asyncio.gather(*list(second.handles.values()))
        assert sorted(calls) == [("queued",False),("resume",True)]
        assert task_repository.get_task("running")["status"] == "paused"
        assert task_repository.get_task("cancelled")["status"] == "cancelled"
        await second.close()
    asyncio.run(scenario())


def test_regeneration_key_is_persistent_and_source_kept():
    from services.task_service import TaskService
    database.init_db()
    task_repository.create_task("old","原报告")
    task_repository.update_task("old",status="completed",final_report="原文")
    async def scenario():
        service = TaskService()
        async def runner(task_id,topic,resume=False):
            task_repository.update_task(task_id,status="completed")
        service.runner = runner
        one = await service.regenerate("old",key="regen-key")
        two = await service.regenerate("old",key="regen-key")
        assert one == two and task_repository.list_tasks()[1] == 2
        assert task_repository.get_task("old")["final_report"] == "原文"
        await service.close()
    asyncio.run(scenario())


def test_corrupt_action_result_is_not_replayed_and_key_is_only_hash():
    import json
    from services.task_service import TaskService, TaskControlError
    database.init_db()
    async def scenario():
        service = TaskService()
        result = await service.submit("账本",key="fixture-private-key")
        with database.get_connection() as conn:
            row = conn.execute("SELECT * FROM task_actions").fetchone()
            assert len(row["key_digest"]) == 64 and "fixture-private-key" not in str(dict(row))
            conn.execute("UPDATE task_actions SET response=?",(json.dumps(result | {"task_id":"other"}),))
        with pytest.raises(TaskControlError,match="任务创建失败"):
            await service.submit("账本",key="fixture-private-key")
        assert task_repository.list_tasks()[1] == 1
        await service.close()
        with pytest.raises(TaskControlError):
            await service.resume(result["task_id"])
        with pytest.raises(TaskControlError):
            await service.regenerate(result["task_id"])
    asyncio.run(scenario())


@pytest.mark.parametrize("change",[{"execution_status":"running"},{"resumed":True,"resume_mode":None}])
def test_invalid_resume_acknowledgement_is_rejected(change):
    from pydantic import ValidationError
    from services.task_service import TaskResumed
    with pytest.raises(ValidationError):
        TaskResumed(**(dict(task_id="t",status="queued",execution_status="queued",resumed=False,resume_mode=None) | change))


def model_scripts(scripts):
    scripts["planner"].outputs = ['[{"id":1,"agent":"researcher","task":"搜","depends_on":[]},{"id":2,"agent":"writer","task":"写","depends_on":[1]}]']
    scripts["react_researcher"].outputs = ['{"action":"web_search","query":"资料"}','{"action":"done"}']
    scripts["react_writer"].outputs = ['# 离线恢复报告[S1]']
    scripts["reviewer"].outputs = ['{"passed":true,"comments":[]}']


@pytest.mark.parametrize("terminal",[False,True])
def test_real_checkpoint_pending_resumes_terminal_restarts(offline_evidence,terminal):
    from graph import workflow
    from memory import checkpointer
    from services.task_service import TaskService
    database.init_db()
    model_scripts(offline_evidence)
    task_repository.create_task("checkpoint","恢复主题")
    async def scenario():
        async with checkpointer.open_checkpointer() as (cp,kind):
            assert kind == "sqlite"
            graph = workflow.build_graph(checkpointer=cp,interrupt_before=None if terminal else ["scheduler"])
            partial = await graph.ainvoke(workflow._initial_state("checkpoint","恢复主题"),{"configurable":{"thread_id":"checkpoint"}})
        task_repository.update_task("checkpoint",status="failed" if terminal else "paused")
        assert checkpointer.resumable_mode("checkpoint") == ("restart" if terminal else "checkpoint")
        if terminal:
            model_scripts(offline_evidence)
        service = TaskService()
        result = await service.resume("checkpoint",key="checkpoint-resume")
        final = await service.wait("checkpoint")
        assert final["status"] == "completed" and final["quality_status"] == "passed"
        assert final["resume_mode"] == result["resume_mode"] == ("restart" if terminal else "checkpoint")
        assert len(offline_evidence["planner"].calls) == (2 if terminal else 1)
        logs = task_repository.get_logs("checkpoint")
        for log in partial["react_logs"][:2]:
            assert sum(r["content"] == log["content"] for r in logs) == 1
        before = list(logs)
        await service.resume("checkpoint",key="checkpoint-resume")
        await asyncio.sleep(0.01)
        assert task_repository.get_logs("checkpoint") == before
        await service.close()
    asyncio.run(scenario())


def test_corrupt_checkpoint_restarts_in_memory_without_false_resume(offline_evidence):
    from memory import checkpointer
    from services.task_service import TaskService
    database.init_db()
    model_scripts(offline_evidence)
    checkpointer.CHECKPOINT_DB.write_bytes(b"corrupt-checkpoint-fixture")
    task_repository.create_task("broken","损坏快照")
    task_repository.update_task("broken",status="paused")
    async def scenario():
        service = TaskService()
        result = await service.resume("broken")
        assert result["resume_mode"] == "restart"
        final = await service.wait("broken")
        assert final["status"] == "completed" and final["resume_mode"] == "restart"
        await service.close()
    asyncio.run(scenario())


def test_resume_does_not_reset_usage_budget(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from services.task_service import TaskService
    from services.usage_service import ModelUsage
    from db import usage_repository
    from llm import client
    from memory import long_term_memory
    monkeypatch.setenv("MAX_TASK_TOKENS", "20000")
    database.init_db()
    task_repository.create_task("budget","预算不重置")
    task_repository.update_task("budget",status="failed")
    usage_repository.save(ModelUsage(call_id="previous",task_id="budget",agent="writer",model="fixture",
        status="failed",duration_ms=0.0,reserved_tokens=20000,created_at="fixture"))
    create = AsyncMock()
    monkeypatch.setattr(client,"get_client",lambda:SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    monkeypatch.setattr(long_term_memory,"_MEMORY_ENABLED",False)
    async def scenario():
        service = TaskService()
        await service.resume("budget")
        final = await service.wait("budget")
        assert final["status"] == "failed" and final["error_code"] == "BUDGET_EXCEEDED"
        assert create.call_count == 0 and usage_repository.list_usage("budget")[0]["call_id"] == "previous"
        await service.close()
    asyncio.run(scenario())


def test_checkpoint_read_failure_after_acceptance_updates_actual_mode(offline_evidence,monkeypatch):
    from graph import workflow
    from memory import checkpointer
    from services.task_service import TaskService
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
    database.init_db()
    task_repository.create_task("late-read","查询失败")
    model_scripts(offline_evidence)
    async def scenario():
        async with checkpointer.open_checkpointer() as (cp,kind):
            assert kind == "sqlite"
            graph = workflow.build_graph(checkpointer=cp,interrupt_before=["scheduler"])
            await graph.ainvoke(workflow._initial_state("late-read","查询失败"),{"configurable":{"thread_id":"late-read"}})
        task_repository.update_task("late-read",status="paused")
        model_scripts(offline_evidence)
        original = AsyncSqliteSaver.aget_tuple
        reads = []
        async def fail_once(self,*args,**kwargs):
            reads.append(1)
            if len(reads) == 1:
                raise RuntimeError("SECRET_CHECKPOINT")
            return await original(self,*args,**kwargs)
        monkeypatch.setattr(AsyncSqliteSaver,"aget_tuple",fail_once)
        service = TaskService()
        accepted = await service.resume("late-read")
        assert accepted["resume_mode"] == "checkpoint"
        final = await service.wait("late-read")
        assert final["status"] == "completed" and final["resume_mode"] == "restart"
        assert len(offline_evidence["planner"].calls) == 2
        await service.close()
    asyncio.run(scenario())


def test_old_schema_migrates_twice_without_inventing_control_state(tmp_path):
    import sqlite3
    path = tmp_path / "old-control.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE tasks(task_id TEXT PRIMARY KEY,topic TEXT,status TEXT,final_report TEXT,created_at TEXT,updated_at TEXT)")
        conn.execute("INSERT INTO tasks VALUES('old','原主题','completed','原报告','旧时间','旧时间')")
    database.init_db(path)
    database.init_db(path)
    task = task_repository.get_task("old")
    assert task["final_report"] == "原报告" and task["quality_status"] == "unknown" and task["resume_mode"] is None
    assert "run_token" not in task and "resume_requested" not in task
    with database.get_connection() as conn:
        assert tuple(conn.execute("SELECT run_token,resume_requested,resume_mode FROM tasks").fetchone()) == (None,0,None)
        assert conn.execute("SELECT COUNT(*) FROM task_actions").fetchone()[0] == 0


def test_v2_action_body_validation_auth_and_cors():
    from fastapi.testclient import TestClient
    from main import app
    from tests.auth_support import READ_HEADERS
    database.init_db()
    task_repository.create_task("test","暂停")
    task_repository.update_task("test",status="paused")
    client = TestClient(app)
    for headers,status in [({},401),(READ_HEADERS,403)]:
        assert client.post("/api/v2/tasks/test/resume",headers=headers).status_code == status
    for action in ("resume","cancel","regenerate"):
        response = client.post(f"/api/v2/tasks/test/{action}",json={"extra":1},headers=ADMIN_HEADERS)
        assert response.status_code == 422 and response.json()["error"]["code"] == "INVALID_ACTION_REQUEST"
    assert task_repository.get_task("test")["status"] == "paused"
    response = client.options("/api/v2/tasks",headers={"Origin":"http://localhost:5174","Access-Control-Request-Method":"POST","Access-Control-Request-Headers":"Authorization,Idempotency-Key"})
    assert response.status_code == 200 and "Idempotency-Key" in response.headers["access-control-allow-headers"]
