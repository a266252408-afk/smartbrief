"""Agent 级记忆 / RAG 注入测试。

- planner_node：检索到长期记忆 → Few-shot 注入 user prompt + memory_hits 记录 + 日志
- react_writer_run：检索到 RAG 术语 → Action 日志标注 + 术语定义拼入 prompt
（均 patch 检索函数，无需真实 Chroma/嵌入/网络。）
"""
import asyncio
from unittest.mock import AsyncMock, patch

from agents import planner, react_writer


def run(coro):
    return asyncio.run(coro)


class FakeMessage:
    def __init__(self, content):
        self.content = content


class FakeResp:
    def __init__(self, content, finish_reason=None):
        self.choices = [type("C", (), {"message": FakeMessage(content), "finish_reason": finish_reason})()]


PLAN_JSON = '[{"id": 1, "agent": "researcher", "task": "搜市场规模", "depends_on": []}, {"id": 2, "agent": "writer", "task": "写报告", "depends_on": [1]}]'


# ---------------------------------------------------------------- Planner 记忆 Few-shot


def test_planner_injects_memory_hit_as_few_shot():
    hit = {
        "topic": "便携储能市场调研",
        "score": 0.91,
        "task_id": "task_old01",
        "created_at": "2026-09-01 10:00:00",
        "structure": ["# 一、市场规模", "# 二、竞争格局", "# 三、技术趋势"],
    }
    state = {"topic": "便携储能", "react_logs": [], "memory_hits": []}
    chat = AsyncMock(return_value=FakeResp(PLAN_JSON))
    with (
        patch.object(planner, "_recall_memory", new=AsyncMock(return_value=[hit])),
        patch.object(planner, "chat", new=chat),
    ):
        out = run(planner.planner_node(state))

    # 1) memory_hits 已记录最相似命中（含 task_id 去重判断字段）
    assert out["memory_hits"] == [{
        "topic": "便携储能市场调研", "score": 0.91,
        "task_id": "task_old01", "created_at": "2026-09-01 10:00:00",
    }]
    # 2) Few-shot 已拼进发给 LLM 的 user 消息
    sent_messages = chat.await_args[0][0]
    user_text = sent_messages[-1]["content"]
    assert "【长期记忆参考】" in user_text
    assert "历史主题：便携储能市场调研" in user_text
    assert "# 一、市场规模" in user_text
    # 3) 日志含"检索到历史报告"Thought
    assert any("检索到历史报告" in log["content"] for log in out["react_logs"])
    # 4) Plan 正常生成
    assert len(out["plan"]) == 2


def test_planner_no_memory_hit_keeps_plain_prompt():
    state = {"topic": "全新主题", "react_logs": [], "memory_hits": []}
    chat = AsyncMock(return_value=FakeResp(PLAN_JSON))
    with (
        patch.object(planner, "_recall_memory", new=AsyncMock(return_value=[])),
        patch.object(planner, "chat", new=chat),
    ):
        out = run(planner.planner_node(state))
    assert out["memory_hits"] == []
    user_text = chat.await_args[0][0][-1]["content"]
    assert "【长期记忆参考】" not in user_text
    assert not any("检索到历史报告" in log["content"] for log in out["react_logs"])


def test_planner_memory_hit_dedup_on_resume():
    """续跑场景：state 已有同 task_id 命中 → 不重复追加。"""
    hit = {"topic": "T", "score": 0.8, "task_id": "task_old01", "created_at": "c1",
           "structure": ["# A"]}
    state = {"topic": "X", "react_logs": [], "memory_hits": [
        {"topic": "T", "score": 0.8, "task_id": "task_old01", "created_at": "c1"},
    ]}
    with (
        patch.object(planner, "_recall_memory", new=AsyncMock(return_value=[hit])),
        patch.object(planner, "chat", new=AsyncMock(return_value=FakeResp(PLAN_JSON))),
    ):
        out = run(planner.planner_node(state))
    assert len(out["memory_hits"]) == 1


def test_planner_recall_failure_ignored():
    """记忆子系统异常 → 不阻断规划（降级为无记忆路径）。

    _recall_memory 是容错包装层：内部 catch 真实 Chroma/线程异常并返回 []。
    故此处让底层 long_term_memory.recall 抛异常来触发降级路径。
    """
    from memory import long_term_memory

    state = {"topic": "主题", "react_logs": [], "memory_hits": []}
    chat = AsyncMock(return_value=FakeResp(PLAN_JSON))
    with (
        patch.object(long_term_memory, "recall", side_effect=RuntimeError("挂了")),
        patch.object(planner, "chat", new=chat),
    ):
        out = run(planner.planner_node(state))
    assert out["memory_hits"] == []
    assert len(out["plan"]) == 2


# ---------------------------------------------------------------- Writer RAG 术语注入


def test_writer_injects_rag_terms():
    logs = []
    terms = ["YoY：同比增长率（Year-over-Year）", "渗透率：某产品在目标市场中的普及程度"]
    chat = AsyncMock(return_value=FakeResp("## 市场规模\n章节内容"))
    with (
        patch.object(react_writer, "_retrieve_terms", new=AsyncMock(return_value=terms)),
        patch.object(react_writer, "chat", new=chat),
    ):
        text = run(react_writer.react_writer_run(
            "撰写市场规模章节", 1, logs, {"step_1": {"observations": [{"query": "q", "result": "100亿"}]}}
        ))
    assert "章节内容" in text
    # Action 日志标注注入条数
    assert any("已注入 RAG 术语 2 条" in log["content"] for log in logs)
    # user prompt 含术语定义
    user_text = chat.await_args[0][0][-1]["content"]
    assert "可参考的术语定义" in user_text
    assert "YoY：同比增长率（Year-over-Year）" in user_text


def test_writer_without_terms_plain_action():
    logs = []
    chat = AsyncMock(return_value=FakeResp("## 章节"))
    with (
        patch.object(react_writer, "_retrieve_terms", new=AsyncMock(return_value=[])),
        patch.object(react_writer, "chat", new=chat),
    ):
        text = run(react_writer.react_writer_run("撰写章节", 2, logs, {}))
    assert text == "## 章节"
    assert any(log["content"].startswith("llm_gen") for log in logs)
    assert not any("已注入 RAG 术语" in log["content"] for log in logs)
    user_text = chat.await_args[0][0][-1]["content"]
    assert "可参考的术语定义" not in user_text
    assert "结构化事实表" not in user_text


def test_writer_injects_structured_fact_table():
    from tests.test_evidence import source

    logs = []
    captured = {}

    async def fake_chat(messages, **kwargs):
        captured["user"] = messages[1]["content"]  # 自检消息追加后仍核对原始写作素材
        return FakeResp("## 市场规模\n章节内容")

    evidence = [source(
        source_id="S11", url="https://example.invalid/a",
        excerpt="The market is projected to grow from USD 46.39 billion in 2026 "
                "to USD 74.98 billion by 2035.",
    )]
    with (
        patch.object(react_writer, "_retrieve_terms", new=AsyncMock(return_value=[])),
        patch.object(react_writer, "chat", new=fake_chat),
    ):
        run(react_writer.react_writer_run("撰写市场规模章节", 1, logs, {}, evidence=evidence))
    assert "结构化事实表" in captured["user"]
    assert "46.39" in captured["user"] and "2026" in captured["user"]
    assert "预测" in captured["user"]
    assert any("结构化事实：2 条" in log["content"] for log in logs)


def test_writer_empty_response_is_explicit_failure():
    logs = []
    chat = AsyncMock(return_value=FakeResp(""))
    with (
        patch.object(react_writer, "_retrieve_terms", new=AsyncMock(return_value=[])),
        patch.object(react_writer, "chat", new=chat),
    ):
        text = run(react_writer.react_writer_run("撰写章节", 3, logs, {}))
    assert text == "（章节撰写失败：模型返回空正文）"
    assert any("空响应" in log["content"] for log in logs)
    assert not any("已生成章节草稿" in log["content"] for log in logs)


def test_writer_empty_response_at_token_limit_reports_cause():
    logs = []
    chat = AsyncMock(return_value=FakeResp("", finish_reason="length"))
    with (
        patch.object(react_writer, "_retrieve_terms", new=AsyncMock(return_value=[])),
        patch.object(react_writer, "chat", new=chat),
    ):
        text = run(react_writer.react_writer_run("撰写章节", 4, logs, {}))
    assert "输出达到 Token 上限" in text
    assert any("finish_reason=length" in log["content"] for log in logs)


def test_writer_truncated_content_is_logged_but_kept():
    logs = []
    chat = AsyncMock(return_value=FakeResp("## 章节\n部分内容", finish_reason="length"))
    with (
        patch.object(react_writer, "_retrieve_terms", new=AsyncMock(return_value=[])),
        patch.object(react_writer, "chat", new=chat),
    ):
        text = run(react_writer.react_writer_run("撰写章节", 5, logs, {}))
    assert text == "## 章节\n部分内容"
    assert any("草稿可能不完整" in log["content"] for log in logs)


def test_writer_term_retrieval_failure_ignored():
    """RAG 子系统异常 → 不阻断撰写（无术语注入继续写）。

    _retrieve_terms 是容错包装层：内部 catch 真实 Chroma/线程异常并返回 []，
    故此处让底层 rag_store.search_rag_terms 抛异常来触发降级路径。
    """
    from rag import store as rag_store

    logs = []
    chat = AsyncMock(return_value=FakeResp("## 章节"))
    with (
        patch.object(rag_store, "search_rag_terms", side_effect=RuntimeError("挂了")),
        patch.object(react_writer, "chat", new=chat),
    ):
        text = run(react_writer.react_writer_run("撰写章节", 1, logs, {}))
    assert text == "## 章节"
    assert not any("已注入 RAG 术语" in log["content"] for log in logs)


def test_writer_term_retrieval_applies_topic_filter():
    from rag import store as rag_store

    terms = ["BMS：电池管理系统", "BEV/PHEV：纯电动汽车", "续航：单次满电里程"]
    with patch.object(rag_store, "search_rag_terms", return_value=terms):
        selected = run(react_writer._retrieve_terms("2026电瓶车市场"))
    assert "BMS：电池管理系统" in selected
    assert "续航：单次满电里程" in selected
    assert "BEV/PHEV：纯电动汽车" not in selected
