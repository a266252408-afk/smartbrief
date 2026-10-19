"""任务管理 API 测试（/generate、/tasks、/tasks/{id}/status）。"""
import json
import time
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from db import task_repository
from db.database import init_db


@pytest.fixture
def client(tmp_path, monkeypatch):
    """隔离配置路径与 SQLite 库文件，启动应用。"""
    monkeypatch.setattr("mcp_client.mcp_client.CONFIG_PATH", tmp_path / "mcp_config.json")
    monkeypatch.setattr("tools.builtin_tools.CONFIG_PATH", tmp_path / "tool_config.json")
    monkeypatch.setattr("db.database.DB_PATH", tmp_path / "api_test.db")
    init_db(tmp_path / "api_test.db")
    from main import app

    with TestClient(app) as c:
        yield c


def test_generate_creates_task_and_returns_id(client):
    with patch(
        "api.routes.workflow.run_task", new=AsyncMock(return_value={"task_id": "t"})
    ):
        resp = client.post("/api/v1/generate", json={"topic": "智能手表 2025 市场"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["task_id"].startswith("task_")
    assert body["status"] == "running"
    assert body["topic"] == "智能手表 2025 市场"
    # 任务已落库（running 初始状态）
    task = task_repository.get_task(body["task_id"])
    assert task is not None
    assert task["status"] == "running"


def test_generate_validation_error(client):
    resp = client.post("/api/v1/generate", json={"topic": ""})
    assert resp.status_code == 422


def test_list_tasks_empty(client):
    resp = client.get("/api/v1/tasks")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 0
    assert body["items"] == []


def test_list_tasks_pagination_and_filter(client):
    for i in range(3):
        task_repository.create_task(f"t{i}", f"主题 {i}")
    task_repository.update_task("t0", status="completed")

    resp = client.get("/api/v1/tasks")
    body = resp.json()
    assert body["total"] == 3
    assert len(body["items"]) == 3

    resp = client.get("/api/v1/tasks", params={"status": "completed"})
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["task_id"] == "t0"

    resp = client.get("/api/v1/tasks", params={"page": 1, "page_size": 2})
    body = resp.json()
    assert body["total"] == 3
    assert len(body["items"]) == 2


def test_list_tasks_keyword_and_order(client):
    """报告中心：keyword 主题搜索 + order 时间排序（API 层）。"""
    task_repository.create_task("a1", "智能手表 2026 市场")
    task_repository.create_task("a2", "便携式储能电源 北美市场")
    task_repository.create_task("a3", "智能门锁 2026 市场")
    task_repository.update_task("a1", status="completed")
    task_repository.update_task("a2", status="completed")
    task_repository.update_task("a3", status="completed")

    # 主题搜索：命中 2 条“智能”主题
    resp = client.get("/api/v1/tasks", params={"keyword": "智能"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    assert {item["task_id"] for item in body["items"]} == {"a1", "a3"}

    # 非法排序值 → 422
    resp = client.get("/api/v1/tasks", params={"order": "up"})
    assert resp.status_code == 422

    # 时间排序：desc 最新在前 / asc 最久在前（同一秒内按 task_id 决胜）
    resp = client.get("/api/v1/tasks", params={"order": "desc"})
    assert resp.json()["items"][0]["task_id"] == "a3"
    resp = client.get("/api/v1/tasks", params={"order": "asc"})
    assert resp.json()["items"][0]["task_id"] == "a1"


def test_task_status_returns_logs(client):
    task_repository.create_task("t1", "手机市场")
    task_repository.update_task("t1", status="completed", plan=[{"id": 1, "agent": "writer", "task": "写", "depends_on": []}])
    task_repository.append_log("t1", 1, "Thought", "开始撰写")
    task_repository.append_log("t1", 1, "Action", "llm_gen()")

    resp = client.get("/api/v1/tasks/t1/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["task_id"] == "t1"
    assert body["status"] == "completed"
    assert len(body["react_logs"]) == 2
    assert body["react_logs"][0]["log_type"] == "Thought"


def test_task_status_not_found(client):
    resp = client.get("/api/v1/tasks/nope/status")
    assert resp.status_code == 404
    assert "任务不存在" in resp.json()["detail"]


def test_delete_task(client):
    task_repository.create_task("t1", "主题")
    task_repository.append_log("t1", 1, "Thought", "思考")

    resp = client.delete("/api/v1/tasks/t1")
    assert resp.status_code == 200
    body = resp.json()
    assert body["task_id"] == "t1"
    assert body["deleted"] is True
    # 任务及其 ReAct 日志均被删除
    assert task_repository.get_task("t1") is None
    assert task_repository.get_logs("t1") == []


def test_delete_task_not_found(client):
    resp = client.delete("/api/v1/tasks/nope")
    assert resp.status_code == 404
    assert "任务不存在" in resp.json()["detail"]
