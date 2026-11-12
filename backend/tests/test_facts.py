"""结构化事实表：年份、量级、实绩/预测拆分与区间合并检测。"""
import json
from pathlib import Path

import pytest

from services.facts import extract_facts, fact_block, fact_table, merged_range_issues
from tests.test_evidence import source

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "reference_audit"
EBIKE = json.loads((FIXTURE_DIR / "ebike.json").read_text(encoding="utf-8"))
EVIDENCE = {item["source_id"]: item for item in EBIKE["evidence"]}


def find_fact(facts, number, unit_kind=None):
    return next(
        fact for fact in facts
        if fact["value"] == number and (unit_kind is None or fact["unit_kind"] == unit_kind)
    )


def test_extracts_market_size_with_year_and_role_from_s11():
    facts = extract_facts([EVIDENCE["S11"]])
    first = find_fact(facts, "46.39")
    assert first["source_id"] == "S11"
    assert first["metric"] == "市场规模"
    assert first["unit"] == "billion"
    assert first["currency"] == "USD"
    assert first["year"] == 2026
    assert first["role"] == "forecast"
    assert first["normalized"] == pytest.approx(46.39e9)

    second = find_fact(facts, "74.98")
    assert second["year"] == 2035
    assert second["role"] == "forecast"


def test_extracts_2036_not_2035_from_s8_table():
    facts = extract_facts([EVIDENCE["S8"]])
    first = find_fact(facts, "59.49")
    assert first["year"] == 2026
    assert first["unit"] in {"Bn", "bn"}
    second = find_fact(facts, "161.46")
    assert second["year"] == 2036
    assert {fact["value"] for fact in facts} & {"2035", "2036"} == set()


def test_extracts_actual_and_forecast_from_same_sentence():
    facts = extract_facts([EVIDENCE["S8"]])
    actual = find_fact(facts, "544.4")
    forecast = find_fact(facts, "1451")
    assert actual["year"] == 2025
    assert actual["role"] == "actual"
    assert forecast["year"] == 2035
    assert forecast["role"] == "forecast"


def test_actual_clause_does_not_inherit_later_forecast_or_year():
    facts = extract_facts([source(
        excerpt="2023年产业总产值已达4,860亿元，同比增长12.7%，预计到2026年将突破7,200亿元。")])
    by_value = {fact["value"]: fact for fact in facts}
    assert by_value["4860"]["year"] == 2023
    assert by_value["4860"]["role"] == "actual"
    assert by_value["12.7"]["year"] == 2023
    assert by_value["12.7"]["role"] == "actual"
    assert by_value["7200"]["year"] == 2026
    assert by_value["7200"]["role"] == "forecast"


def test_conflicts_group_same_metric_year_and_ignore_rounding_difference():
    table = fact_table([EVIDENCE["S4"], EVIDENCE["S7"]])
    conflict_values = {
        tuple(fact["value"] for fact in conflict["facts"])
        for conflict in table["conflicts"]
    }
    assert ("12.6", "13") in conflict_values
    # 2825.2 万与 2825 万只差 0.007%，不算口径冲突。
    assert ("2825.2", "2825") not in conflict_values


def test_fact_block_lists_rows_separately_and_marks_conflicts():
    block = fact_block(fact_table([EVIDENCE["S8"]]))
    assert "S8" in block and "161.46" in block and "2036" in block
    assert "59.49" in block and "2026" in block
    assert "预测" in block
    assert "不得" in block

    conflict_block = fact_block(fact_table([EVIDENCE["S4"], EVIDENCE["S7"]]))
    assert "冲突" in conflict_block
    assert "12.6" in conflict_block and "13" in conflict_block


def test_merged_range_detects_two_sources():
    facts = fact_table([EVIDENCE["S4"], EVIDENCE["S7"]])["facts"]
    issues = merged_range_issues("2026年上半年内销同比下滑12.6%至13%。", facts)
    assert len(issues) == 1
    issue = issues[0]
    assert issue["kind"] == "merged_fact_range"
    assert issue["severity"] == "hard"
    assert issue["numbers"] == ["12.6%", "13%"]
    assert issue["source_ids"] == ["S4", "S7"]


def test_merged_range_allows_same_source_genuine_range():
    evidence = [source(excerpt="2025年价格带4,000-8,000元，其中高端增长10%-20%。")]
    facts = fact_table(evidence)["facts"]
    assert not merged_range_issues("高端增长10%至20%。", facts)


def test_merged_range_allows_range_present_in_cited_source_sentence():
    evidence = [
        source(source_id="S1", excerpt="外卖佣金率15%。"),
        source(source_id="S2", excerpt="外卖佣金率20%。"),
        source(source_id="S3", excerpt="外卖佣金率15%-20%，而自提近乎零成本。"),
    ]
    two_sources = fact_table(evidence[:2])["facts"]
    assert merged_range_issues("外卖佣金率15-20%，而自提近乎零成本。[S1]", two_sources)
    facts = fact_table(evidence)["facts"]
    assert not merged_range_issues("外卖佣金率15-20%，而自提近乎零成本。[S3]", facts)


def test_check_citations_reports_merged_range():
    from services.citations import check_citations
    result = check_citations(
        "t",
        "2026年上半年内销同比下滑12.6%至13%。[S4]",
        [source(source_id="S4", url="https://example.invalid/a",
                excerpt="2026年上半年内销2825.2万台，同比下降12.6%。"),
         source(source_id="S7", url="https://example.invalid/b",
                excerpt="2026年上半年国内销量2825万台，同比下跌13%。")],
    )
    issue = next(item for item in result["binding_issues"]
                 if item["kind"] == "merged_fact_range")
    assert issue["source_ids"] == ["S4", "S7"]


def test_fact_extraction_skips_dates_years_and_standard_codes():
    evidence = [source(excerpt="截至2026年10月31日，GB 12021.2-2025 标准实施，市场规模1.2万亿元。")]
    facts = extract_facts(evidence)
    assert [fact["value"] for fact in facts] == ["1.2"]
    assert facts[0]["unit"] == "万亿元"
    assert facts[0]["metric"] == "市场规模"


def test_fact_table_skips_deleted_sources():
    deleted = dict(EVIDENCE["S11"], source_deleted=True)
    assert fact_table([deleted])["facts"] == []


def test_fact_without_year_is_kept_without_guessing():
    facts = extract_facts([source(excerpt="市场规模100亿元。")])
    assert facts[0]["year"] is None
    assert facts[0]["unit"] == "亿元"


def test_fact_block_empty_and_budget():
    assert fact_block({"facts": []}) == ""
    block = fact_block(fact_table([EVIDENCE["S8"]]), budget=200)
    assert len(block) <= 200
    assert "结构化事实表" in block


def test_merged_range_skips_year_ranges_and_unknown_facts():
    facts = fact_table([EVIDENCE["S4"], EVIDENCE["S7"]])["facts"]
    assert not merged_range_issues("2026-2035年市场规模", facts)
    assert not merged_range_issues("第1-2季度", facts)
    assert not merged_range_issues("增长10%至20%", facts)


def test_merged_range_detects_two_sources_for_counts():
    evidence = [
        source(source_id="S1", url="https://example.invalid/a", excerpt="2026年销量100万台。"),
        source(source_id="S2", url="https://example.invalid/b", excerpt="2026年销量200万台。"),
    ]
    facts = fact_table(evidence)["facts"]
    issues = merged_range_issues("2026年销量100万至200万台。", facts)
    assert issues
    assert issues[0]["source_ids"] == ["S1", "S2"]


class _Resp:
    def __init__(self, content):
        self.choices = [type("C", (), {"message": type("M", (), {"content": content})()})()]


def test_reviewer_rejects_merged_range_as_claim_unsupported():
    import asyncio
    from unittest.mock import AsyncMock, patch

    from agents import reviewer

    state = {
        "task_id": "t",
        "draft_content": "2026年上半年内销同比下滑12.6%至13%。[S4][S7]",
        "evidence": [
            source(source_id="S4", url="https://example.invalid/a",
                   excerpt="2026年上半年内销2825.2万台，同比下降12.6%。"),
            source(source_id="S7", url="https://example.invalid/b",
                   excerpt="2026年上半年国内销量2825万台，同比下跌13%。"),
        ],
    }
    with patch.object(reviewer, "chat",
                      new=AsyncMock(return_value=_Resp('{"passed": true, "comments": []}'))):
        out = asyncio.run(reviewer.reviewer_node(state))
    assert out["passed"] is False
    assert out["quality_status"] == "rejected"
    assert out["error_code"] == "CLAIM_UNSUPPORTED"
    assert any("区间" in comment for comment in out["review_comments"])


def test_rewriter_prompt_mentions_merged_range():
    import asyncio
    from unittest.mock import patch

    from agents import rewriter

    captured = {}

    async def fake_chat(messages, **kwargs):
        captured["user"] = messages[-1]["content"]
        return _Resp("请分别引用[S4][S7]。")

    state = {
        "task_id": "t",
        "topic": "2026电瓶车市场调研",
        "draft_content": "2026年上半年内销同比下滑12.6%至13%。[S4][S7]",
        "evidence": [
            source(source_id="S4", url="https://example.invalid/a",
                   excerpt="2026年上半年内销2825.2万台，同比下降12.6%。"),
            source(source_id="S7", url="https://example.invalid/b",
                   excerpt="2026年上半年国内销量2825万台，同比下跌13%。"),
        ],
        "review_comments": ["请修正区间合并"],
    }
    with patch.object(rewriter, "chat", new=fake_chat):
        asyncio.run(rewriter.rewriter_node(state))
    assert "被合并成区间" in captured["user"]
    assert "分别引用" in captured["user"]


def test_english_thousands_separator_does_not_split_year_clause():
    # 千分位逗号不是分句边界：数字后的 "in 2024" 必须优先于标题里的 2025。
    facts = extract_facts([source(excerpt=(
        "Frankfurt, Sep 25, 2025 — The new World Robotics 2025 statistics on industrial robots "
        "showed 542,000 robots installed in 2024."))])
    assert find_fact(facts, "542000")["year"] == 2024


def test_english_units_and_rate_take_trailing_year_after_thousands_separator():
    facts = extract_facts([source(excerpt=(
        "Frankfurt, Sep 30, 2026 — Global shipments of professional service robots increased by 24% "
        "to almost 250,000 units in 2025."))])
    assert find_fact(facts, "250000")["year"] == 2025
    assert find_fact(facts, "24")["year"] == 2025


def test_bullet_table_rows_do_not_inherit_forecast_role_from_later_cagr():
    facts = extract_facts([source(excerpt=(
        "Worldwide annual installations of industrial robots:\n"
        "• 2025: 603,307 units (+11%) • 2026: 655,000 units (+9%) "
        "• 2029: 806,000 units (+7% CAGR from 2026 to 2029)"))])
    assert find_fact(facts, "603307")["role"] != "forecast"


def test_press_release_dateline_year_is_not_used_as_data_year():
    facts = extract_facts([source(excerpt=(
        "Frankfurt, Oct 08, 2024 — Sales of professional service robots increased by 30% worldwide."))])
    assert find_fact(facts, "30")["year"] is None


def test_multiline_press_release_does_not_borrow_report_year_for_growth():
    facts = extract_facts([source(excerpt=(
        "Sales of Service Robots up 30% Worldwide\n\n"
        "World Robotics 2024 Service Robots report released\n\n"
        "Frankfurt, Oct 08, 2024 — Sales of professional service robots increased by 30% worldwide. "
        "More than 205,000 units were registered by IFR's statistics department in 2023."))])
    assert find_fact(facts, "205000")["year"] == 2023
    assert all(fact["year"] != 2024 for fact in facts if fact["value"] == "30")
