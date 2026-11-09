"""R05-02覆盖与去重正反例，真实文档/审核/返工边界。"""

import asyncio
import copy
import json
from unittest.mock import AsyncMock, patch
import pytest
from services.content_integrity import (
    guard_content,
    deduplicate_document,
    deduplicate_markdown,
    validate_review_content,
)
from services.report_document import build_document, render_document, fact_context
from tests.test_report_document import example
from tests.test_evidence import source
from tests.test_rewriter import FakeResp


def document():
    p, e, c = example()
    e = [source(excerpt="市场规模10亿元。同比增长20%。出口规模30亿元。")]
    c = fact_context("t", e)
    aliases = {
        c["registry"][key]["quote"]: alias for alias, key in c["aliases"].items()
    }
    p["sections"][1]["blocks"][0]["claims"][0]["facts"] = [aliases["市场规模10亿元。"]]
    p["sections"][0]["blocks"] = [dict(kind="analysis", text="研究范围说明。")]
    p["sections"][1]["blocks"].append(
        dict(
            kind="fact",
            text="同比增长20%。",
            claims=[dict(quote="同比增长20%。", facts=[aliases["同比增长20%。"]])],
        )
    )
    return p, e, build_document(p, "t", e, c)


def test_equal_fact_count_cannot_hide_lost_fact_or_analysis_premise():
    p, e, d = document()
    changed = copy.deepcopy(d)
    changed["sections"][1]["blocks"].pop(0)
    # 移除一个旧事实并加入新事实，数量相同也必须拒绝。
    fake = copy.deepcopy(changed["sections"][1]["blocks"][0])
    fake["claims"][0]["fact_ids"] = ["Fnew"]
    changed["sections"][1]["blocks"].append(fake)
    with pytest.raises(ValueError, match="覆盖"):
        guard_content("", "", before_document=d, after_document=changed)
    original = copy.deepcopy(d)
    original["sections"][1]["blocks"][0]["claims"][0]["premise_ids"] = ["Fp"]
    with pytest.raises(ValueError, match="覆盖"):
        guard_content("", "", before_document=original, after_document=d)


def test_safe_duplicate_removal_keeps_summary_and_unique_fact():
    p, e, d = document()
    p["sections"][1]["blocks"].append(copy.deepcopy(p["sections"][1]["blocks"][0]))
    d = build_document(p, "t", e, fact_context("t", e))
    result, removed = deduplicate_document(d)
    assert len(removed) == 1 and len(result["sections"][1]["blocks"]) == 2
    assert result["sections"][0] == d["sections"][0]
    assert result["revision"] == d["revision"] + 1
    assert "20%" in render_document(result)
    guard_content(
        render_document(d),
        render_document(result),
        before_document=d,
        after_document=result,
    )


def test_markdown_dedup_never_erases_summary_or_distinct_source():
    text = "这是关于市场竞争格局的完整说明，必须保留相关证据及原始引用[S1]。"
    body = (
        "# 报告\n\n## 执行摘要\n\n"
        + text
        + "\n\n## 正文\n\n"
        + text
        + "\n\n"
        + text
        + "\n\n"
        + text.replace("S1", "S2")
        + "\n\n## 结论与建议\n\n建议观察。"
    )
    result, removed = deduplicate_markdown(body)
    assert len(removed) == 1 and result.count(text) == 2 and "[S2]" in result
    from services.report_quality import repeated_passages

    summary = (
        "# 报告\n\n## 执行摘要\n"
        + text
        + text
        + "\n\n## 正文\n"
        + text
        + text
        + "\n\n## 结论与建议\n建议。"
    )
    assert not repeated_passages(summary)


def test_explicit_core_quote_and_valid_v2_bindings_cannot_be_deleted():
    with pytest.raises(ValueError, match="核心"):
        guard_content("核心回答。", "替代文字。", core_quotes=["核心回答。"])
    with pytest.raises(ValueError, match="覆盖"):
        guard_content(
            "有效事实。",
            "删除后正文。",
            before_bindings=[dict(text="有效事实。", fact_ids=["F1"])],
        )


def test_review_content_checks_both_quotes_questions_and_information_loss():
    review = dict(
        core_coverage=[
            dict(
                question="市场", covered=True, quote="规模结论。", reason="回答核心问题"
            )
        ],
        semantic_duplicates=[
            dict(
                first_quote="规模结论。",
                second_quote="规模结论重复。",
                information_loss=False,
                reason="同义复述",
            )
        ],
    )
    out = validate_review_content(review, "规模结论。规模结论重复。", ["市场"])
    assert out["core_status"] == "covered" and len(out["semantic_duplicates"]) == 1
    with pytest.raises(ValueError):
        validate_review_content(review, "伪造原句", ["市场"])
    with pytest.raises(ValueError):
        validate_review_content(review, "规模结论。规模结论重复。", ["另一个问题"])
    assert validate_review_content({}, "原文", ["市场"])["core_status"] == "unknown"
    forged = copy.deepcopy(review)
    forged["semantic_duplicates"][0]["second_quote"] = "不存在的另一处原句"
    with pytest.raises(ValueError):
        validate_review_content(forged, "规模结论。规模结论重复。", ["市场"])
    from pathlib import Path
    from services.review_contracts import ReviewResult

    defs = json.loads(
        (Path(__file__).resolve().parents[2] / "docs/schemas/contracts.json").read_text(
            encoding="utf-8"
        )
    )["$defs"]
    shape = ReviewResult.model_json_schema(ref_template="#/$defs/{model}")
    nested = shape.pop("$defs")
    assert defs["ReviewResult"] == shape and all(
        defs[k] == v for k, v in nested.items()
    )


def test_actual_action_cannot_swap_old_fact_for_different_fact():
    from services.document_rewrite import apply_actions, rewrite_context

    p, e, d = document()
    # 用来源中的另一个合法事实替换旧事实，总数量不变也必须拒绝。
    aliases = {
        fact_context("t", e)["registry"][key]["quote"]: alias
        for alias, key in fact_context("t", e)["aliases"].items()
    }
    payload = dict(
        actions=[
            dict(
                target="B2",
                operation="replace",
                value=dict(
                    kind="fact",
                    text="出口规模30亿元。",
                    claims=[
                        dict(quote="出口规模30亿元。", facts=[aliases["出口规模30亿元。"]])
                    ],
                ),
            )
        ]
    )
    out = apply_actions(d, payload, rewrite_context(d, e), e)
    assert out["document"] == d and out["rejected"]


def test_real_rewriter_deduplicates_without_model_and_keeps_audit():
    from agents import rewriter

    p, e, d = document()
    p["sections"][1]["blocks"].append(copy.deepcopy(p["sections"][1]["blocks"][0]))
    d = build_document(p, "t", e, fact_context("t", e))
    model = AsyncMock(side_effect=AssertionError("去重不需模型"))
    with patch.object(rewriter, "chat", new=model):
        out = asyncio.run(
            rewriter.rewriter_node(
                dict(
                    task_id="t",
                    report_pipeline_version=3,
                    report_document=d,
                    draft_content=render_document(d),
                    evidence=e,
                    quality_status="rejected",
                )
            )
        )
    assert out["review_history"][-1]["rewrite"]["mode"] == "deterministic_dedup"
    assert (
        out["draft_content"].count("市场规模10亿元。") == 1
    )  # 来源附录仅显示链接，正文保留一次
    model.assert_not_awaited()


def test_semantic_duplicate_requires_two_distinct_occurrences_and_covered_false():
    with pytest.raises(ValueError):
        validate_review_content(
            dict(
                semantic_duplicates=[
                    dict(
                        first_quote="原句。",
                        second_quote="原句。",
                        information_loss=False,
                        reason="重复",
                    )
                ]
            ),
            "原句。",
            [],
        )
    out = validate_review_content(
        dict(
            core_coverage=[
                dict(question="问题", covered=False, quote="原句。", reason="缺证据")
            ]
        ),
        "原句。",
        ["问题"],
    )
    assert out["core_status"] == "missing"
    body = "# 报告\n\n## 正文\n\n```\n代码正文\n\n必须保持代码中重复段落的原字节，不允许删除。\n\n必须保持代码中重复段落的原字节，不允许删除。\n```"
    assert deduplicate_markdown(body) == (body, [])


def test_real_review_validates_core_quote_and_persists_full_report_checks():
    from tests.test_review_validation import execute
    from db import database, task_repository as repo

    good = dict(
        passed=True,
        comments=[],
        core_coverage=[
            dict(question="市场", covered=True, quote="建议跟踪。", reason="覆盖建议")
        ],
    )
    bad = copy.deepcopy(good)
    bad["core_coverage"][0]["quote"] = "伪造核心原句"
    out, model = execute([bad, good])
    assert out["passed"] and model.await_count == 2
    checks = out["review_history"][-1]["content_checks"]
    assert (
        checks["core_status"] == "covered" and checks["review_scope"] == "full_report"
    )
    database.init_db()
    repo.create_task("t", "市场")
    repo.update_task("t", review_history=out["review_history"])
    assert repo.get_task("t")["review_history"][-1]["content_checks"] == checks
    missing = copy.deepcopy(good)
    missing["core_coverage"][0].update(covered=False, reason="需要补充核心证据")
    out, _ = execute([missing])
    assert out["quality_status"] == "rejected" and any(
        f["rule"] == "core_evidence_gap" for f in out["findings"]
    )


def test_actual_v3_graph_removes_duplicate_then_reviews_entire_report(offline_services):
    from graph.workflow import build_graph, _initial_state
    from memory.checkpointer import open_checkpointer

    p, e, d = document()
    p["sections"][1]["blocks"].append(copy.deepcopy(p["sections"][1]["blocks"][0]))
    d = build_document(p, "t", e, fact_context("t", e))
    offline_services["reviewer"].outputs = ['{"passed":true,"comments":[]}'] * 2
    saved = _initial_state("t", "市场") | dict(
        report_pipeline_version=3,
        report_document=d,
        draft_content=render_document(d),
        evidence=e,
    )

    async def scenario():
        async with open_checkpointer() as (cp, kind):
            graph = build_graph(checkpointer=cp)
            config = dict(configurable=dict(thread_id="t"))
            await graph.aupdate_state(config, saved, as_node="scheduler")
            out = await graph.ainvoke(None, config)
            assert out["quality_status"] == "passed"
            assert out["draft_content"].count("市场规模10亿元。") == 1
            assert out["review_history"][0]["rewrite"]["review_required"]

    asyncio.run(scenario())
    assert len(offline_services["reviewer"].calls) == 2
    assert not offline_services["rewriter"].calls


def test_premise_edge_cannot_disappear_even_if_fact_is_used_elsewhere():
    _, _, d = document()
    original = copy.deepcopy(d)
    original["sections"][1]["blocks"][0]["claims"][0]["premise_ids"] = list(
        d["sections"][1]["blocks"][1]["claims"][0]["fact_ids"]
    )
    with pytest.raises(ValueError, match="前提"):
        guard_content("", "", before_document=original, after_document=d)


def test_real_core_answer_rollback_keeps_rejection_trace():
    from agents import rewriter

    _, e, d = document()
    state = dict(
        task_id="t",
        report_pipeline_version=3,
        report_document=d,
        draft_content=render_document(d),
        evidence=e,
        quality_status="rejected",
        review_history=[
            dict(
                content_checks=dict(
                    core_coverage=[dict(covered=True, quote="建议继续观察。")]
                )
            )
        ],
    )
    response = FakeResp(
        json.dumps(
            dict(
                actions=[
                    dict(
                        target="B4",
                        operation="replace",
                        value=dict(kind="recommendation", text="删除原建议。"),
                    )
                ]
            )
        )
    )
    with patch.object(rewriter, "chat", new=AsyncMock(return_value=response)):
        out = asyncio.run(rewriter.rewriter_node(state))
    assert out["report_document"] == d
    receipt = out["review_history"][-1]["rewrite"]
    assert receipt["attempts"][-1]["code"] == "CONTENT_COVERAGE_REJECTED"
    assert not receipt["changed"]
