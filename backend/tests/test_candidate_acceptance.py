"""候选验收必须识别污染，同时允许重复问题减少及段落重排。"""
import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from agents import rewriter
from services.candidate_acceptance import validate_candidate
from services.citations import check_citations
from services.rewrite_patches import paragraph_table
from tests.test_rewriter_patch_flow import state
from tests.test_rewriter import FakeResp


def check(*issues):
    return {"errors": [], "binding_issues": list(issues)}


def issue(paragraph, sentence="苹果同比增长40.9%。"):
    return dict(kind="unbound_number", severity="hard", paragraph=paragraph,
                sentence=sentence, numbers=["40.9%"], source_ids=["S10"])


def test_remove_one_duplicate_and_move_remaining_occurrence():
    validate_candidate("旧稿", "新稿", check(issue(19), issue(31)), check(issue(30)))


def test_new_occurrence_is_rejected():
    with pytest.raises(ValueError, match="新增硬错误"):
        validate_candidate("旧稿", "新稿", check(issue(19)), check(issue(19), issue(31)))


def test_existing_missing_citation_can_move_with_report_reordering():
    before = dict(check(), errors=["含数字段落缺少引用（段落19：市场规模10亿元）"])
    after = dict(check(), errors=["含数字段落缺少引用（段落31：市场规模10亿元）"])
    validate_candidate("旧稿", "新稿", before, after, full=True)


def test_distinct_claim_with_same_number_is_rejected():
    with pytest.raises(ValueError, match="新增硬错误"):
        validate_candidate("旧稿", "新稿", check(issue(19)),
                           check(issue(19, "华为同比增长40.9%。")))


def test_nested_rewrite_protocol_rejected_but_json_example_allowed():
    validate_candidate("旧稿", '# 报告\n\n```json\n{"销量": 10}\n```', check(), check())
    polluted = '# 报告\n\n```json\n' + json.dumps(
        dict(mode="full", content="# 报告", results=[])) + "\n```"
    with pytest.raises(ValueError, match="返工协议"):
        validate_candidate("旧稿", polluted, check(), check())


def test_invalid_json_example_and_new_reference_error():
    validate_candidate("旧稿", "代码示例\n{这不是合法JSON}", check(), check())
    after = dict(check(), errors=["[S999]来源不存在或不属于当前任务"])
    with pytest.raises(ValueError, match="新增引用错误"):
        validate_candidate("旧稿", "新稿[S999]", check(), after)


def test_full_response_keeps_previous_draft_on_pollution():
    s = state()
    s["full_rewrite_required"] = True
    content = s["draft_content"] + '\n\n```json\n{"mode":"full","content":"# 副本","results":[]}\n```'
    response = FakeResp(json.dumps(dict(mode="full", content=content, reason="修复", results=[])))
    with patch.object(rewriter, "chat", new=AsyncMock(return_value=response)):
        out = asyncio.run(rewriter.rewriter_node(s))
    assert out["draft_content"] == s["draft_content"]
    assert out["review_history"][-1]["rewrite"]["attempts"][0]["decision"] == "rejected"


def test_real_e10c_removal_reduces_occurrences_and_preserves_other_error():
    fixture = json.loads((Path(__file__).parent / "fixtures/reference_audit/task_e10c.json").read_text(encoding="utf-8"))
    draft = fixture["drafts"][1]
    rows = paragraph_table(draft)
    replacement = fixture["patch"]["rejected"][0]["proposal"]["replacement"]
    # 真实第19段删除无支持增幅，不改动末尾副本。
    candidate = draft.replace(rows[18]["text"], replacement, 1)
    before = check_citations(fixture["task_id"], draft, fixture["evidence"])
    after = check_citations(fixture["task_id"], candidate, fixture["evidence"])
    assert sum(i["kind"] == "unbound_number" for i in before["binding_issues"]) == 2
    assert sum(i["kind"] == "unbound_number" for i in after["binding_issues"]) == 1
    # 原稿本身已含污染，本步专门验证硬错误身份与位置的分离。
    from services.candidate_acceptance import hard_problem_counts
    assert not hard_problem_counts(after) - hard_problem_counts(before)
    validate_candidate(draft, candidate, before, after)
    with pytest.raises(ValueError, match="返工协议"):
        validate_candidate(fixture["drafts"][0], draft,
                           check_citations(fixture["task_id"], fixture["drafts"][0], fixture["evidence"]), before)


def test_legacy_markdown_protocol_is_rejected():
    s = state()
    s["findings"] = []
    s["draft_content"] = "旧报告"
    content = '# 标题\n\n旧报告\n\n{"mode":"full","content":"副本","results":[]}'
    with patch.object(rewriter, "chat", new=AsyncMock(return_value=FakeResp(content))):
        out = asyncio.run(rewriter.rewriter_node(s))
    assert out["draft_content"] == s["draft_content"]
    assert out["review_history"][-1]["rewrite"]["attempts"][0]["decision"] == "rejected"


def test_full_deterministic_target_must_improve():
    s = state()
    s["full_rewrite_required"] = True
    content = s["draft_content"] + "\n\n仅增加无关文案。"
    response = FakeResp(json.dumps(dict(mode="full", content=content, reason="修复", results=[])))
    with patch.object(rewriter, "chat", new=AsyncMock(return_value=response)):
        out = asyncio.run(rewriter.rewriter_node(s))
    assert out["draft_content"] == s["draft_content"]
    assert out["review_history"][-1]["rewrite"]["attempts"][0]["decision"] == "rejected"
