"""SQLite 持久化层测试（db/database.py + db/task_repository.py）。"""
import json

import pytest

from db import task_repository
from db.database import init_db


@pytest.fixture
def repo_db(tmp_path):
    """隔离 SQLite 库文件（临时目录）。"""
    init_db(tmp_path / "test.db")
    yield
    task_repository.get_connection().close()


def test_create_and_get_task(repo_db):
    task_repository.create_task("t1", "智能手表 2025 市场")
    task = task_repository.get_task("t1")
    assert task["task_id"] == "t1"
    assert task["topic"] == "智能手表 2025 市场"
    assert task["status"] == "running"
    assert task["plan"] == []
    assert task["completed_steps"] == []
    assert task["research_data"] == {}


def test_get_missing_task(repo_db):
    assert task_repository.get_task("nope") is None


def test_update_task_json_roundtrip(repo_db):
    task_repository.create_task("t1", "主题")
    plan = [{"id": 1, "agent": "researcher", "task": "搜索", "depends_on": []}]
    task_repository.update_task(
        "t1",
        status="completed",
        plan=plan,
        completed_steps=[1],
        research_data={"step_1": {"observations": []}},
        draft_content="草稿",
        final_report="报告",
    )
    task = task_repository.get_task("t1")
    assert task["status"] == "completed"
    assert task["plan"] == plan
    assert task["completed_steps"] == [1]
    assert task["research_data"] == {"step_1": {"observations": []}}
    assert task["draft_content"] == "草稿"
    assert task["final_report"] == "报告"


def test_update_task_ignores_unknown_field(repo_db):
    task_repository.create_task("t1", "主题")
    task_repository.update_task("t1", status="completed", bogus="x")
    task = task_repository.get_task("t1")
    assert task["status"] == "completed"
    assert "bogus" not in task


def test_append_and_get_logs(repo_db):
    task_repository.create_task("t1", "主题")
    task_repository.append_log("t1", 1, "Thought", "思考")
    task_repository.append_log("t1", 1, "Action", "web_search(query='x')")
    logs = task_repository.get_logs("t1")
    assert len(logs) == 2
    assert logs[0]["log_type"] == "Thought"
    assert logs[0]["step_id"] == 1
    assert logs[1]["content"] == "web_search(query='x')"
    assert "created_at" in logs[0]


def test_list_tasks_pagination_and_filter(repo_db):
    for i in range(5):
        task_repository.create_task(f"t{i}", f"主题 {i}")
    task_repository.update_task("t0", status="completed")
    task_repository.update_task("t1", status="failed")

    items, total = task_repository.list_tasks(limit=2, offset=0)
    assert total == 5
    assert len(items) == 2

    completed, total_completed = task_repository.list_tasks(status="completed")
    assert total_completed == 1
    assert completed[0]["task_id"] == "t0"

    failed, total_failed = task_repository.list_tasks(status="failed")
    assert total_failed == 1
    assert failed[0]["task_id"] == "t1"

    running, total_running = task_repository.list_tasks(status="running")
    assert total_running == 3


def test_list_tasks_keyword_and_order(repo_db):
    """报告中心：按主题模糊搜索 + 时间排序（desc/asc）。"""
    task_repository.create_task("t1", "智能手表 2026 市场")
    task_repository.create_task("t2", "便携式储能电源 北美市场")
    task_repository.create_task("t3", "智能门锁 2026 中国市场")
    task_repository.update_task("t1", status="completed")
    task_repository.update_task("t2", status="completed")
    task_repository.update_task("t3", status="completed")

    # 主题模糊搜索：命中 2 条（含“智能”）
    items, total = task_repository.list_tasks(keyword="智能")
    assert total == 2
    assert {r["task_id"] for r in items} == {"t1", "t3"}

    # 搜索 + 状态过滤可叠加
    items, total = task_repository.list_tasks(keyword="市场", status="completed")
    assert total == 3

    # 无命中返回空
    items, total = task_repository.list_tasks(keyword="不存在的主题")
    assert total == 0
    assert items == []

    # 时间排序：desc 最新在前（t3 最后创建 → 第一），asc 最久在前（t1 第一）
    desc_items, _ = task_repository.list_tasks(order="desc")
    assert desc_items[0]["task_id"] == "t3"
    asc_items, _ = task_repository.list_tasks(order="asc")
    assert asc_items[0]["task_id"] == "t1"


def test_list_tasks_empty(repo_db):
    items, total = task_repository.list_tasks()
    assert items == []
    assert total == 0


def test_delete_task_removes_task_and_logs(repo_db):
    task_repository.create_task("t1", "主题")
    task_repository.append_log("t1", 1, "Thought", "思考")
    assert task_repository.delete_task("t1") is True
    assert task_repository.get_task("t1") is None
    assert task_repository.get_logs("t1") == []


def test_delete_missing_task_returns_false(repo_db):
    assert task_repository.delete_task("nope") is False
