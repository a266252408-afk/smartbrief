"""v2用量入口与费用、权限、损坏记录边界。"""
from fastapi.testclient import TestClient

from db import database, task_repository
from tests.auth_support import ADMIN_HEADERS, READ_HEADERS
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.fixture
def measured(monkeypatch):
    from llm import client
    database.init_db()
    task_repository.create_task("usage", "离线")
    model = AsyncMock(return_value=SimpleNamespace(usage={"prompt_tokens": 100, "completion_tokens": 50}))
    monkeypatch.setattr(client, "get_client", lambda: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=model))))
    monkeypatch.setenv("OPENAI_MODEL", "fixture-model")
    monkeypatch.delenv("MODEL_PRICES_JSON", raising=False)
    return model


def invoke():
    from llm.client import chat
    from services.usage_service import usage_scope
    @usage_scope("writer", task=True)
    async def run(task_id):
        await chat([{"role": "user", "content": "PRIVATE_PROMPT"}])
    asyncio.run(run("usage"))


def test_usage_api_empty_history_is_unknown_and_readonly_allowed():
    from main import app
    database.init_db()
    task_repository.create_task("old", "历史")
    for headers in (ADMIN_HEADERS, READ_HEADERS):
        response = TestClient(app).get("/api/v2/tasks/old/usage", headers=headers)
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["items"] == [] and data["input_tokens"] is None
        assert data["estimated_costs"] == {} and data["cost_complete"] is False


def test_price_is_explicit_and_snapshot_not_repriced(measured, monkeypatch):
    from main import app
    from services.usage_summary import task_usage
    monkeypatch.setenv("MODEL_PRICES_JSON", json.dumps({"fixture-model": dict(input_per_million=2, output_per_million=4, currency="USD")}))
    invoke()
    monkeypatch.setenv("MODEL_PRICES_JSON", json.dumps({"fixture-model": dict(input_per_million=3, output_per_million=6, currency="CNY")}))
    invoke()
    monkeypatch.delenv("MODEL_PRICES_JSON")
    database.init_db()
    report = task_usage("usage")
    assert report["estimated_costs"] == pytest.approx({"USD": 0.0004, "CNY": 0.0006})
    assert report["input_tokens"] == 200 and report["cost_complete"]
    data = TestClient(app).get("/api/v2/tasks/usage/usage", headers=READ_HEADERS).json()["data"]
    assert data == report and "PRIVATE_PROMPT" not in json.dumps(data)
    invoke()
    assert not task_usage("usage")["cost_complete"]


@pytest.mark.parametrize("known", [False, True])
def test_unknown_usage_or_absent_model_price_no_fake_fee(measured, monkeypatch, known):
    from services.usage_summary import task_usage
    monkeypatch.setenv("MODEL_PRICES_JSON", '{"other-model":{"input_per_million":2,"output_per_million":4,"currency":"USD"}}')
    if not known:
        measured.return_value = SimpleNamespace(usage=None)
    invoke()
    report = task_usage("usage")
    assert not report["cost_complete"] and report["estimated_costs"] == {}
    assert report["unknown_calls"] == int(not known)
    assert report["budget_estimated"] == (not known)


@pytest.mark.parametrize("price", ["[]", "{", '{"fixture-model":{}}',
    '{"fixture-model":{"input_per_million":true,"output_per_million":2,"currency":"USD"}}',
    '{"fixture-model":{"input_per_million":"2","output_per_million":2,"currency":"USD"}}',
    '{"fixture-model":{"input_per_million":-1,"output_per_million":2,"currency":"USD"}}'])
def test_invalid_price_configuration_fails_safely_before_call(measured, monkeypatch, price):
    from services.usage_service import UsagePersistenceError
    monkeypatch.setenv("MODEL_PRICES_JSON", price)
    with pytest.raises(UsagePersistenceError, match="单价配置无效"):
        invoke()
    assert measured.call_count == 0


def test_unknown_tokens_with_price_and_zero_actual_cost(measured, monkeypatch):
    from services.usage_summary import task_usage
    monkeypatch.setenv("MODEL_PRICES_JSON", '{"fixture-model":{"input_per_million":0,"output_per_million":0,"currency":"USD"}}')
    measured.return_value = SimpleNamespace(usage=None)
    invoke()
    assert task_usage("usage")["estimated_costs"] == {}
    measured.return_value = SimpleNamespace(usage={"prompt_tokens": 0, "completion_tokens": 0})
    invoke()
    data = task_usage("usage")
    assert data["estimated_costs"] == {"USD": 0.0} and not data["cost_complete"]


def test_usage_errors_auth_missing_task_and_corruption(measured, monkeypatch):
    from main import app
    client = TestClient(app)
    for headers in ({}, {"Authorization": "Bearer wrong"}):
        response = client.get("/api/v2/tasks/usage/usage", headers=headers)
        assert response.status_code == 401 and "request_id" in response.json()["error"]
    response = client.get("/api/v2/tasks/missing/usage", headers=ADMIN_HEADERS)
    assert response.status_code == 404 and response.json()["error"]["code"] == "TASK_NOT_FOUND"
    # 损坏记录必须错误，不能伪装零调用或漏掉失败行。
    with database.get_connection() as conn:
        conn.execute("INSERT INTO model_usage VALUES ('bad','usage','SECRET_CORRUPTION')")
    response = client.get("/api/v2/tasks/usage/usage", headers=READ_HEADERS)
    assert response.status_code == 503 and response.json()["error"]["code"] == "USAGE_UNAVAILABLE"
    assert "SECRET" not in response.text
    with database.get_connection() as conn:
        conn.execute("DELETE FROM model_usage")
    from api import usage_routes
    monkeypatch.setattr(usage_routes, "task_usage", lambda _: (_ for _ in ()).throw(RuntimeError("SECRET_DB")))
    response = client.get("/api/v2/tasks/usage/usage", headers=ADMIN_HEADERS)
    assert response.status_code == 503 and "SECRET" not in response.text
    assert client.get("/api/v1/tasks/usage/status", headers=READ_HEADERS).status_code == 200


@pytest.mark.parametrize("field", ["task_id", "call_id"])
def test_corrupt_attribution_is_rejected_not_mixed(measured, field):
    from main import app
    from db import usage_repository
    invoke()
    row = usage_repository.list_usage("usage")[0]
    row[field] = "other"
    with database.get_connection() as conn:
        conn.execute("UPDATE model_usage SET data=?", (json.dumps(row),))
    response = TestClient(app).get("/api/v2/tasks/usage/usage", headers=READ_HEADERS)
    assert response.status_code == 503 and "other" not in response.text


def test_inflight_call_is_running_and_stale_call_is_interrupted(measured):
    from services.usage_summary import task_usage
    from services.usage_service import ModelUsage
    from db import usage_repository
    async def response(**kwargs):
        during = task_usage("usage")
        assert during["items"][0]["status"] == "running"
        assert during["items"][0]["error_code"] is None
        return SimpleNamespace(usage={"prompt_tokens": 10, "completion_tokens": 5})
    measured.side_effect = response
    invoke()
    row = task_usage("usage")["items"][0]
    assert row["status"] == "success"
    row.update(status="running", input_tokens=None, output_tokens=None, usage_known=False)
    usage_repository.save(ModelUsage(**row))
    task_repository.update_task("usage", status="paused")
    stale = task_usage("usage")["items"][0]
    assert stale["status"] == "failed" and stale["error_code"] == "USAGE_INTERRUPTED"


@pytest.mark.parametrize("budget_failure", [False, True])
def test_real_generate_http_graph_persistence_usage(measured, monkeypatch, budget_failure):
    import httpx
    from agents import react_researcher
    from main import app
    from services.usage_service import usage_context
    from tests.fakes import fixture_search
    monkeypatch.setattr(react_researcher.mcp_manager, "call_tool", AsyncMock(return_value=fixture_search("离线")))
    monkeypatch.setenv("MAX_TASK_TOKENS", "1" if budget_failure else "20000")
    scripts = {"planner": ['[{"id":1,"agent":"researcher","task":"搜","depends_on":[]},{"id":2,"agent":"writer","task":"写","depends_on":[1]}]'],
               "researcher": ['{"action":"web_search","query":"离线"}', '{"action":"done"}'],
               "writer": ["## 离线报告[S1]"], "reviewer": ['{"passed":true,"comments":[]}']}
    async def response(**kwargs):
        content = scripts[usage_context.get()[1]].pop(0)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))], usage={"prompt_tokens": 10, "completion_tokens": 5})
    measured.side_effect = response
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test", headers=ADMIN_HEADERS) as client:
            generated = await client.post("/api/v1/generate", json={"topic": "离线用量集成"})
            assert generated.status_code == 200
            tid = generated.json()["task_id"]
            for _ in range(1000):
                state = (await client.get(f"/api/v1/tasks/{tid}/status")).json()
                if state["status"] in {"completed", "failed"}:
                    break
                await asyncio.sleep(0.01)
            assert state["status"] == ("failed" if budget_failure else "completed")
            response = await client.get(f"/api/v2/tasks/{tid}/usage")
            assert response.status_code == 200 and response.json()["meta"]["request_id"]
            report = response.json()["data"]
            assert report["task_id"] == tid and all(r["task_id"] == tid for r in report["items"])
            if budget_failure:
                assert report["call_count"] == 0 and report["blocked_count"] == 1
                assert state["error_code"] == "BUDGET_EXCEEDED" and measured.call_count == 0
            else:
                assert report["call_count"] == 5 and report["input_tokens"] == 50
                assert {r["agent"] for r in report["items"]} == set(scripts)
    asyncio.run(scenario())
