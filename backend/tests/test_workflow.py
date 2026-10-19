"""LangGraph 工作流测试（graph/workflow.py）。"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from db import task_repository
from db.database import init_db


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def repo_db(tmp_path):
    init_db(tmp_path / "wf_test.db")
    yield
    task_repository.get_connection().close()


def test_build_graph_linear():
    from graph.workflow import build_graph

    graph = build_graph()
    # 编译成功即代表节点与边关系合法
    assert graph is not None


def test_run_task_success(repo_db):
    from graph.workflow import run_task

    task_id = "t_flow_1"
    task_repository.create_task(task_id, "主题")
    with patch(
        "graph.workflow.build_graph",
        new=MagicMock(
            return_value=MagicMock(
                ainvoke=AsyncMock(
                    return_value={
                        "task_id": task_id,
                        "topic": "主题",
                        "plan": [{"id": 1, "agent": "writer", "task": "写", "depends_on": []}],
                        "current_step_index": 1,
                        "completed_steps": [1],
                        "react_logs": [{"type": "Thought", "content": "ok", "step_id": 1}],
                        "research_data": {},
                        "draft_content": "## 报告",
                        "final_report": None,
                        "error": None,
                    }
                )
            )
        ),
    ):
        final = run(run_task(task_id, "主题"))
    assert final["draft_content"] == "## 报告"
    task = task_repository.get_task(task_id)
    assert task["status"] == "completed"
    assert task["completed_steps"] == [1]
    assert task["final_report"] == "## 报告"


def test_run_task_failure_sets_status(repo_db):
    from graph.workflow import run_task

    task_id = "t_flow_fail"
    task_repository.create_task(task_id, "主题")
    with patch(
        "graph.workflow.build_graph",
        new=MagicMock(
            return_value=MagicMock(ainvoke=AsyncMock(side_effect=RuntimeError("工作流挂了")))
        ),
    ):
        with pytest.raises(RuntimeError):
            run(run_task(task_id, "主题"))
    task = task_repository.get_task(task_id)
    assert task["status"] == "failed"
    assert "工作流挂了" in task["error"]


def test_run_task_full_flow_with_real_graph(repo_db):
    """端到端：真实 build_graph + 全部 mock 底层 LLM/MCP，验证状态贯通。"""
    from graph.workflow import run_task

    from agents import planner, react_researcher, react_writer, reviewer, rewriter

    class FakeMessage:
        def __init__(self, content):
            self.content = content

    class FakeResp:
        def __init__(self, content):
            self.choices = [type("C", (), {"message": FakeMessage(content)})()]

    plan_json = '[{"id": 1, "agent": "researcher", "task": "搜手机市场", "depends_on": []}, {"id": 2, "agent": "writer", "task": "写摘要", "depends_on": [1]}]'

    task_repository.create_task("t_e2e", "手机市场")
    with (
        patch.object(planner, "chat", new=AsyncMock(return_value=FakeResp(plan_json))),
        patch.object(react_researcher, "chat", new=AsyncMock(return_value=FakeResp('{"action": "web_search", "query": "手机"}'))),
        patch.object(react_researcher.mcp_manager, "call_tool", new=AsyncMock(return_value={"summary": "市场100亿"})),
        patch.object(react_writer, "chat", new=AsyncMock(return_value=FakeResp("## 报告\n手机市场100亿"))),
        # Reviewer 一次通过，不触发 Rewriter
        patch.object(reviewer, "chat", new=AsyncMock(return_value=FakeResp('{"passed": true, "comments": []}'))),
        patch.object(rewriter, "chat", new=AsyncMock(return_value=FakeResp("## 修改稿"))),
    ):
        final = run(run_task("t_e2e", "手机市场"))
    assert final["completed_steps"] == [1, 2]
    assert "手机市场100亿" in final["draft_content"]
    assert final["passed"] is True
    assert final["iteration"] == 0
    task = task_repository.get_task("t_e2e")
    assert task["status"] == "completed"
    logs = task_repository.get_logs("t_e2e")
    assert any(log["log_type"] == "Thought" for log in logs)
    assert any(log["log_type"] == "Action" for log in logs)
    assert any(log["log_type"] == "Observation" for log in logs)


# ---------------------------------------------------------------- A2A 条件循环


def test_run_task_reviewer_rejects_then_rewrites(repo_db):
    """A2A：Reviewer 第 1 次打回 → Rewriter 返工 → Reviewer 第 2 次通过。"""
    from graph.workflow import run_task

    from agents import planner, react_writer, reviewer, rewriter

    class FakeMessage:
        def __init__(self, content):
            self.content = content

    class FakeResp:
        def __init__(self, content):
            self.choices = [type("C", (), {"message": FakeMessage(content)})()]

    plan_json = '[{"id": 1, "agent": "writer", "task": "写报告", "depends_on": []}]'
    task_repository.create_task("t_a2a_1", "储能市场")
    with (
        patch.object(planner, "chat", new=AsyncMock(return_value=FakeResp(plan_json))),
        patch.object(react_writer, "chat", new=AsyncMock(return_value=FakeResp("## 初稿\n只有一句话"))),
        # Reviewer 第一次打回、第二次通过
        patch.object(
            reviewer, "chat",
            new=AsyncMock(side_effect=[
                FakeResp('{"passed": false, "comments": ["缺少数据", "字数不足"]}'),
                FakeResp('{"passed": true, "comments": []}'),
            ]),
        ),
        patch.object(rewriter, "chat", new=AsyncMock(return_value=FakeResp("## 修改稿\n包含 2 个数据点与来源"))),
    ):
        final = run(run_task("t_a2a_1", "储能市场"))
    assert final["passed"] is True
    assert final["iteration"] == 1                       # 只打回一次
    assert final["forced_pass"] is False
    assert final["review_comments"] == []                # 最后一次审核无意见
    assert "修改稿" in final["draft_content"]            # 草稿被 Rewriter 覆盖
    logs = task_repository.get_logs("t_a2a_1")
    contents = [log["content"] for log in logs]
    assert any("审核不通过" in c for c in contents)       # 打回日志存在
    assert any("修改稿" in c for c in contents)           # 返工日志存在


def test_run_task_reviewer_force_pass_after_max_iterations(repo_db):
    """A2A：Reviewer 连续不通过 → 达上限强制通过 + 草稿标记警告。"""
    from graph.workflow import run_task

    from agents import planner, react_writer, reviewer, rewriter

    class FakeMessage:
        def __init__(self, content):
            self.content = content

    class FakeResp:
        def __init__(self, content):
            self.choices = [type("C", (), {"message": FakeMessage(content)})()]

    plan_json = '[{"id": 1, "agent": "writer", "task": "写报告", "depends_on": []}]'
    task_repository.create_task("t_a2a_2", "手机市场")
    with (
        patch.object(planner, "chat", new=AsyncMock(return_value=FakeResp(plan_json))),
        patch.object(react_writer, "chat", new=AsyncMock(return_value=FakeResp("## 初稿\n数据不足"))),
        # 三次审核都不通过（第 3 次触发强制通过，上限 3）
        patch.object(
            reviewer, "chat",
            new=AsyncMock(return_value=FakeResp('{"passed": false, "comments": ["始终缺数据"]}')),
        ),
        patch.object(rewriter, "chat", new=AsyncMock(return_value=FakeResp("## 改了一版\n还是不够"))),
    ):
        final = run(run_task("t_a2a_2", "手机市场"))
    assert final["passed"] is True                       # 强制通过
    assert final["forced_pass"] is True                  # 标记强制
    assert final["iteration"] == 3                       # 循环 3 次后退出
    assert len(final["review_history"]) == 3             # 三轮审核记录按轮保留
    assert "⚠️" in final["draft_content"]                # 草稿末尾带警告
    task = task_repository.get_task("t_a2a_2")
    assert task["forced_pass"] == 1
    assert task["iteration"] == 3
    assert task["max_iterations"] == 3               # 循环上限随任务落库（status 接口透传给前端）
    assert len(task["review_history"]) == 3              # 落库后仍可读回各轮意见


def test_route_after_review():
    """条件边路由函数：passed=True → passed；否则 → rewrite。"""
    from graph.workflow import route_after_review

    assert route_after_review({"passed": True}) == "passed"
    assert route_after_review({"passed": False}) == "rewrite"
    assert route_after_review({}) == "rewrite"           # 未审核（无字段）→ 打回路径兜底


# ---------------------------------------------------------------- 容错：单 Agent 超时跳过


def test_scheduler_skips_timeout_step():
    """Agent 步骤执行超时 → 跳过（降级记录 + 标记完成），不卡死工作流。"""
    import asyncio

    from agents import scheduler as scheduler_mod

    async def hang(task, step_id, logs):
        await asyncio.sleep(10)                          # 模拟 Agent 卡死
        return {"observations": []}

    state = {
        "plan": [{"id": 1, "agent": "researcher", "task": "搜市场", "depends_on": []}],
        "completed_steps": [],
        "react_logs": [],
        "research_data": {},
    }
    with (
        patch.object(scheduler_mod, "AGENT_STEP_TIMEOUT_SECONDS", 0.05),
        patch.object(scheduler_mod, "react_researcher_run", new=hang),
    ):
        out = run(scheduler_mod.scheduler_node(state))
    assert out["completed_steps"] == [1]                  # 跳过但仍标记完成（解锁后续依赖）
    assert out["research_data"]["step_1"]["skipped"] is True   # 降级标记
    assert "timeout" in out["research_data"]["step_1"]["error"]
    assert any("超时" in log["content"] for log in out["react_logs"])


def test_scheduler_skips_timeout_writer_step():
    """writer 步骤超时同样被跳过，章节降级为占位文本。"""
    import asyncio

    from agents import scheduler as scheduler_mod

    async def hang_writer(task, step_id, logs, research_data):
        await asyncio.sleep(10)
        return "## 章节"

    state = {
        "plan": [{"id": 1, "agent": "writer", "task": "写章节", "depends_on": []}],
        "completed_steps": [],
        "react_logs": [],
        "research_data": {},
    }
    with (
        patch.object(scheduler_mod, "WRITER_STEP_TIMEOUT_SECONDS", 0.05),
        patch.object(scheduler_mod, "react_writer_run", new=hang_writer),
    ):
        out = run(scheduler_mod.scheduler_node(state))
    assert out["completed_steps"] == [1]
    assert "撰写超时" in out["draft_content"]             # 占位文本兜底
