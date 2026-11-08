"""新任务正常规划/写作入口使用事实绑定，旧计划保持原样。"""
import asyncio
import json
import re
from unittest.mock import AsyncMock, patch

from agents import planner, react_writer
from services.fact_binding import build_registry
from tests.fakes import ScriptedLLM
from tests.test_evidence import source
from tests.test_rewriter import FakeResp


def test_new_plan_has_one_final_writer():
    plan = [{"id": 1, "agent": "researcher", "task": "搜", "depends_on": []},
            {"id": 2, "agent": "writer", "task": "主体", "depends_on": [1]},
            {"id": 3, "agent": "writer", "task": "摘要", "depends_on": [2]}]
    with patch.object(planner, "chat", new=AsyncMock(return_value=FakeResp(json.dumps(plan)))), patch.object(
            planner, "_recall_memory", new=AsyncMock(return_value=[])), patch.object(
            planner, "_template_reference", new=AsyncMock(return_value="")):
        out = asyncio.run(planner.planner_node({"topic": "市场", "report_pipeline_version": 2}))
    writers = [s for s in out["plan"] if s["agent"] == "writer"]
    assert len(writers) == 1 and writers[0]["depends_on"] == [1]
    assert "执行摘要" in writers[0]["task"]


def test_writer_renders_fact_reference_at_real_entry():
    evidence = [source("t", excerpt="2026年第一季度市场规模10亿元。")]
    key = next(iter(build_registry("t", evidence)))
    body = f"# 报告\n\n## 执行摘要\n推断：市场有增长空间。\n\n## 正文\n2026年第一季度市场规模10亿元[{key}]。\n\n## 结论与建议\n建议跟踪。"
    context = {"task_id": "t", "claims": []}
    with patch.object(react_writer, "chat", new=AsyncMock(return_value=FakeResp(body))), patch.object(
            react_writer, "_retrieve_terms", new=AsyncMock(return_value=[])):
        out = asyncio.run(react_writer.react_writer_run("完整报告", 2, [], {}, evidence, binding_context=context))
    assert "[S1]" in out and key not in out
    assert context["claims"][0]["fact_ids"] == [key]


def test_v2_real_graph_persists_bound_report(offline_evidence, monkeypatch):
    from agents import react_researcher
    from db import database, task_repository
    from graph.workflow import run_task
    database.init_db()
    task_repository.create_task("bound-graph", "市场研究", metadata={"report_pipeline_version": 2})
    monkeypatch.setattr(react_researcher.mcp_manager, "call_tool", AsyncMock(return_value={
        "source": "fixture", "results": [{"title": "季度统计", "url": "https://example.invalid/stat",
                                       "snippet": "2026年第一季度市场规模10亿元。同比增长20%。"}]}))
    offline_evidence["planner"].outputs = [json.dumps([
        {"id": 1, "agent": "researcher", "task": "研究市场", "depends_on": []},
        {"id": 2, "agent": "writer", "task": "完整报告", "depends_on": [1]}])]
    def write(messages):
        ids = re.findall(r"^\[(F[a-f0-9]+)\]", messages[-1]["content"], re.M)
        return ("# 市场研究报告\n\n## 执行摘要\n推断：建议关注市场变化。\n\n## 正文\n"
                f"2026年第一季度市场规模10亿元[{ids[0]}]。\n\n同比增长20%[{ids[1]}]。\n\n"
                "## 结论与建议\n建议跟踪后续统计。")
    offline_evidence["react_writer"].outputs = [write]
    offline_evidence["reviewer"].outputs = ['{"passed":true,"comments":[]}']
    asyncio.run(run_task("bound-graph", "市场研究"))
    task = task_repository.get_task("bound-graph")
    assert task["quality_status"] == "passed"
    assert len([s for s in task["plan"] if s["agent"] == "writer"]) == 1
    assert "[S1]" in task["final_report"] and "[F" not in task["final_report"]
    history = task["review_history"][0]
    assert len(history["claim_bindings"]) == 2
    assert all(key in history["fact_registry"] for c in history["claim_bindings"] for key in c["fact_ids"])
    from services.review_policy import policy_text
    policy = history['review_policy']
    assert policy['task_id'] == 'bound-graph'
    assert policy['required_questions'] == ['市场研究']
    for agent in ('react_writer', 'reviewer'):
        messages = offline_evidence[agent].calls[0][0]
        assert policy_text(policy) in '\n'.join(m['content'] for m in messages)
    async def saved_policy():
        from memory.checkpointer import open_checkpointer
        from graph.workflow import build_graph
        async with open_checkpointer() as (cp, kind):
            assert kind == 'sqlite'
            snapshot = await build_graph(checkpointer=cp).aget_state({'configurable': {'thread_id': 'bound-graph'}})
            return snapshot.values['review_policy']
    assert asyncio.run(saved_policy()) == policy



def test_summary_can_repeat_one_fact_but_not_two_verbatim_sentences():
    from services.report_quality import repeated_passages
    first = "2026年第一季度市场规模为10亿元[S1]。"
    second = "2026年市场的同比增长率为20%[S1]。"
    assert not repeated_passages("## 执行摘要\n" + first + "\n\n## 正文\n" + first)
    repeated = repeated_passages("## 执行摘要\n" + first + second + "\n\n## 正文\n" + first + second)
    assert repeated[0]["kind"] == "duplicate_passage"


def test_bound_single_writer_always_checks_full_structure():
    task = planner.single_writer_plan([], "市场")[-1]["task"]
    findings = react_writer._mechanical_findings(
        "# 报告\n\n市场规模10亿元[S1]。", [source("t", excerpt="市场规模10亿元。")],
        task, detect_repetition=True)
    assert {"missing_summary", "missing_conclusion"} <= {f["kind"] for f in findings}


def test_v2_reviewer_cannot_pass_missing_required_sections():
    from agents import reviewer
    s = {"task_id": "t", "report_pipeline_version": 2, "draft_content": "# 报告\n\n市场规模10亿元[S1]。",
         "evidence": [source("t", excerpt="市场规模10亿元。")]}
    with patch.object(reviewer, "chat", new=AsyncMock(return_value=FakeResp('{"passed":true,"comments":[]}'))):
        out = asyncio.run(reviewer.reviewer_node(s))
    assert out["quality_status"] == "rejected"
    assert any(f.get("rule") == "required_structure" for f in out["findings"])


def _bound_writer_run(evidence, outputs, task="撰写市场规模章节"):
    logs = []
    model = ScriptedLLM(outputs)
    context = {"task_id": "t", "claims": []}
    with patch.object(react_writer, "_retrieve_terms", new=AsyncMock(return_value=[])), patch.object(
            react_writer, "chat", new=model.chat):
        content = asyncio.run(
            react_writer.react_writer_run(task, 1, logs, {}, evidence, binding_context=context))
    return content, logs, model, context


def test_writer_repairs_unbound_sentence_once_before_failing():
    evidence = [source("t", excerpt="2026年第一季度市场规模10亿元。")]
    key = next(iter(build_registry("t", evidence)))
    bad = ("# 市场报告\n\n## 执行摘要\n\n市场前景广阔。\n\n## 正文\n\n"
           f"2026年第一季度市场规模10亿元[{key}]。\n\n## 结论与建议\n\n建议跟踪。")
    fixed = bad.replace("市场前景广阔。", "推断：市场前景广阔。")
    content, logs, model, context = _bound_writer_run(evidence, [bad, fixed])
    assert len(model.calls) == 2
    assert "推断：市场前景广阔。" in content
    assert context["claims"][0]["fact_ids"] == [key]
    assert any("事实绑定未通过" in log["content"] for log in logs)
    assert any("修复稿通过校验" in log["content"] for log in logs)


def test_writer_drops_unsupported_sentence_when_repair_still_unbound():
    evidence = [source("t", excerpt="2026年第一季度市场规模10亿元。")]
    key = next(iter(build_registry("t", evidence)))
    body = ("# 市场报告\n\n## 执行摘要\n\n市场前景广阔。\n\n## 正文\n\n"
            f"2026年第一季度市场规模10亿元[{key}]。\n\n## 结论与建议\n\n建议跟踪。")
    content, logs, model, context = _bound_writer_run(evidence, [body, body])
    assert len(model.calls) == 2
    assert "市场前景广阔。" not in content
    assert "2026年第一季度市场规模10亿元[S1]。" in content
    assert context["claims"][0]["fact_ids"] == [key]
    assert any("按事实协议删除 1 条无支持声明" in log["content"] for log in logs)


def test_v2_graph_recovers_from_unbound_first_draft(offline_evidence, monkeypatch):
    from agents import react_researcher
    from db import database, task_repository
    from graph.workflow import run_task
    database.init_db()
    task_repository.create_task("bound-repair", "市场研究", metadata={"report_pipeline_version": 2})
    monkeypatch.setattr(react_researcher.mcp_manager, "call_tool", AsyncMock(return_value={
        "source": "fixture", "results": [{"title": "季度统计", "url": "https://example.invalid/stat",
                                       "snippet": "2026年第一季度市场规模10亿元。同比增长20%。"}]}))
    offline_evidence["planner"].outputs = [json.dumps([
        {"id": 1, "agent": "researcher", "task": "研究市场", "depends_on": []},
        {"id": 2, "agent": "writer", "task": "完整报告", "depends_on": [1]}])]

    def ids(messages):
        return re.findall(r"(?m)^\[(F[a-f0-9]+)\]", "\n".join(m["content"] for m in messages))

    def first(messages):
        found = ids(messages)
        return ("# 市场研究报告\n\n## 执行摘要\n\n市场前景广阔。\n\n## 正文\n\n"
                f"2026年第一季度市场规模10亿元[{found[0]}]。\n\n同比增长20%[{found[1]}]。\n\n"
                "## 结论与建议\n\n建议跟踪后续统计。")

    def repaired(messages):
        found = ids(messages)
        return ("# 市场研究报告\n\n## 执行摘要\n\n推断：市场前景广阔。\n\n## 正文\n\n"
                f"2026年第一季度市场规模10亿元[{found[0]}]。\n\n同比增长20%[{found[1]}]。\n\n"
                "## 结论与建议\n\n建议跟踪后续统计。")

    offline_evidence["react_writer"].outputs = [first, repaired]
    offline_evidence["reviewer"].outputs = ['{"passed":true,"comments":[]}']
    asyncio.run(run_task("bound-repair", "市场研究"))
    task = task_repository.get_task("bound-repair")
    assert task["execution_status"] == "completed"
    assert "推断：市场前景广阔。" in task["final_report"]
    assert "[S1]" in task["final_report"] and "[F" not in task["final_report"]
