"""评测 API 测试：/evaluation/run、/evaluation/reports(/id)、/tasks/{id}/report。"""
import pytest
from fastapi.testclient import TestClient
from tests.auth_support import ADMIN_HEADERS

from db import evaluation_repository, task_repository
from db.database import init_db
from evaluation import runner


@pytest.fixture
def client(tmp_path, monkeypatch):
    """隔离 SQLite 库文件，启动应用（评测后台执行打桩，不发真实 LLM）。"""
    monkeypatch.setattr("mcp_client.mcp_client.CONFIG_PATH", tmp_path / "mcp_config.json")
    monkeypatch.setattr("tools.builtin_tools.CONFIG_PATH", tmp_path / "tool_config.json")
    monkeypatch.setattr("db.database.DB_PATH", tmp_path / "eval_api_test.db")
    init_db(tmp_path / "eval_api_test.db")
    from main import app

    with TestClient(app, headers=ADMIN_HEADERS) as c:
        yield c


def test_trigger_evaluation_returns_eval_id(client, monkeypatch):
    """POST /evaluation/run：立即返回 eval_id + running + 用例总数（后台执行打桩）。

    用例总数动态取自 runner.case_count()（当前 TEST_CASES 仅启用第 1 个）。
    """

    def fake_start():
        evaluation_repository.create("eval_xyz", runner.case_count())
        return "eval_xyz"

    monkeypatch.setattr("evaluation.runner.start_evaluation", fake_start)
    resp = client.post("/api/v1/evaluation/run")
    assert resp.status_code == 200
    body = resp.json()
    assert body["eval_id"].startswith("eval_")
    assert body["status"] == "running"
    assert body["total_cases"] == runner.case_count()
    # 评测记录已落库（running 初始状态，前端可立即轮询）
    record = evaluation_repository.get(body["eval_id"])
    assert record["status"] == "running"
    assert record["total_cases"] == runner.case_count()


def test_trigger_evaluation_rejects_concurrent_run(client, monkeypatch):
    """评测运行中再次触发 → 400（并发保护）。"""
    monkeypatch.setattr("evaluation.runner._running", True)
    resp = client.post("/api/v1/evaluation/run")
    assert resp.status_code == 400
    assert "已在运行中" in resp.json()["detail"]


def test_list_and_get_evaluation_reports(client):
    evaluation_repository.create("eval_ab12", 10)
    evaluation_repository.finish(
        "eval_ab12",
        [{"topic": "便携储能", "success": True}],
        {"summary": {"total_cases": 10}, "targets": {}, "met": {}},
        "# 自动化评测报告",
    )
    evaluation_repository.create("eval_cd34", 10)
    evaluation_repository.set_progress("eval_cd34", 3)

    # 列表：倒序（最新的 eval_cd34 在前），不含大文本字段
    resp = client.get("/api/v1/evaluation/reports")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    assert body["items"][0]["eval_id"] == "eval_cd34"
    assert "results" not in body["items"][0]
    # 列表带指标摘要，供前端直接显示执行完成/质量通过列。
    assert "metrics" in body["items"][0]

    # 详情：执行中返回进度
    resp = client.get("/api/v1/evaluation/reports/eval_cd34")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "running"
    assert body["completed_cases"] == 3
    assert body["total_cases"] == 10

    # 详情：完成后返回结果 + 指标 + Markdown
    resp = client.get("/api/v1/evaluation/reports/eval_ab12")
    body = resp.json()
    assert body["status"] == "completed"
    assert len(body["results"]) == 1
    assert "# 自动化评测报告" in body["report_md"]


def test_delete_evaluation_report(client):
    """DELETE /evaluation/reports/{id}：已完成/失败可删，运行中 400，不存在 404。"""
    # 可删除：已完成的评测
    evaluation_repository.create("eval_del", 10)
    evaluation_repository.finish(
        "eval_del", [{"topic": "储能", "success": True}],
        {"summary": {"total_cases": 1}, "targets": {}, "met": {}}, "# 完成",
    )
    resp = client.delete("/api/v1/evaluation/reports/eval_del")
    assert resp.status_code == 200
    assert resp.json()["deleted"] is True
    assert evaluation_repository.get("eval_del") is None

    # 运行中的评测不可删除（后台任务仍会写进度，删除会造成混乱）
    evaluation_repository.create("eval_run", 10)
    resp = client.delete("/api/v1/evaluation/reports/eval_run")
    assert resp.status_code == 400
    assert "执行中" in resp.json()["detail"]
    assert evaluation_repository.get("eval_run") is not None

    # 不存在的评测 → 404
    resp = client.delete("/api/v1/evaluation/reports/eval_nope")
    assert resp.status_code == 404
    assert "评测报告不存在" in resp.json()["detail"]


def test_evaluation_report_not_found(client):
    resp = client.get("/api/v1/evaluation/reports/eval_nope")
    assert resp.status_code == 404
    assert "评测报告不存在" in resp.json()["detail"]


def test_lifespan_marks_running_evals_failed(tmp_path, monkeypatch):
    """后端启动收尾：上次进程遗留的 running 评测置为 failed（评测中心不卡进度条）。

    TestClient 会触发完整 lifespan（含拉起 MCP Server），按现有 API 测试惯例
    把 MCP / 工具配置重定向到 tmp_path，避免测试期间启动真实子进程。
    """
    monkeypatch.setattr("mcp_client.mcp_client.CONFIG_PATH", tmp_path / "mcp_config.json")
    monkeypatch.setattr("tools.builtin_tools.CONFIG_PATH", tmp_path / "tool_config.json")
    monkeypatch.setattr("db.database.DB_PATH", tmp_path / "lf_eval.db")
    init_db(tmp_path / "lf_eval.db")
    evaluation_repository.create("eval_stale", 10)  # 模拟上次进程中断遗留的评测
    from main import app

    with TestClient(app, headers=ADMIN_HEADERS):
        record = evaluation_repository.get("eval_stale")
        assert record["status"] == "failed"
        assert "服务中断" in record["error"]


def test_task_report_endpoint(client):
    """GET /tasks/{id}/report：返回 Markdown 报告 + 执行摘要（耗时 / ReAct 次数 / 记忆命中）。"""
    task_repository.create_task("t1", "便携储能 2026 北美市场")
    task_repository.update_task(
        "t1",
        status="completed",
        draft_content="# 便携储能市场报告\n市场规模 2026 年达 12 亿美元",
        final_report="# 便携储能市场报告\n市场规模 2026 年达 12 亿美元",
        memory_hits=[{"topic": "历史报告", "score": 0.7}],
    )
    task_repository.append_log("t1", 1, "Action", "web_search(市场规模)")
    task_repository.append_log("t1", 2, "Action", "web_search(增长率)")

    resp = client.get("/api/v1/tasks/t1/report")
    assert resp.status_code == 200
    body = resp.json()
    assert body["task_id"] == "t1"
    assert body["title"] == "便携储能 2026 北美市场"
    assert "市场规模" in body["content_markdown"]
    assert body["charts"] == []
    summary = body["executive_summary"]
    assert summary["react_loops"] == 2
    assert summary["memory_hit_count"] == 1
    assert isinstance(summary["total_time"], (int, float))  # created_at → updated_at


def test_task_report_not_completed_or_missing(client):
    task_repository.create_task("t_run", "执行中的任务")
    resp = client.get("/api/v1/tasks/t_run/report")
    assert resp.status_code == 404
    assert "尚未完成" in resp.json()["detail"]

    resp = client.get("/api/v1/tasks/t_nope/report")
    assert resp.status_code == 404
    assert "任务不存在" in resp.json()["detail"]