"""LangGraph 工作流测试（graph/workflow.py）。"""
import asyncio
import json
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
        patch.object(react_researcher.mcp_manager, "call_tool", new=AsyncMock(return_value={"results":[{"title":"测试来源","snippet":"市场100亿","url":"https://example.invalid/a"}]})),
        patch.object(react_writer, "chat", new=AsyncMock(return_value=FakeResp("## 报告\n手机市场100亿[S1]"))),
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


def test_run_task_reviewer_rejects_then_rewrites(repo_db, offline_evidence):
    """A2A：Reviewer 第 1 次打回 → Rewriter 返工 → Reviewer 第 2 次通过。"""
    from graph.workflow import run_task

    from agents import planner, react_writer, reviewer, rewriter

    class FakeMessage:
        def __init__(self, content):
            self.content = content

    class FakeResp:
        def __init__(self, content):
            self.choices = [type("C", (), {"message": FakeMessage(content)})()]

    plan_json = '[{"id":1,"agent":"researcher","task":"搜资料","depends_on":[]},{"id":2,"agent":"writer","task":"写报告","depends_on":[1]}]'
    task_repository.create_task("t_a2a_1", "储能市场")
    with (
        patch.object(planner, "chat", new=AsyncMock(return_value=FakeResp(plan_json))),
        patch.object(react_writer, "chat", new=AsyncMock(return_value=FakeResp("## 初稿\n只有一句话[S1]"))),
        # Reviewer 第一次打回、第二次通过
        patch.object(
            reviewer, "chat",
            new=AsyncMock(side_effect=[
                FakeResp('{"passed": false, "comments": ["结构需重组"], "full_rewrite_required": true}'),
                FakeResp('{"passed": true, "comments": []}'),
            ]),
        ),
        patch.object(rewriter, "chat", new=AsyncMock(return_value=FakeResp(json.dumps({"mode":"full", "reason":"结构重组", "content":"## 修改稿\n包含 2 个数据点与来源[S1]", "results":[]})))),
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
    assert any("返工回执" in c for c in contents)         # 日志仅记录决策摘要，原始提案保存在审核历史
    saved_rewrite = task_repository.get_task('t_a2a_1')['review_history'][0]['rewrite']
    assert saved_rewrite['applied'] is True
    assert '修改稿' in json.loads(saved_rewrite['attempts'][0]['raw_response'])['content']


def test_run_task_reviewer_force_pass_after_max_iterations(repo_db, offline_evidence):
    """A2A：Reviewer 连续不通过 → 达上限强制输出 + 草稿标记警告。"""
    from graph.workflow import run_task

    from agents import planner, react_writer, reviewer, rewriter

    class FakeMessage:
        def __init__(self, content):
            self.content = content

    class FakeResp:
        def __init__(self, content):
            self.choices = [type("C", (), {"message": FakeMessage(content)})()]

    plan_json = '[{"id":1,"agent":"researcher","task":"搜资料","depends_on":[]},{"id":2,"agent":"writer","task":"写报告","depends_on":[1]}]'
    task_repository.create_task("t_a2a_2", "手机市场")
    with (
        patch.object(planner, "chat", new=AsyncMock(return_value=FakeResp(plan_json))),
        patch.object(react_writer, "chat", new=AsyncMock(return_value=FakeResp("## 初稿\n数据不足[S1]"))),
        # 审核持续拒绝，第二轮硬错误数不降提前停止
        patch.object(
            reviewer, "chat",
            new=AsyncMock(return_value=FakeResp('{"passed": false, "comments": ["结构仍需完善"], "full_rewrite_required": true}')),
        ),
        patch.object(rewriter, "chat", new=AsyncMock(side_effect=[FakeResp(json.dumps({"mode":"full", "reason":"结构重组", "content":text, "results":[]})) for text in ["## 改了一版\n还是不够[S1]", "## 又改一版\n仍需完善[S1]"]])),
    ):
        final = run(run_task("t_a2a_2", "手机市场"))
    assert final["passed"] is False                      # 强制输出仍未通过
    assert final["quality_status"] == "rejected"
    assert final["forced_output"] is True
    assert final["forced_pass"] is True                  # 标记强制
    assert final["iteration"] == 3                       # 无硬错误计数不能证明收敛，真实达到审核上限
    assert final["review_history"][-1]["stop_reason"] == "MAX_ITERATIONS"
    assert len(final["review_history"]) == 3             # 三轮审核全部保留
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
    """Agent 步骤执行超时 → 降级记录 + 标记完成，且保留已完成轮次的部分结果。"""
    import asyncio

    from agents import scheduler as scheduler_mod

    async def partial_then_hang(task, step_id, logs, observations=None, deadline=None):
        # 先完成一轮搜索（结果同步进调用方共享列表），再卡死触发整步超时
        if observations is not None:
            observations.append({"query": "智能手机 2026 出货量", "result": {
                "source": "bing", "results": [
                    {"title": "2026 智能手机出货报告", "snippet": "出货量数据", "url": "https://example.com/a"},
                ],
                "retrieved_at": "2026-10-26T08:00:00+00:00",
            }})
        await asyncio.sleep(10)                          # 模拟 Agent 卡死
        return {"observations": []}

    state = {
        "task_id": "t_timeout_1",
        "plan": [{"id": 1, "agent": "researcher", "task": "搜市场", "depends_on": []}],
        "completed_steps": [],
        "react_logs": [],
        "research_data": {},
    }
    with (
        patch.object(scheduler_mod, "AGENT_STEP_TIMEOUT_SECONDS", 0.05),
        patch.object(scheduler_mod, "react_researcher_run", new=partial_then_hang),
    ):
        out = run(scheduler_mod.scheduler_node(state))
    assert out["completed_steps"] == [1]                  # 跳过但仍标记完成（解锁后续依赖）
    step_result = out["research_data"]["step_1"]
    assert step_result["skipped"] is True                 # 降级标记
    assert "timeout" in step_result["error"]
    assert len(step_result["observations"]) == 1          # 已完成轮次不被丢弃
    assert out["evidence"]                                # 部分结果已注册为可追溯证据
    assert any("超时" in log["content"] for log in out["react_logs"])


def test_scheduler_writer_timeout_after_partial_research():
    """writer 前的 researcher 超时保留部分结果时，writer 仍正常派发并引用证据。"""
    from agents import scheduler as scheduler_mod

    async def partial_then_hang(task, step_id, logs, observations=None, deadline=None):
        if observations is not None:
            observations.append({"query": "q", "result": {
                "source": "bing", "retrieved_at": "2026-10-26T08:00:00+00:00",
                "results": [{"title": "报告", "snippet": "数据", "url": "https://example.com/a"}],
            }})
        await asyncio.sleep(10)

    async def writer(task, step_id, logs, research_data, evidence=None):
        return "## 章节"

    state = {
        "task_id": "t_partial_1",
        "plan": [
            {"id": 1, "agent": "researcher", "task": "搜市场", "depends_on": []},
            {"id": 2, "agent": "writer", "task": "写章节", "depends_on": [1]},
        ],
        "completed_steps": [],
        "react_logs": [],
        "research_data": {},
    }
    with (
        patch.object(scheduler_mod, "AGENT_STEP_TIMEOUT_SECONDS", 0.05),
        patch.object(scheduler_mod, "react_researcher_run", new=partial_then_hang),
        patch.object(scheduler_mod, "react_writer_run", new=writer),
    ):
        out = run(scheduler_mod.scheduler_node(state))
    assert out["research_data"]["step_1"]["skipped"] is True
    assert len(out["research_data"]["step_1"]["observations"]) == 1
    assert out["evidence"]                                # 部分结果注册为证据，writer 可引用
    assert out["draft_content"] == "## 章节"


def test_scheduler_skips_timeout_writer_step():
    """writer 步骤超时同样被跳过，章节降级为占位文本。"""
    import asyncio

    from agents import scheduler as scheduler_mod

    async def hang_writer(task, step_id, logs, research_data, evidence=None):
        await asyncio.sleep(10)
        return "## 章节"

    state = {
        "task_id": "t_timeout_2",
        "plan": [{"id": 1, "agent": "writer", "task": "写章节", "depends_on": []}],
        "completed_steps": [],
        "react_logs": [],
        # 预置有效研究数据：证据非空才会派发 writer（空证据走的是跳过撰写分支）
        "research_data": {"step_1": {"observations": [{"query": "q", "result": {
            "source": "bing", "retrieved_at": "2026-10-26T08:00:00+00:00",
            "results": [{"title": "报告", "snippet": "数据", "url": "https://example.com/a"}],
        }}]}},
    }
    with (
        patch.object(scheduler_mod, "WRITER_STEP_TIMEOUT_SECONDS", 0.05),
        patch.object(scheduler_mod, "react_writer_run", new=hang_writer),
    ):
        out = run(scheduler_mod.scheduler_node(state))
    assert out["completed_steps"] == [1]
    assert "撰写超时" in out["draft_content"]             # 占位文本兜底


def _step_budget_error():
    from services.usage_service import ModelCallError
    error = ModelCallError("模型调用超时")
    error.code = "MODEL_TIMEOUT"
    error.step_budget_limited = True
    return error


def test_scheduler_degrades_on_step_budget_model_error():
    """步预算截断的模型超时按步骤降级，不让整个图失败。"""
    from agents import scheduler as scheduler_mod

    async def budget_limited(task, step_id, logs, observations=None, deadline=None):
        if observations is not None:
            observations.append({"query": "q", "result": {
                "source": "bing", "retrieved_at": "2026-10-26T08:00:00+00:00",
                "results": [{"title": "报告", "snippet": "数据", "url": "https://example.com/a"}],
            }})
        raise _step_budget_error()

    state = {
        "task_id": "t_budget_research",
        "plan": [{"id": 1, "agent": "researcher", "task": "搜市场", "depends_on": []}],
        "completed_steps": [],
        "react_logs": [],
        "research_data": {},
    }
    with patch.object(scheduler_mod, "react_researcher_run", new=budget_limited):
        out = run(scheduler_mod.scheduler_node(state))
    assert out["completed_steps"] == [1]
    assert out["research_data"]["step_1"]["skipped"] is True
    assert len(out["research_data"]["step_1"]["observations"]) == 1
    assert any("步预算" in log["content"] for log in out["react_logs"])


def test_scheduler_degrades_on_writer_step_budget_error():
    """写作步预算截断时保留占位章节，后续审核按缺口处理。"""
    from agents import scheduler as scheduler_mod

    async def budget_limited(task, step_id, logs, research_data, evidence=None):
        raise _step_budget_error()

    state = {
        "task_id": "t_budget_writer",
        "plan": [{"id": 1, "agent": "writer", "task": "写章节", "depends_on": []}],
        "completed_steps": [],
        "react_logs": [],
        "research_data": {"step_1": {"observations": [{"query": "q", "result": {
            "source": "bing", "retrieved_at": "2026-10-26T08:00:00+00:00",
            "results": [{"title": "报告", "snippet": "数据", "url": "https://example.com/a"}],
        }}]}},
    }
    with patch.object(scheduler_mod, "react_writer_run", new=budget_limited):
        out = run(scheduler_mod.scheduler_node(state))
    assert out["completed_steps"] == [1]
    assert "撰写超时" in out["draft_content"]
    assert any("步预算" in log["content"] for log in out["react_logs"])
