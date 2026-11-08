"""FLOW-01：针对实际任务失败链的离线行为复现与跨层回归。"""
import asyncio
import json
import time
from unittest.mock import AsyncMock

import pytest

from agents import scheduler, react_researcher
from db import database, task_repository
from services.citations import check_citations
from services.report_quality import DATA_GAP_MARKER, data_gap_section
from tests.test_evidence import source
from mcp_servers import web_search_server as search_server


def search_result(url, text):
    return {"source": "fixture", "results": [
        {"title": "智能手表测试资料", "snippet": text, "url": url}
    ], "retrieved_at": "2026-10-26T08:00:00+00:00"}


def test_scheduler_keeps_topic_in_general_research_and_writing(monkeypatch):
    # 有可追溯证据才会派发 writer；零证据场景由单独用例覆盖。
    researcher = AsyncMock(return_value={"observations": [
        {"query": "智能手表品牌", "result": search_result("https://example.invalid/brand", "品牌数据")}
    ]})
    writer = AsyncMock(return_value="智能手表品牌数据[S1]")
    monkeypatch.setattr(scheduler, "react_researcher_run", researcher)
    monkeypatch.setattr(scheduler, "react_writer_run", writer)
    asyncio.run(scheduler.scheduler_node({
        "task_id": "t", "topic": "2026年智能手表的市场分析调研",
        "plan": [
            {"id": 1, "agent": "researcher", "task": "调研头部品牌产品矩阵", "depends_on": []},
            {"id": 2, "agent": "writer", "task": "撰写竞争格局", "depends_on": [1]},
        ],
    }))
    assert "智能手表" in researcher.call_args.args[0]
    assert "deadline" in researcher.call_args.kwargs
    assert "智能手表" in writer.call_args.args[0]


@pytest.mark.parametrize("sentence", [
    "为2026年智能手表产品定位与营销触达提供依据。",
    "本部分对2026年智能手表赛道进行结构化拆解。",
    "## 2026年市场分析\n\n1. 建议继续研究。",
])
def test_year_in_scope_description_does_not_require_numeric_citation(sentence):
    assert check_citations("t", "声明[S1]。\n\n" + sentence, [source()])["valid"]


@pytest.mark.parametrize("sentence", ["2026年增长20%。", "出货量500万台。", "价格20260元。", "出货量2026。"])
def test_real_numeric_claim_still_rejected_with_location(sentence):
    check = check_citations("t", "声明[S1]。\n\n" + sentence, [source()])
    assert not check["valid"]
    assert any(sentence in error and "段落" in error for error in check["errors"])


class HtmlResponse:
    def __init__(self, title, snippet):
        self.html = (f'<li class="b_algo"><h2><a href="https://example.invalid/report">{title}</a>'
                     f'</h2><p>{snippet}</p></li>').encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def read(self):
        return self.html


def test_search_retries_irrelevant_endpoint_and_preserves_diagnostics(monkeypatch):
    calls = []

    def fetch(request, **kwargs):
        calls.append(request.full_url)
        return HtmlResponse("2026年日历", "节假日放假安排") if len(calls) == 1 else HtmlResponse(
            "智能手表出货量", "智能手表市场测试资料，非真实数据")

    monkeypatch.setattr(search_server.urllib.request, "urlopen", fetch)
    result = search_server._web_search("2026年全球智能手表市场规模 出货量预测")
    assert len(calls) == 2
    assert result["results"][0]["title"] == "智能手表出货量"
    assert result["search_endpoint"] == search_server.SEARCH_ENDPOINTS[1]
    assert result["attempts"][0]["rejected_count"] == 1


def test_all_irrelevant_search_hits_are_not_registered_as_evidence(monkeypatch):
    monkeypatch.setattr(search_server.urllib.request, "urlopen", lambda *a, **k: HtmlResponse(
        "2026年日历", "节假日放假安排"))
    result = search_server._web_search("2026年智能手表市场 出货量")
    assert result["results"] == []
    assert result["error_code"] == "SEARCH_IRRELEVANT"
    assert len(result["attempts"]) == 2
    # 诊断保留被筛除的标题样本，便于区分“无结果”和“端点按首词降级”。
    assert "2026年日历" in result["attempts"][0]["rejected_titles"][0]


def configure_repair(offline_evidence, monkeypatch, *, pass_second=True):
    scripts = offline_evidence
    scripts["planner"].outputs = [json.dumps([
        {"id": 1, "agent": "researcher", "task": "调研头部品牌", "depends_on": []},
        {"id": 2, "agent": "writer", "task": "撰写报告", "depends_on": [1]},
    ])]
    scripts["react_researcher"].outputs = [
        '{"action":"web_search","query":"首次搜索"}', '{"action":"done"}',
        '{"action":"web_search","query":"智能手表出货量"}', '{"action":"done"}',
    ]
    scripts["react_writer"].outputs = ["原资料说明[S1]。\n\n智能手表数据待补充。"]
    scripts["reviewer"].outputs = [
        '{"passed":false,"comments":["缺乏行业数据，请补充具体数据点及相关来源"]}',
        '{"passed":true,"comments":[]}' if pass_second else '{"passed":false,"comments":["仍缺数据"]}',
        '{"passed":false,"comments":["仍缺数据"]}',
    ]
    scripts["rewriter"].outputs = ["保留原资料说明[S1]。\n\n测试出货量500万台[S2]。"] * 2
    search = AsyncMock(side_effect=[
        search_result("https://example.invalid/old", "原资料未包含行业数据"),
        search_result("https://example.invalid/new", "测试出货量500万台"),
    ])
    monkeypatch.setattr(react_researcher.mcp_manager, "call_tool", search)
    return scripts, search


def test_real_graph_researches_missing_evidence_before_rewrite(tmp_path, offline_evidence, monkeypatch):
    from graph.workflow import run_task
    scripts, search = configure_repair(offline_evidence, monkeypatch)
    database.init_db(tmp_path / "repair.db")
    task_repository.create_task("t", "2026年智能手表市场")
    final = asyncio.run(run_task("t", "2026年智能手表市场"))
    task = task_repository.get_task("t")
    assert final["research_revision_count"] == 1
    assert task["execution_status"] == "completed" and task["quality_status"] == "passed"
    assert [e["source_id"] for e in task["evidence"]] == ["S1", "S2"]
    assert search.await_count == 2
    assert "智能手表" in scripts["react_researcher"].calls[2][0][1]["content"]
    assert "补充具体数据点" in scripts["react_researcher"].calls[2][0][1]["content"]
    assert "S2" in scripts["rewriter"].calls[0][0][1]["content"]
    logs = task_repository.get_logs("t")
    assert any("补充研究" in item["content"] for item in logs)
    assert all("T" in item["created_at"] for item in logs)


def test_research_repair_is_bounded_across_checkpoint_resume(tmp_path, offline_evidence, monkeypatch):
    from graph.workflow import build_graph, _initial_state
    from langgraph.checkpoint.memory import MemorySaver
    scripts, search = configure_repair(offline_evidence, monkeypatch, pass_second=False)

    async def scenario():
        cp = MemorySaver()
        config = {"configurable": {"thread_id": "t"}}
        paused = build_graph(checkpointer=cp, interrupt_before=["rewriter"])
        await paused.ainvoke(_initial_state("t", "智能手表市场"), config)
        snapshot = await paused.aget_state(config)
        assert snapshot.values["research_revision_count"] == 1
        assert snapshot.next == ("rewriter",)
        final = await build_graph(checkpointer=cp).ainvoke(None, config)
        assert final["quality_status"] == "rejected" and final["forced_output"]
        assert len(final["review_history"]) == 2
        assert final["review_history"][-1]["stop_reason"] == "REVIEW_NO_ACTIONABLE_FINDINGS"
        assert final["research_revision_count"] == 1

    asyncio.run(scenario())
    assert search.await_count == 2
    assert len(scripts["rewriter"].calls) == 1


def test_log_finalization_preserves_event_times_and_legacy_logs(tmp_path):
    database.init_db(tmp_path / "times.db")
    task_repository.create_task("t", "主题")
    times = ["2026-10-26T16:38:27.100+08:00", "2026-10-26T16:40:28.200+08:00"]
    logs = [{"type": "Thought", "content": str(i), "step_id": 1, "created_at": stamp}
            for i, stamp in enumerate(times)]
    logs.append({"type": "Observation", "content": "旧格式", "step_id": 1})
    task_repository.finalize_task("t", {"status": "completed", "quality_status": "rejected"}, logs)
    persisted = task_repository.get_logs("t")
    assert [item["created_at"] for item in persisted[:2]] == times
    assert persisted[2]["created_at"]


def test_timeout_step_log_distinguishes_degraded_end(monkeypatch):
    monkeypatch.setattr(scheduler, "react_researcher_run", AsyncMock(side_effect=TimeoutError))
    result = asyncio.run(scheduler.scheduler_node({
        "task_id": "t", "topic": "智能手表", "plan": [
            {"id": 1, "agent": "researcher", "task": "搜索", "depends_on": []}],
    }))
    assert result["completed_steps"] == [1]
    assert result["research_data"]["step_1"]["skipped"]
    assert not any(item["content"] == "步骤 1 完成" for item in result["react_logs"])


def test_supplemental_search_unavailable_preserves_rejected_output(tmp_path, offline_services, monkeypatch):
    from graph.workflow import run_task
    scripts = offline_services
    scripts["planner"].outputs = ['[{"id":1,"agent":"writer","task":"写报告","depends_on":[]}]']
    scripts["react_writer"].outputs = ["行业数据待补充。"]
    scripts["react_researcher"].outputs = ['{"action":"web_search","query":"智能手表"}']
    scripts["rewriter"].outputs = ["仍无来源，数据待补充。"] * 2
    scripts["reviewer"].outputs = [
        '{"passed":false,"comments":["缺乏数据，请补充具体数据点及来源"]}'
    ] * 3
    search = AsyncMock(side_effect=OSError("离线测试故障"))
    monkeypatch.setattr(react_researcher.mcp_manager, "call_tool", search)
    database.init_db(tmp_path / "unavailable.db")
    task_repository.create_task("t", "智能手表")
    final = asyncio.run(run_task("t", "智能手表"))
    task = task_repository.get_task("t")
    assert final["research_revision_count"] == 1
    assert task["execution_status"] == "completed" and task["quality_status"] == "rejected"
    assert task["forced_output"] and not task["evidence"]
    assert search.await_count == 1
    assert len(task["review_history"]) == 2
    assert task["review_history"][-1]["stop_reason"] == "REVIEW_NO_ACTIONABLE_FINDINGS"
    assert len(scripts["reviewer"].calls) == 2


def test_supplemental_timeout_retains_partial_evidence_and_cancellation_propagates(monkeypatch):
    from agents import research_revision

    async def partial_then_wait(task, step_id, logs, observations=None, deadline=None):
        observations.append({"query": "手表", "result": search_result(
            "https://example.invalid/new", "测试数据500万台")})
        await asyncio.Event().wait()

    monkeypatch.setattr(research_revision, "react_researcher_run", partial_then_wait)
    monkeypatch.setattr(research_revision, "AGENT_STEP_TIMEOUT_SECONDS", 0.01)
    state = {"task_id": "t", "topic": "智能手表", "evidence": [source()], "research_data": {}}
    result = asyncio.run(research_revision.research_revision_node(state))
    assert result["research_revision_count"] == 1
    assert [e["source_id"] for e in result["evidence"]] == ["S1", "S2"]
    assert result["research_data"]["review_research_1"]["skipped"]

    async def scenario():
        monkeypatch.setattr(research_revision, "AGENT_STEP_TIMEOUT_SECONDS", 90)
        task = asyncio.create_task(research_revision.research_revision_node(state))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())


def test_researcher_reformulates_irrelevant_query_and_records_endpoint(offline_services, monkeypatch):
    script = offline_services["react_researcher"]
    script.outputs = ['{"action":"web_search","query":"2026 手表"}',
                      '{"action":"web_search","query":"智能手表 出货量"}', '{"action":"done"}']
    search = AsyncMock(side_effect=[
        {"results": [], "error": "只有日历", "error_code": "SEARCH_IRRELEVANT", "attempts": [{"endpoint": "test"}]},
        search_result("https://example.invalid/new", "测试数据") | {"search_endpoint": "test"},
    ])
    monkeypatch.setattr(react_researcher.mcp_manager, "call_tool", search)
    logs = []
    result = asyncio.run(react_researcher.react_researcher_run("研究主题：智能手表", 1, logs))
    assert len(result["observations"]) == 1 and "error" not in result
    assert any("只有日历" in message["content"] for message in script.calls[1][0])
    assert any('"search_endpoint": "test"' in log["content"] for log in logs)


def test_event_log_copy_retains_old_times_without_backfilling_unknown():
    from services.event_logs import EventLogs
    unknown = {"type": "Thought", "content": "历史记录"}
    logs = EventLogs([unknown])
    logs.insert(1, {"type": "Action", "content": "新动作"})
    logs.extend([{"type": "Observation", "content": "新结果"}])
    copied = EventLogs(logs)
    assert "created_at" not in copied[0]
    assert copied[1]["created_at"] == logs[1]["created_at"]
    assert copied[2]["created_at"] == logs[2]["created_at"]


@pytest.mark.parametrize("comments,expected", [
    (["来源不相关，请补充行业白皮书"], True),
    (["非相关参考文献应替换为行业统计"], True),
    (["资料不足"], True),
    (["请调整措辞"], False),
])
def test_review_routes_only_evidence_gaps_to_research(comments, expected):
    from graph.workflow import route_after_review
    state = {"quality_status": "rejected", "topic": "智能手表", "evidence": [source()],
             "review_comments": comments, "iteration": 1}
    assert route_after_review(state) == ("research" if expected else "rewrite")
    state["research_revision_count"] = 1
    assert route_after_review(state) == "rewrite"


def test_missing_citation_with_existing_evidence_does_not_trigger_new_research():
    from graph.workflow import route_after_review
    state = {"quality_status": "rejected", "topic": "智能手表", "evidence": [source()],
             "error_code": "CITATION_INVALID", "review_comments": ["含数字的段落缺少来源引用，数据待补充"]}
    assert route_after_review(state) == "rewrite"


def test_researcher_stops_after_two_irrelevant_searches(offline_services, monkeypatch):
    script = offline_services["react_researcher"]
    script.outputs = ['{"action":"web_search","query":"日历一"}',
                      '{"action":"web_search","query":"日历二"}',
                      '{"action":"web_search","query":"日历三"}']
    search = AsyncMock(side_effect=[
        {"results": [], "error": "只有日历", "error_code": "SEARCH_IRRELEVANT", "attempts": [{"endpoint": "test"}]},
        {"results": [], "error": "只有日历", "error_code": "SEARCH_IRRELEVANT", "attempts": [{"endpoint": "test"}]},
    ])
    monkeypatch.setattr(react_researcher.mcp_manager, "call_tool", search)
    logs = []
    result = asyncio.run(react_researcher.react_researcher_run("研究主题：智能手表", 1, logs))
    assert search.await_count == 2                  # 第 3 轮不再换词空转
    assert result["observations"] == [] and result["error"]
    assert any("停止换词重试" in log["content"] for log in logs)


def test_researcher_deadline_returns_error_instead_of_outer_timeout(monkeypatch):
    async def slow_chat(messages):
        await asyncio.sleep(10)

    monkeypatch.setattr(react_researcher, "chat", slow_chat)
    logs = []
    result = asyncio.run(react_researcher.react_researcher_run(
        "研究主题：智能手表", 1, logs, deadline=time.monotonic() + 0.02,
    ))
    assert result["observations"] == []
    assert result["error"] == "LLM 决策超时"
    assert any("LLM 决策超时" in log["content"] for log in logs)


def test_scheduler_zero_evidence_writes_data_gap_without_writer(monkeypatch):
    writer = AsyncMock(return_value="不应被调用的模型草稿")
    monkeypatch.setattr(scheduler, "react_writer_run", writer)
    result = asyncio.run(scheduler.scheduler_node({
        "task_id": "t", "topic": "2026年智能手表市场",
        "plan": [{"id": 1, "agent": "writer", "task": "撰写报告", "depends_on": []}],
        "react_logs": [], "research_data": {},
    }))
    writer.assert_not_awaited()
    assert DATA_GAP_MARKER in result["draft_content"]
    assert "数据待补充：暂无可追溯来源" in result["draft_content"]
    assert "智能手表" in result["draft_content"]
    assert any("跳过模型撰写" in log["content"] for log in result["react_logs"])


def test_rewriter_keeps_data_gap_draft_without_model(monkeypatch):
    from agents import rewriter
    chat = AsyncMock()
    monkeypatch.setattr(rewriter, "chat", chat)
    draft = data_gap_section("研究主题：智能手表\n子任务：撰写报告", "智能手表")
    result = asyncio.run(rewriter.rewriter_node({
        "task_id": "t", "topic": "智能手表", "draft_content": draft,
        "evidence": [], "review_comments": ["暂无可追溯来源"], "react_logs": [],
    }))
    chat.assert_not_awaited()
    assert result["draft_content"] == draft
    assert result["citation_errors"]
    assert any("跳过模型返工" in log["content"] for log in result["react_logs"])


def test_literal_placeholder_citations_are_stripped():
    from services.citations import strip_placeholder_citations
    assert strip_placeholder_citations("甲（引用位：[S编号]）乙[S编号]丙") == "甲乙丙"
    assert strip_placeholder_citations("引用位：[S编号]") == ""
