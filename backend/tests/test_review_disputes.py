"""独立复核可以撤销误读，真实冲突与复核失败继续拒绝。"""
import asyncio
import json
from unittest.mock import AsyncMock

from agents.reviewer import _parse_response
from services.review_disputes import resolve_disputes
from services.review_progress import review_progress
from tests.test_evidence import source
from tests.test_rewriter import FakeResp
import pytest
from services.usage_service import UsagePersistenceError


def test_disputed_source_review_can_withdraw_or_confirm():
    initial = {"passed": False, "comments": ["来源未提第二"], "findings": [
        {"finding_id": "M1", "severity": "hard", "evidence": {"kind": "source", "source_id": "S1"}}]}
    for passed in (True, False):
        result = {"passed": passed, "comments": [] if passed else ["统计对象不一致"], "findings": []}
        chat = AsyncMock(return_value=FakeResp(json.dumps(result)))
        review, audit = asyncio.run(resolve_disputes(
            initial, "苹果排名第二[S1]。", [source(excerpt="Apple followed in second place.")], chat, _parse_response))
        assert review["passed"] == passed
        assert audit["status"] == "reviewed" and chat.await_count == 1
        assert "second place" in chat.call_args.args[0][1]["content"]


def test_dispute_failure_preserves_rejection_and_incomplete_status():
    initial = {"passed": False, "comments": ["冲突"], "findings": [
        {"severity": "hard", "evidence": {"kind": "source"}}]}
    result, audit = asyncio.run(resolve_disputes(initial, "正文", [],
        AsyncMock(side_effect=RuntimeError("失败")), _parse_response))
    assert not result["passed"]
    assert result["review_diagnostics"]["code"] == "DISPUTE_UNRESOLVED"
    assert audit["status"] == "unresolved"


def test_removing_one_duplicate_counts_as_progress():
    issue = {"rule": "unbound_number", "numbers": ["40.9%"], "problem": "无支持"}
    previous = {"hard_finding_set": [issue, issue], "review_complete": True, "rewrite": {"changed": True}}
    result = review_progress(previous, [issue], True)
    assert result["resolved"] and not result["stopped"]


def test_dispute_limits_and_ledger_failure():
    initial = {"passed": False, "comments": ["冲突"], "findings": [
        {"severity": "hard", "evidence": {"kind": "source"}}]}
    chat = AsyncMock()
    result, audit = asyncio.run(resolve_disputes(initial, "字" * 64001, [], chat, _parse_response))
    assert not result["passed"] and audit["status"] == "unresolved"
    chat.assert_not_awaited()
    with pytest.raises(UsagePersistenceError):
        asyncio.run(resolve_disputes(initial, "正文", [],
            AsyncMock(side_effect=UsagePersistenceError("账本失败")), _parse_response))
    result, audit = asyncio.run(resolve_disputes(initial, "正文", [],
        AsyncMock(return_value=FakeResp("{}")), lambda response: {"review_diagnostics": {"code": "BAD"}}))
    assert audit["status"] == "unresolved" and not result["passed"]
