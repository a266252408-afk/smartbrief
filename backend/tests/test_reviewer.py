"""Reviewer Agent 测试（agents/reviewer.py，A2A 审核）。"""
import asyncio
import pytest
from unittest.mock import AsyncMock, patch

from agents import reviewer
from agents.reviewer import parse_review, reviewer_node


def run(coro):
    return asyncio.run(coro)


class FakeMessage:
    def __init__(self, content):
        self.content = content


class FakeResp:
    def __init__(self, content):
        self.choices = [type("C", (), {"message": FakeMessage(content)})()]


# ---------------------------------------------------------------- parse_review 容错


def test_parse_review_plain_json():
    assert parse_review('{"passed": true, "comments": []}') == {"passed": True, "comments": []}


def test_parse_review_with_code_fence():
    content = '```json\n{"passed": false, "comments": ["缺数据"]}\n```'
    assert parse_review(content) == {"passed": False, "comments": ["缺数据"]}


def test_parse_review_with_extra_text():
    content = '审核结果如下：{"passed": true, "comments": []} 完毕'
    with pytest.raises(ValueError):
        parse_review(content)


def test_parse_review_broken_falls_back_pass():
    """彻底解析失败必须拒绝解析。"""
    for raw in ("完全不是 JSON", ""):
        with pytest.raises(ValueError):
            parse_review(raw)


def test_parse_review_comments_trimmed_and_capped():
    content = '{"passed": false, "comments": ["  a  ", "b", "", "c", "d", "e", "f"]}'
    result = parse_review(content)
    assert result["comments"] == ["a", "b", "c", "d", "e"]   # 去空白、去空串、最多 5 条


# ---------------------------------------------------------------- reviewer_node


def _state(draft="## 草稿", iteration=0, max_iterations=3):
    return {
        "draft_content": draft,
        "react_logs": [],
        "iteration": iteration,
        "max_iterations": max_iterations,
    }


def test_reviewer_node_passed():
    with patch.object(
        reviewer, "chat", new=AsyncMock(return_value=FakeResp('{"passed": true, "comments": []}'))
    ):
        out = run(reviewer_node(_state()))
    assert out["passed"] is True
    assert out["review_comments"] == []
    assert out["react_logs"]
    assert out["review_history"] == [{"round": 1, "passed": True, "comments": []}]


def test_reviewer_node_rejected():
    with patch.object(
        reviewer, "chat",
        new=AsyncMock(return_value=FakeResp('{"passed": false, "comments": ["缺数据", "字数不足"]}')),
    ):
        out = run(reviewer_node(_state()))
    assert out["passed"] is False
    assert out["iteration"] == 1                      # 打回 → 循环次数 +1
    assert out["forced_pass"] is False
    assert out["review_comments"] == ["缺数据", "字数不足"]
    # 第 1 次审核记录入历史，日志 Observation 带意见原文（供详情页与日志对应）
    assert out["review_history"] == [{"round": 1, "passed": False, "comments": ["缺数据", "字数不足"]}]
    assert any("缺数据" in log["content"] for log in out["react_logs"])


def test_reviewer_node_force_pass_after_max():
    """已达循环上限仍不通过 → 强制输出 + 草稿追加警告标记。

    语义：默认上限 3 次 → 前 2 次打回（iteration 1→2），
    第 3 次（iteration=2 进入）仍不通过才强制输出（iteration → 3）。
    """
    with patch.object(
        reviewer, "chat",
        new=AsyncMock(return_value=FakeResp('{"passed": false, "comments": ["仍缺数据"]}')),
    ):
        out = run(reviewer_node(_state(iteration=2, max_iterations=3)))
    assert out["passed"] is False
    assert out["quality_status"] == "rejected"
    assert out["forced_output"] is True
    assert out["forced_pass"] is True
    assert out["iteration"] == 3
    assert "⚠️" in out["draft_content"]               # 警告标记追加到草稿末尾
    assert out["review_history"] == [{"round": 3, "passed": False, "comments": ["仍缺数据"]}]


def test_reviewer_node_default_max_is_three():
    """未显式传 max_iterations 时默认上限为 3：前 2 次打回，第 3 次才强制输出。"""
    from unittest.mock import AsyncMock as _AsyncMock

    with patch.object(
        reviewer, "chat",
        new=_AsyncMock(return_value=FakeResp('{"passed": false, "comments": ["不行"]}')),
    ):
        first = run(reviewer_node(_state()))
    assert first["passed"] is False and first["iteration"] == 1
    second_state = {**_state(iteration=first["iteration"]), "review_history": first["review_history"]}
    with patch.object(
        reviewer, "chat",
        new=_AsyncMock(return_value=FakeResp('{"passed": false, "comments": ["不行"]}')),
    ):
        second = run(reviewer_node(second_state))
    assert second["passed"] is False and second["iteration"] == 2
    third_state = {**_state(iteration=second["iteration"]), "review_history": second["review_history"]}
    with patch.object(
        reviewer, "chat",
        new=_AsyncMock(return_value=FakeResp('{"passed": false, "comments": ["不行"]}')),
    ):
        third = run(reviewer_node(third_state))
    assert third["passed"] is False
    assert third["quality_status"] == "rejected"
    assert third["forced_pass"] is True
    assert third["iteration"] == 3
    assert len(third["review_history"]) == 3


def test_reviewer_node_llm_error_passes():
    """LLM 审核调用失败 → 质量不可用，意见记录失败原因。"""
    with patch.object(reviewer, "chat", new=AsyncMock(side_effect=RuntimeError("网络错误"))):
        out = run(reviewer_node(_state()))
    assert out["passed"] is False
    assert out["quality_status"] == "unavailable"
    assert out["review_history"][-1]["passed"] is None
    assert out["review_comments"][0].startswith("审核调用失败")
