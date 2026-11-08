"""真实模型封装→服务→SQLite的离线行为验证。"""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from db import database, usage_repository
from llm import client
from services.usage_service import usage_context, usage_scope


@pytest.fixture
def setup_usage(monkeypatch):
    database.init_db()
    create = AsyncMock(return_value=SimpleNamespace(usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5)))
    monkeypatch.setattr(client, "get_client", lambda: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    return create


def test_real_client_persists_and_isolates_concurrent_tasks(setup_usage):
    @usage_scope("planner", task=True)
    async def invoke(task_id):
        await asyncio.sleep(0)
        await client.chat([{"role": "user", "content": "PRIVATE_PROMPT"}])
    async def scenario():
        await asyncio.gather(invoke("one"), invoke("two"))
    asyncio.run(scenario())
    for tid in ("one", "two"):
        rows = usage_repository.list_usage(tid)
        assert len(rows) == 1
        row = rows[0]
        assert row["task_id"] == tid and row["agent"] == "planner"
        assert row["status"] == "success" and row["duration_ms"] >= 0
        assert row["input_tokens"] == 10 and row["output_tokens"] == 5
        assert row["usage_known"] and row["estimated_cost"] is None
        assert "PRIVATE_PROMPT" not in str(row)
    assert usage_context.get() is None


@pytest.mark.parametrize("usage", [None, {}, {"prompt_tokens": 1}, {"prompt_tokens": -1, "completion_tokens": 0}, {"prompt_tokens": True, "completion_tokens": 3}])
def test_missing_or_invalid_usage_stays_unknown(setup_usage, usage):
    setup_usage.return_value = SimpleNamespace(usage=usage)
    @usage_scope("writer", task=True)
    async def invoke(task_id):
        await client.chat([])
    asyncio.run(invoke("unknown"))
    row = usage_repository.list_usage("unknown")[0]
    assert row["input_tokens"] is None and row["output_tokens"] is None
    assert row["usage_known"] is False and row["estimated_cost"] is None


def test_failed_call_has_safe_persisted_result(setup_usage):
    setup_usage.side_effect = RuntimeError("SECRET_KEY PRIVATE_PROMPT")
    @usage_scope("reviewer", task=True)
    async def invoke(task_id):
        await client.chat([])
    with pytest.raises(Exception):
        asyncio.run(invoke("failure"))
    rows = usage_repository.list_usage("failure")
    assert len(rows) == 1 and rows[0]["status"] == "failed"
    assert rows[0]["error_code"] == "MODEL_FAILED"
    assert "SECRET_KEY" not in str(rows) and "PRIVATE_PROMPT" not in str(rows)


def test_cancellation_is_recorded_and_context_restored(setup_usage):
    setup_usage.side_effect = asyncio.CancelledError()
    @usage_scope("writer", task=True)
    async def invoke(task_id):
        await client.chat([])
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(invoke("cancel"))
    row = usage_repository.list_usage("cancel")[0]
    assert row["status"] == "cancelled" and row["error_code"] == "MODEL_CANCELLED"
    assert usage_context.get() is None


@pytest.mark.parametrize("change", [
    {"usage_known": True}, {"input_tokens": 0},
    {"currency": "USD"}, {"estimated_cost": 0.0, "currency": "USD"},
    {"input_tokens": -1}, {"attempt": 0}, {"duration_ms": float("nan")},
    {"prompt": "PRIVATE"},
])
def test_contract_rejects_fake_usage(change):
    from pydantic import ValidationError
    from services.usage_service import ModelUsage
    with pytest.raises(ValidationError):
        ModelUsage(**(dict(call_id="c", task_id="t", agent="a", model="m", status="success",
                          duration_ms=0.0, created_at="2026-10-21T00:00:00Z") | change))


@pytest.mark.parametrize("failure_at", [1, 2])
def test_persistence_failure_blocks_or_fails_call(setup_usage, monkeypatch, failure_at):
    from services.usage_service import UsagePersistenceError
    original = usage_repository.save
    original_reserve = usage_repository.reserve
    writes = []
    def check():
        writes.append(1)
        if len(writes) == failure_at:
            raise RuntimeError("SECRET_DATABASE_PATH")
    def failing(entry):
        check()
        original(entry)
    def failing_reserve(entry, limit):
        check()
        return original_reserve(entry, limit)
    monkeypatch.setattr(usage_repository, "save", failing)
    monkeypatch.setattr(usage_repository, "reserve", failing_reserve)
    @usage_scope("planner", task=True)
    async def invoke(task_id):
        await client.chat([])
    with pytest.raises(UsagePersistenceError, match="持久化失败|账本不可用"):
        asyncio.run(invoke("write_failure"))
    assert setup_usage.call_count == failure_at - 1
    if failure_at == 2:
        row = usage_repository.list_usage("write_failure")[0]
        assert row["status"] == "running" and row["error_code"] is None and not row["usage_known"]


def test_migration_idempotent_preserves_old_reports(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE tasks (task_id TEXT PRIMARY KEY,topic TEXT,status TEXT,final_report TEXT)")
        conn.execute("INSERT INTO tasks VALUES ('old','主题','completed','原报告')")
    database.init_db(path)
    database.init_db(path)
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT final_report FROM tasks").fetchone()[0] == "原报告"
    assert usage_repository.list_usage("old") == []


@pytest.mark.parametrize("fail_writer", [False, True])
def test_real_graph_agent_attribution_and_failure(setup_usage, monkeypatch, fail_writer):
    from agents import react_researcher
    from db import task_repository
    from graph.workflow import run_task
    from memory import long_term_memory
    from tests.fakes import fixture_search
    monkeypatch.setattr(long_term_memory, "_MEMORY_ENABLED", False)
    monkeypatch.setattr(react_researcher.mcp_manager, "call_tool", AsyncMock(return_value=fixture_search("测试")))
    script = {
        "planner": ['[{"id":1,"agent":"researcher","task":"搜","depends_on":[]},{"id":2,"agent":"writer","task":"写","depends_on":[1]}]'],
        "researcher": ['{"action":"web_search","query":"测试"}', '{"action":"done"}'],
        "writer": ["## 离线报告[S1]"],
        "reviewer": ['{"passed":false,"comments":["结构需重组"],"full_rewrite_required":true}', '{"passed":true,"comments":[]}'],
        "rewriter": [json.dumps({'mode':'full','reason':'统一结构','content':'## 离线修订报告[S1]','results':[]})],
    }
    async def response(**kwargs):
        agent = usage_context.get()[1]
        if agent == "writer" and fail_writer:
            raise RuntimeError("PRIVATE_PROMPT SECRET_KEY")
        content = script[agent].pop(0)
        return SimpleNamespace(usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
                               choices=[SimpleNamespace(message=SimpleNamespace(content=content))])
    setup_usage.side_effect = response
    task_repository.create_task("graph", "离线")
    if fail_writer:
        with pytest.raises(RuntimeError):
            asyncio.run(run_task("graph", "离线"))
        assert task_repository.get_task("graph")["execution_status"] == "failed"
        assert "SECRET_KEY" not in str(task_repository.get_logs("graph"))
    else:
        asyncio.run(run_task("graph", "离线"))
        assert task_repository.get_task("graph")["quality_status"] == "passed"
    rows = usage_repository.list_usage("graph")
    assert {r["agent"] for r in rows} == ({"planner", "researcher", "writer"} if fail_writer else set(script))
    assert len({r["call_id"] for r in rows}) == len(rows)
    assert not usage_repository.list_usage("unrelated")


def test_standalone_chat_has_own_ledger(setup_usage, monkeypatch):
    from agents import single_agent
    monkeypatch.setattr(single_agent, "_enabled_schemas", AsyncMock(return_value=[]))
    setup_usage.return_value = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
        content="离线答复", tool_calls=None))], usage={"prompt_tokens": 0, "completion_tokens": 0})
    asyncio.run(single_agent.run("问候"))
    with database.get_connection() as conn:
        tid = conn.execute("SELECT task_id FROM model_usage").fetchone()[0]
    assert tid.startswith("chat_")
    row = usage_repository.list_usage(tid)[0]
    assert row["agent"] == "single_agent" and row["usage_known"] and row["input_tokens"] == 0
