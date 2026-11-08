"""REFERENCE-AUDIT-REPAIR-01 Phase 1：确定性单位换算、段落兜底、结构修复与错误分类。"""
import asyncio
from unittest.mock import patch

import pytest

from agents import rewriter
from services.citations import (
    apply_unit_scale_fixes,
    check_citations,
    quantitative_tokens,
)
from services.report_quality import repair_structure
from tests.fakes import ScriptedLLM
from tests.test_evidence import source
from tests.test_reference_audit_fixtures import EBIKE, FRIDGE


def kinds(result, kind):
    return [item for item in result["binding_issues"] if item["kind"] == kind]


def test_composite_units_are_binding_tokens():
    assert quantitative_tokens("规模1.2万亿元，出货3.5千万台") == ["1.2万亿", "3.5千万"]
    result = check_citations(
        "t",
        "市场规模达到1.2万亿元[S1]。",
        [source(excerpt="2026年市场规模1.2万亿元")],
    )
    assert not kinds(result, "unbound_number")


def test_b4_uncited_sentence_uses_paragraph_source_and_reports_scale():
    body = (
        "该机构预计2026年市场规模为59.49亿美元，2035年达到161.46亿美元。"
        "另有来源给出544.4亿美元与1451亿美元。[S8]"
    )
    sources = [source(source_id="S8",
                      excerpt="2026 市場規模 $ 59.49 Bn，2036 預測 $ 161.46 Bn；"
                              "2025年市場規模為544.4億美元，2035年超過1,451億美元。")]
    result = check_citations("t", body, sources)
    issue = next(item for item in kinds(result, "unbound_number")
                 if item["uncited_sentence"])
    assert issue["numbers"] == ["59.49亿", "161.46亿"]
    assert issue["source_ids"] == ["S8"]
    assert issue["scale_matches"]["59.49亿"][0] == {"source": "S8", "factor": 10}


def test_b4_does_not_report_numbers_supported_by_a_source_cited_elsewhere():
    body = (
        "早期章节已说明保有量突破4.5亿辆[S12]。\n\n"
        "## 结论\n\n超4.5亿辆的庞大基数使内销增速承压。"
    )
    sources = [source(source_id="S12", url="https://example.invalid/b",
                      excerpt="中国电动两轮车社会保有量已突破4.5亿辆。")]
    result = check_citations("t", body, sources)
    assert not kinds(result, "unbound_number")


def test_b4_skips_when_paragraph_refs_are_unknown():
    body = "该机构预计市场规模为59.49亿美元。另见[S9]。"
    result = check_citations("t", body, [source(excerpt="其他资料")])
    assert not kinds(result, "unbound_number")
    assert any("S9" in error for error in result["errors"])


def test_b4_does_not_report_when_paragraph_refs_collectively_cover_tokens():
    body = "本段未标引用的句子提到增长10%与20%。另见[S1][S2]。"
    sources = [
        source(excerpt="收入增长10%"),
        source(source_id="S2", url="https://example.invalid/b", excerpt="销量增长20%"),
    ]
    result = check_citations("t", body, sources)
    assert not kinds(result, "unbound_number")


def test_apply_unit_scale_fixes_corrects_billion_and_bn():
    body = ("S11预计从46.39亿美元增至74.98亿美元。[S11] "
            "S8预计从59.49亿美元增至161.46亿美元。[S8]")
    sources = [
        source(source_id="S11", url="https://example.invalid/a",
               excerpt="The market grows from USD 46.39 billion in 2026 to USD 74.98 billion by 2035."),
        source(source_id="S8", url="https://example.invalid/b",
               excerpt="2026 市場規模 $ 59.49 Bn，2036 預測 $ 161.46 Bn"),
    ]
    result = apply_unit_scale_fixes(body, sources)
    assert "463.9亿美元" in result["body"]
    assert "749.8亿美元" in result["body"]
    assert "594.9亿美元" in result["body"]
    assert "1614.6亿美元" in result["body"]
    assert "46.39亿美元" not in result["body"]
    assert "59.49亿美元" not in result["body"]
    assert len(result["fixed"]) == 4


def test_apply_unit_scale_fixes_corrects_uncited_ebike_sentence():
    result = apply_unit_scale_fixes(EBIKE["draft_content"], EBIKE["evidence"])
    assert "594.9亿美元" in result["body"]
    assert "1614.6亿美元" in result["body"]
    assert "59.49亿美元" not in result["body"]
    assert "161.46亿美元" not in result["body"]
    # 繁体“億”的 544.4亿/1451亿本来就正确，不能被改。
    assert "544.4亿美元" in result["body"]
    assert "1451亿美元" in result["body"]


@pytest.mark.parametrize("body,excerpt", [
    ("从46.39亿美元增至74.98亿美元。[S11]", "USD 46.39 billion and 46.39 million"),
    ("市场规模为46.39亿美元。[S11]", "USD 46.40 billion"),
    ("从544.4亿美元增至1451亿美元。[S11]", "2025年市場規模為544.4億美元，2035年超過1,451億美元"),
])
def test_apply_unit_scale_fixes_refuses_unsafe_cases(body, excerpt):
    before = body
    result = apply_unit_scale_fixes(body, [source(source_id="S11", excerpt=excerpt)])
    assert result["body"] == before
    assert result["fixed"] == []


def test_error_categories_distinguish_miscitation_unit_scale_and_unsupported():
    miscitation = check_citations(
        "t", "线上6K+保持增长[S1]。",
        [source(excerpt="只有定性描述"),
         source(source_id="S2", url="https://example.invalid/b",
                excerpt="线上6K+市场保持正向增长")],
    )
    issue = kinds(miscitation, "unbound_number")[0]
    assert issue["category"] == "miscitation"
    assert issue["candidate_sources"] == ["S2"]
    assert "挂错" in issue["reason"]

    unit_scale = check_citations(
        "t", "市场规模将从46.39亿美元增长。[S11]",
        [source(source_id="S11", excerpt="from USD 46.39 billion in 2026")],
    )
    issue = kinds(unit_scale, "unbound_number")[0]
    assert issue["category"] == "unit_scale"
    assert "量级" in issue["reason"]
    assert issue["scale_matches"]["46.39亿"][0]["factor"] == 10

    unsupported = check_citations(
        "t", "销量为9999万台[S1]。",
        [source(excerpt="其他资料")],
    )
    issue = kinds(unsupported, "unbound_number")[0]
    assert issue["category"] == "unsupported"
    assert "未在任何来源" in issue["reason"]


def test_repair_structure_enforces_single_title_and_section_order():
    draft = (
        "## 第一章 市场\n\n内容A[S1]。\n\n"
        "# 另一份报告标题\n\n"
        "## 结论与建议\n\n结论[S1]。\n\n"
        "## 执行摘要\n\n摘要。"
    )
    repaired = repair_structure(draft, "2026电瓶车的市场调研")
    lines = repaired.splitlines()
    assert sum(1 for line in lines if line.startswith("# ")) == 1
    titles = [line[3:].strip() for line in lines if line.startswith("## ")]
    assert titles[0] == "执行摘要"
    assert titles[-1] == "结论与建议"
    assert "内容A[S1]。" in repaired
    assert "结论[S1]。" in repaired


def test_repair_structure_removes_duplicate_title():
    draft = "# 报告标题\n\n## 执行摘要\n\n摘要。\n\n# 报告标题\n\n## 结论与建议\n\n结论。"
    repaired = repair_structure(draft, "主题")
    assert repaired.count("# 报告标题") == 1
    assert repair_structure(repaired, "主题") == repaired


def test_rewriter_applies_unit_scale_fix_after_model_output():
    evidence = [source(source_id="S11", url="https://example.invalid/a",
                       excerpt="from USD 46.39 billion in 2026 to USD 74.98 billion by 2035")]
    model = ScriptedLLM(["市场规模将从46.39亿美元增至74.98亿美元。[S11]"])
    with patch.object(rewriter, "chat", new=model.chat):
        result = asyncio.run(rewriter.rewriter_node({
            "task_id": "t",
            "topic": "市场调研",
            "draft_content": "旧稿[S11]。",
            "evidence": evidence,
            "review_comments": ["请按来源原文换算单位"],
        }))
    assert "463.9亿美元" in result["draft_content"]
    assert "749.8亿美元" in result["draft_content"]
    assert "46.39亿美元" not in result["draft_content"]


def test_fridge_fixture_unit_fix_is_noop():
    """task_511 没有英文量级错误，单位修复不得改动正文。"""
    result = apply_unit_scale_fixes(FRIDGE["draft_content"], FRIDGE["evidence"])
    assert result["fixed"] == []
    assert result["body"] == FRIDGE["draft_content"]
