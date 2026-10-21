"""超时、重试与持久化预算的调用前拒绝行为。"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from db import database, usage_repository
from llm import client
from services.usage_service import usage_scope

real_get_client = client.get_client


@pytest.fixture
def model(monkeypatch):
    database.init_db()
    monkeypatch.setenv("MAX_TASK_TOKENS", "100")
    monkeypatch.setenv("MODEL_COMPLETION_RESERVE", "20")
    monkeypatch.setenv("MODEL_TIMEOUT_SECONDS", "0.01")
    create = AsyncMock(return_value=SimpleNamespace(usage=None))
    monkeypatch.setattr(client, "get_client", lambda: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    return create


@usage_scope("planner", task=True)
async def invoke(task_id, times=1):
    for _ in range(times):
        await client.chat([{"role": "user", "content": "测试"}])


def test_unknown_usage_consumes_reservation_before_next_call(model):
    from services.budget import BudgetExceeded
    with pytest.raises(BudgetExceeded):
        asyncio.run(invoke("limit", 10))
    assert 0 < model.call_count < 10
    rows = usage_repository.list_usage("limit")
    assert rows[-1]["status"] == "budget_exceeded"
    assert rows[0]["reserved_tokens"] > 20 and not rows[0]["usage_known"]
    count = model.call_count
    with pytest.raises(BudgetExceeded):
        asyncio.run(invoke("limit"))
    assert model.call_count == count


def test_sdk_retries_disabled_and_output_capped(model, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "fixture")
    client._client = None
    actual = real_get_client()
    assert actual.max_retries == 0
    client._client = None
    asyncio.run(invoke("cap"))
    assert model.call_args.kwargs["max_tokens"] == 20


def test_timeout_retries_are_bounded_and_recorded(model, monkeypatch):
    monkeypatch.setenv("MAX_TASK_TOKENS", "1000")
    async def slow(**kwargs):
        await asyncio.sleep(1)
    model.side_effect = slow
    with pytest.raises(Exception):
        asyncio.run(invoke("timeout"))
    rows = usage_repository.list_usage("timeout")
    assert len(rows) == model.call_count == 2
    assert [r["attempt"] for r in rows] == [1, 2]
    assert all(r["error_code"] == "MODEL_TIMEOUT" for r in rows)


@pytest.mark.parametrize("name,value", [("MAX_TASK_TOKENS", "0"), ("MODEL_MAX_RETRIES", "-1"),
    ("MODEL_TIMEOUT_SECONDS", "nan"), ("MODEL_COMPLETION_RESERVE", "bad")])
def test_invalid_config_fails_before_call(model, monkeypatch, name, value):
    from services.usage_service import UsagePersistenceError
    monkeypatch.setenv(name, value)
    with pytest.raises(UsagePersistenceError, match="配置无效"):
        asyncio.run(invoke("bad"))
    assert model.call_count == 0


@pytest.mark.parametrize("status", ["cancelled", "failed", "completed", "paused", "queued"])
def test_terminal_or_unclaimed_task_cannot_call(model, status):
    from db import task_repository
    from services.budget import TaskStopped
    task_repository.create_task("stopped", "主题")
    task_repository.update_task("stopped", status=status)
    with pytest.raises(TaskStopped):
        asyncio.run(invoke("stopped"))
    assert model.call_count == 0 and not usage_repository.list_usage("stopped")


def test_known_usage_releases_reservation_and_survives_reopen(model):
    model.return_value = SimpleNamespace(usage={"prompt_tokens": 2, "completion_tokens": 1})
    asyncio.run(invoke("known", 5))
    database.init_db()
    asyncio.run(invoke("known", 5))
    rows = usage_repository.list_usage("known")
    assert len(rows) == 10 and sum(r["input_tokens"] + r["output_tokens"] for r in rows) == 30


def test_same_task_concurrency_reserves_atomically(model, monkeypatch):
    monkeypatch.setenv("MODEL_TIMEOUT_SECONDS", "1")
    from services.budget import BudgetExceeded
    async def slow(**kwargs):
        await asyncio.sleep(0.02)
        return SimpleNamespace(usage=None)
    model.side_effect = slow
    async def scenario():
        return await asyncio.gather(invoke("same"), invoke("same"), return_exceptions=True)
    results = asyncio.run(scenario())
    assert sum(isinstance(r, BudgetExceeded) for r in results) == 1
    assert model.call_count == 1


def test_retry_success_and_nonretryable_failure(model, monkeypatch):
    import httpx
    from openai import APIConnectionError, BadRequestError
    monkeypatch.setenv("MAX_TASK_TOKENS", "1000")
    request = httpx.Request("POST", "https://example.invalid")
    model.side_effect = [APIConnectionError(request=request), SimpleNamespace(usage=None)]
    asyncio.run(invoke("retried"))
    assert [r["status"] for r in usage_repository.list_usage("retried")] == ["failed", "success"]
    model.reset_mock()
    model.side_effect = BadRequestError("SECRET", response=httpx.Response(400, request=request), body=None)
    with pytest.raises(Exception, match="模型调用失败"):
        asyncio.run(invoke("badrequest"))
    assert model.call_count == 1


def test_cancel_never_retries(model):
    model.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(invoke("cancel"))
    assert model.call_count == 1
    assert usage_repository.list_usage("cancel")[0]["status"] == "cancelled"


def test_legacy_unknown_usage_is_not_free(model):
    from services.usage_service import ModelUsage
    from services.budget import BudgetExceeded
    entry = ModelUsage(call_id="old", task_id="legacy", agent="writer", model="fixture",
                       status="failed", duration_ms=0.0, created_at="unknown")
    usage_repository.save(entry)
    with pytest.raises(BudgetExceeded):
        asyncio.run(invoke("legacy"))
    assert model.call_count == 0


@pytest.mark.parametrize("agent", ["planner", "researcher", "writer", "reviewer", "rewriter"])
def test_budget_exception_is_never_swallowed_by_agents(model, monkeypatch, agent):
    from agents import planner, react_researcher, react_writer, reviewer, rewriter
    from services.budget import BudgetExceeded
    from services.evidence import normalize_evidence
    from tests.fakes import fixture_search
    monkeypatch.setenv("MAX_TASK_TOKENS", "1")
    evidence = normalize_evidence("agents", {"step_1": {"observations": [{"result": fixture_search("测试")}]}})
    state = dict(task_id="agents", topic="主题", draft_content="## 报告[S1]", evidence=evidence, review_comments=["修改"])
    @usage_scope("test", task=True)
    async def call(task_id):
        if agent == "planner":
            await planner.planner_node(state)
        elif agent == "researcher":
            await react_researcher.react_researcher_run("搜", 1, [])
        elif agent == "writer":
            await react_writer.react_writer_run("写", 2, [], {}, evidence=evidence)
        elif agent == "reviewer":
            await reviewer.reviewer_node(state)
        else:
            await rewriter.rewriter_node(state)
    with pytest.raises(BudgetExceeded):
        asyncio.run(call("agents"))
    assert model.call_count == 0


def test_real_workflow_budget_failure_has_explicit_terminal_reason(model, monkeypatch):
    from db import task_repository
    from graph.workflow import run_task
    from services.budget import BudgetExceeded
    monkeypatch.setenv("MAX_TASK_TOKENS", "1")
    task_repository.create_task("workflow", "主题")
    with pytest.raises(BudgetExceeded):
        asyncio.run(run_task("workflow", "主题"))
    task = task_repository.get_task("workflow")
    assert task["execution_status"] == "failed" and task["quality_status"] == "not_run"
    assert task["error_code"] == "BUDGET_EXCEEDED" and not task["forced_output"]
    assert model.call_count == 0


@pytest.mark.parametrize("status,retries", [(429, 2), (500, 2), (503, 2), (401, 1)])
def test_only_transient_http_failures_retry(model, monkeypatch, status, retries):
    import httpx
    from openai import APIStatusError
    monkeypatch.setenv("MAX_TASK_TOKENS", "1000")
    response = httpx.Response(status, request=httpx.Request("POST", "https://example.invalid"))
    model.side_effect = APIStatusError("PRIVATE", response=response, body=None)
    with pytest.raises(Exception, match="模型调用失败"):
        asyncio.run(invoke("http"))
    assert model.call_count == retries
