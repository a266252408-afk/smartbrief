"""从真实 v1 入口贯穿工作流、MCP 和 SQLite，付费边界使用固定脚本。"""
import json
import time

import pytest
from fastapi.testclient import TestClient
from tests.auth_support import ADMIN_HEADERS

from db import task_repository


def wait_for_task(client, task_id):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/tasks/{task_id}/status")
        assert response.status_code == 200
        data = response.json()
        if data["status"] in {"completed", "failed"}:
            return data
        time.sleep(0.02)
    pytest.fail("离线任务未在 10 秒内结束")


@pytest.mark.parametrize("invalid_plan", [False, True])
def test_generate_real_graph_and_persisted_api_state(offline_services, invalid_plan):
    from main import app
    scripts = offline_services
    if invalid_plan:
        scripts["planner"].outputs = ['[{"id":"bad","agent":"writer","task":"写"}]']
    else:
        scripts["planner"].outputs = [json.dumps([
            {"id": 1, "agent": "researcher", "task": "离线检索", "depends_on": []},
            {"id": 2, "agent": "writer", "task": "离线撰写", "depends_on": [1]}
        ])]
        scripts["react_researcher"].outputs = ['{"action":"web_search","query":"固定资料"}', '{"action":"done"}']
        scripts["react_writer"].outputs = ["## 离线报告\n本文仅为测试夹具。[S1]"]
        scripts["reviewer"].outputs = ['{"passed":true,"comments":[]}']
    with TestClient(app, headers=ADMIN_HEADERS) as client:
        task_repository.create_task("unrelated", "独立任务")
        response = client.post("/api/v1/generate", json={"topic": "离线集成验收"})
        assert response.status_code == 200
        task_id = response.json()["task_id"]
        data = wait_for_task(client, task_id)
        persisted = task_repository.get_task(task_id)
        assert data["status"] == persisted["status"]
        assert task_repository.get_task("unrelated")["status"] == "running"
        if invalid_plan:
            assert data["status"] == "failed"
            assert "bad" in persisted["error"]
            assert not persisted["final_report"]
        else:
            assert data["status"] == "completed"
            assert data["final_report"] == persisted["final_report"] == "## 离线报告\n本文仅为测试夹具。[S1]"
            assert len(data["plan"]) == 2
            assert persisted["research_data"]["step_1"]["observations"][0]["result"]["source"] == "fixture"
            assert any(row["log_type"] == "Action" for row in task_repository.get_logs(task_id))
        assert all(not script.outputs for script in scripts.values())
