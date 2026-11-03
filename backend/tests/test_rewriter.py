"""Rewriter Agent 测试（agents/rewriter.py，A2A 返工）。"""
import asyncio
from unittest.mock import AsyncMock, patch

from agents import rewriter
from agents.rewriter import rewriter_node


def run(coro):
    return asyncio.run(coro)


class FakeMessage:
    def __init__(self, content):
        self.content = content


class FakeResp:
    def __init__(self, content):
        self.choices = [type("C", (), {"message": FakeMessage(content)})()]


def _state(draft="## 原稿", comments=("缺数据",), logs=()):
    return {"draft_content": draft, "review_comments": list(comments), "react_logs": list(logs)}


def test_rewriter_node_rewrites():
    """按审核意见生成修改稿，覆盖式回写 draft_content。"""
    with patch.object(rewriter, "chat", new=AsyncMock(return_value=FakeResp("## 修改稿\n含数据与来源"))):
        out = run(rewriter_node(_state()))
    assert out["draft_content"] == "## 修改稿\n含数据与来源"
    assert "原稿" not in out["draft_content"]          # 覆盖式：不再包含旧稿
    contents = [log["content"] for log in out["react_logs"]]
    assert any("修改稿" in c for c in contents)         # Observation 记录返工结果


def test_rewriter_node_keeps_original_on_llm_error():
    """LLM 返工失败 → 保留原草稿（不恶化），记录 Observation。"""
    with patch.object(rewriter, "chat", new=AsyncMock(side_effect=RuntimeError("网络错误"))):
        out = run(rewriter_node(_state()))
    assert out["draft_content"] == "## 原稿"
    contents = [log["content"] for log in out["react_logs"]]
    assert any("保留原草稿" in c for c in contents)


def test_rewriter_node_injects_topic_and_research_data():
    """Prompt 必须携带研究主题与可引用研究数据，防止改写跑题/凭空编造。"""
    captured = {}

    async def fake_chat(messages):
        captured["system"] = messages[0]["content"]
        captured["user"] = messages[1]["content"]
        return FakeResp("## 围绕主题的修改稿")

    state = {
        "topic": "2026年智能手表市场",
        "draft_content": "（步骤 4 撰写超时，内容待补充）",   # 原稿为空/占位 → 需基于数据重写
        "review_comments": ["请补充市场规模数据"],
        "react_logs": [],
        "research_data": {
            "step_1": {"observations": [{"query": "市场规模", "result": {"value": "100亿美元"}}]}
        },
    }
    with patch.object(rewriter, "chat", new=fake_chat):
        out = run(rewriter_node(state))
    assert out["draft_content"] == "## 围绕主题的修改稿"
    # 主题注入 Prompt，作为改写锚点
    assert "2026年智能手表市场" in captured["user"]
    # 研究数据摘要被注入（搜索结果里的"市场规模"关键词应出现在摘要里）
    assert "step_1" in captured["user"]
    # 系统提示词包含"围绕主题/严禁跑题"约束
    assert "研究主题" in captured["system"]
    assert "严禁" in captured["system"] or "不得偏离" in captured["system"]


def test_rewriter_prompt_shares_factual_discipline():
    captured = {}

    async def fake_chat(messages):
        captured["system"] = messages[0]["content"]
        return FakeResp("## 修改稿")

    with patch.object(rewriter, "chat", new=fake_chat):
        run(rewriter_node(_state(draft="## 原稿")))
    for keyword in ("预测", "存量", "百分点", "冲突", "用户指令"):
        assert keyword in captured["system"]


def test_rewriter_attaches_unique_citation_before_review():
    from tests.test_evidence import source

    async def fake_chat(messages):
        return FakeResp('预测销量为2300万辆。')

    state = {'task_id': 't', 'topic': '电动车', 'draft_content': '预测销量为2300万辆。',
             'review_comments': ['含数字的声明段落缺少来源引用'], 'react_logs': [],
             'evidence': [source(excerpt='2026年预计2300万辆')], 'research_data': {}}
    with patch.object(rewriter, 'chat', new=fake_chat):
        out = run(rewriter_node(state))
    assert out['draft_content'].strip().endswith('[S1]')
    assert any('确定性补引用' in log['content'] for log in out['react_logs'])


def test_rewriter_prompt_lists_candidate_sources_when_ambiguous():
    from tests.test_evidence import source
    captured = {}

    async def fake_chat(messages):
        captured['user'] = messages[1]['content']
        return FakeResp('预测销量为2300万辆。')

    sources = [source(excerpt='2300万辆'), source(source_id='S2', url='https://example.invalid/b', excerpt='2300万辆')]
    state = {'task_id': 't', 'topic': '电动车', 'draft_content': '预测销量为2300万辆。',
             'review_comments': ['含数字的声明段落缺少来源引用'], 'react_logs': [],
             'evidence': sources, 'research_data': {}}
    with patch.object(rewriter, 'chat', new=fake_chat):
        run(rewriter_node(state))
    assert '候选来源：S1、S2' in captured['user']
