"""Phase 3 审核闭环：始终调用 agent、四要素 findings、证据校验与规则误判出口。"""
import asyncio
import json
from unittest.mock import patch

from agents import reviewer, rewriter
from tests.fakes import ScriptedLLM
from tests.test_evidence import source

PASS = '{"passed": true, "comments": []}'


def review(body, evidence, model_output):
    model = ScriptedLLM([model_output])
    with patch.object(reviewer, "chat", new=model.chat):
        out = asyncio.run(reviewer.reviewer_node({
            "task_id": "t",
            "draft_content": body,
            "evidence": evidence,
        }))
    return out, model


def structured(**changes):
    payload = {
        "passed": False,
        "comments": ["请修正"],
        "findings": [{
            "finding_id": "BIND-0001",
            "problem": "单位量级错误",
            "evidence": {"source_id": "S11", "quote": "from USD 46.39 billion"},
            "location": {"paragraph": 1, "sentence": 1},
            "expected": "改写为 463.9 亿美元",
            "severity": "hard",
        }],
        "possible_rule_issue": None,
        "full_rewrite_required": False,
    }
    payload.update(changes)
    return json.dumps(payload, ensure_ascii=False)


def test_reviewer_calls_agent_even_when_mapping_invalid():
    out, model = review(
        "销量2300万辆[S1]；另见[S999]。",
        [source(excerpt="增长10%")],
        PASS,
    )
    assert len(model.calls) == 1
    assert out["passed"] is False
    assert out["quality_status"] == "rejected"
    assert any(finding["rule"] == "unbound_number" for finding in out["findings"])


def test_deterministic_findings_have_four_elements():
    out, _ = review(
        "市场规模将从46.39亿美元增长。[S11]",
        [source(source_id="S11", excerpt="from USD 46.39 billion in 2026")],
        PASS,
    )
    assert out["findings"]
    for finding in out["findings"]:
        assert finding["problem"]
        assert finding["evidence"]["source_id"]
        assert finding["evidence"]["quote"]
        assert finding["location"]["paragraph"] >= 1
        assert finding["location"]["sentence"] >= 1
        assert finding["expected"]
        assert finding["severity"] in {"hard", "soft"}
    assert "463.9亿美元" in out["findings"][0]["expected"]
    assert out["review_history"][0]["findings"] == out["findings"]


def test_reviewer_drops_model_finding_with_fabricated_quote():
    out, model = review(
        "收入增长10%[S1]。",
        [source(excerpt="收入增长10%")],
        structured(
            findings=[{
                "finding_id": "REV-0001",
                "problem": "模型编造",
                "evidence": {"source_id": "S1", "quote": "来源没有这句话"},
                "location": {"paragraph": 1, "sentence": 1},
                "expected": "删除",
                "severity": "hard",
            }],
        ),
    )
    assert len(model.calls) == 1
    assert not out["findings"]
    assert any("证据不在来源中" in log["content"] for log in out["react_logs"])


def test_reviewer_records_possible_rule_issue_without_overriding():
    out, _ = review(
        "市场规模将从46.39亿美元增长。[S11]",
        [source(source_id="S11", excerpt="from USD 46.39 billion in 2026")],
        structured(possible_rule_issue="数值相同但单位换算，可能误判"),
    )
    assert out["passed"] is False
    assert out["possible_rule_issue"] == "数值相同但单位换算，可能误判"
    assert any("仅记录" in log["content"] for log in out["react_logs"])


def test_reviewer_records_full_rewrite_required():
    out, _ = review(
        "收入增长10%[S1]。",
        [source(excerpt="收入增长10%")],
        structured(findings=[], full_rewrite_required=True),
    )
    assert out["full_rewrite_required"] is True
    assert out["review_history"][0]["full_rewrite_required"] is True


def test_rewriter_prompt_includes_structured_findings():
    captured = {}

    async def fake_chat(messages, **kwargs):
        captured["user"] = messages[-1]["content"]
        return type("R", (), {"choices": [type("C", (), {
            "message": type("M", (), {"content": "修正稿[S1]。"})()
        })()]})()

    state = {
        "task_id": "t",
        "topic": "市场调研",
        "draft_content": "原稿[S1]。",
        "evidence": [source(excerpt="收入增长10%")],
        "review_comments": ["请修正单位"],
        "findings": [{
            "finding_id": "BIND-0001",
            "problem": "单位量级错误",
            "evidence": {"source_id": "S1", "quote": "收入增长10%"},
            "location": {"paragraph": 1, "sentence": 1},
            "expected": "改写为正确单位",
            "severity": "hard",
        }],
    }
    with patch.object(rewriter, "chat", new=fake_chat):
        asyncio.run(rewriter.rewriter_node(state))
    assert "BIND-0001" in captured["user"]
    assert "单位量级错误" in captured["user"]
    assert "改写为正确单位" in captured["user"]
