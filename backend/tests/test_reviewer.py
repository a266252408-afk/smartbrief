"""Reviewer Agent 测试（agents/reviewer.py，A2A 审核）。"""
import asyncio
import pytest
from unittest.mock import AsyncMock, patch

from tests.test_evidence import source
from agents import reviewer
from agents.reviewer import parse_review, reviewer_node


def run(coro):
    return asyncio.run(coro)


def history_view(history):
    return [{key: item[key] for key in ("round", "passed", "comments")} for item in history]


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


def test_parse_review_comments_trimmed_without_losing_actionable_items():
    content = '{"passed": false, "comments": ["  a  ", "b", "", "c", "d", "e", "f"]}'
    result = parse_review(content)
    assert result["comments"] == ["a", "b", "c", "d", "e", "f"]   # 去空白、去空串，完整保留意见


# ---------------------------------------------------------------- reviewer_node


def _state(draft="## 草稿[S1]", iteration=0, max_iterations=3):
    return {
        "task_id": "t",
        "evidence": [source()],
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
    assert history_view(out["review_history"]) == [{"round": 1, "passed": True, "comments": []}]


def test_length_contradiction_comment_is_dropped_without_invalidating_review():
    result = {'comments': ['字数不足：约600字。', '其他建议'], 'findings': [], 'passed': True}
    violations = [{'code': 'LENGTH_CONTRADICTS_COUNT', 'text': '字数不足：约600字。'}]
    dropped = reviewer._drop_length_contradictions(result, violations)
    assert dropped == ['字数不足：约600字。']
    assert result['comments'] == ['其他建议']


def test_reviewer_blocks_glued_heading_even_when_model_passes():
    draft = ('# 报告\n\n## 执行摘要\n\n摘要内容[S1]。\n\n'
             '## 二、标题与正文粘连继续说明[S1]。\n\n'
             '## 结论与建议\n\n建议内容[S1]。')
    state = _state(draft=draft)
    state["report_pipeline_version"] = 2
    with patch.object(
        reviewer, "chat", new=AsyncMock(return_value=FakeResp('{"passed": true, "comments": []}'))
    ):
        out = run(reviewer_node(state))
    assert out["passed"] is False
    assert out["quality_status"] == "rejected"
    assert any(f.get("structure_kind") == "glued_heading" for f in out["findings"])


def test_reviewer_node_rejected():
    with patch.object(
        reviewer, "chat",
        new=AsyncMock(return_value=FakeResp('{"passed": false, "comments": ["缺数据", "字数不足"]}')),
    ):
        out = run(reviewer_node(_state()))
    assert out["passed"] is False
    assert out["iteration"] == 1                      # 打回 → 循环次数 +1
    assert out["forced_pass"] is True
    assert out["review_history"][-1]["stop_reason"] == "REVIEW_NO_ACTIONABLE_FINDINGS"
    assert out["review_comments"] == ["缺数据", "字数不足"]
    # 第 1 次审核记录入历史，日志 Observation 带意见原文（供详情页与日志对应）
    assert history_view(out["review_history"]) == [{"round": 1, "passed": False, "comments": ["缺数据", "字数不足"]}]
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
    assert history_view(out["review_history"]) == [{"round": 3, "passed": False, "comments": ["仍缺数据"]}]


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


def test_reviewer_prompt_accepts_internal_citation_ids_and_limited_sources():
    captured = {}

    async def fake_chat(messages, **kwargs):
        captured['system'] = messages[0]['content']
        captured['user'] = messages[1]['content']
        return FakeResp('{"passed": true, "comments": []}')

    with patch.object(reviewer, 'chat', new=fake_chat):
        run(reviewer_node(_state(draft='## 草稿[S1]')))
    assert '[S#' in captured['system']
    assert '不得要求改写' in captured['system']
    assert '可用来源编号：S1' in captured['user']
    assert '资料不足以支撑 1000 字' in captured['user']




def test_isolate_soft_invalid_drops_only_soft_findings():
    findings = [{'finding_id': 'H', 'severity': 'hard'}, {'finding_id': 'S', 'severity': 'soft'}]
    kept = reviewer.isolate_soft_invalid(findings, [{'path': '/findings/1/evidence/quote'}])
    assert [finding['finding_id'] for finding in kept] == ['H']


def test_isolate_soft_invalid_keeps_round_when_hard_or_non_finding_invalid():
    findings = [{'finding_id': 'H', 'severity': 'hard'}, {'finding_id': 'S', 'severity': 'soft'}]
    assert reviewer.isolate_soft_invalid(findings, [{'path': '/findings/0/evidence/quote'}]) is None
    assert reviewer.isolate_soft_invalid(findings, [{'path': '/content_checks'}]) is None


def test_repair_advisory_quotes_uses_verbatim_sentence_and_drops_invalid_rows():
    body = "# 标题\n\n## 执行摘要\n\n2024年安装量为54.2万台。[S1] 存量为466.4万台。[S2]"
    result = {
        'core_coverage': [{
            'question': 'q', 'covered': True,
            'quote': '## 执行摘要\n2024年安装量为54.2万台。[S1]', 'reason': 'r',
        }],
        'semantic_duplicates': [{
            'first_quote': '报告中不存在', 'second_quote': '2024年安装量为54.2万台。[S1]',
        }],
    }
    corrections = reviewer.repair_advisory_quotes(result, body)
    assert result['core_coverage'][0]['quote'] == '2024年安装量为54.2万台。[S1]'
    assert result['semantic_duplicates']
    assert 'semantic_duplicates row invalid' in corrections


def test_repair_source_finding_quotes_keeps_longest_verbatim_segment():
    evidence = [source(source_id="S3", excerpt=(
        "庭院设计与施工服务在2024年占据了约38%的市场份额，规模约为323亿元；"
        "园林绿化植物供应紧随其后，占比达32%，约为272亿元。"))]
    result = {"findings": [{
        "finding_id": "MODEL-0002", "severity": "hard",
        "evidence": {"kind": "source", "source_id": "S3",
                     "quote": "庭院设计与施工服务在2024年占据了约38%的市场份额，规模约为323亿元... "
                              "园艺植物供应紧随其后，占比达32%，约为272亿元"},
    }]}
    fixes = reviewer.repair_source_finding_quotes(result, evidence, "t")
    assert fixes
    quote = result["findings"][0]["evidence"]["quote"]
    assert quote.startswith("庭院设计与施工服务")
    assert "园艺植物供应" not in quote


def test_fully_paraphrased_source_quote_is_not_repaired():
    evidence = [source(source_id="S3", excerpt="庭院设计与施工服务在2024年占据了约38%的市场份额。")]
    result = {"findings": [{"finding_id": "M1", "evidence": {
        "kind": "source", "source_id": "S3", "quote": "完全改写且来源没有的句子"}}]}
    assert reviewer.repair_source_finding_quotes(result, evidence, "t") == []
    assert result["findings"][0]["evidence"]["quote"] == "完全改写且来源没有的句子"
