"""事实编号跨补研究稳定，正文引用由事实映射生成。"""
import pytest
import asyncio
import json
from unittest.mock import AsyncMock, patch
from services.fact_binding import BindingError, build_registry, render_bound_text, salvage_bound_text
from tests.test_evidence import source


def test_stable_id_and_task_isolation():
    a = source("t", excerpt="2026年第一季度规模10亿元。")
    registry = build_registry("t", [a])
    assert registry.items() <= build_registry("t", [a, source("t", source_id="S2", url="https://example.invalid/b")]).items()
    key = next(iter(registry))
    text, claims = render_bound_text(f"2026年第一季度规模10亿元[{key}]。", "t", registry)
    assert "[S1]" in text and claims[0]["fact_ids"] == [key]
    with pytest.raises(ValueError, match="归属"):
        render_bound_text(f"规模10亿元[{key}]。", "other", registry)


def test_wrong_period_object_and_unknown_id_rejected():
    registry = build_registry("t", [source("t", excerpt="2026年第一季度全球可穿戴设备规模10亿元。")])
    key = next(iter(registry))
    for sentence in (f"2026年上半年规模10亿元[{key}]。", f"全球智能手表规模10亿元[{key}]。", "规模10亿元[Funknown]。"):
        with pytest.raises(ValueError):
            render_bound_text(sentence, "t", registry)


def test_incomplete_tail_not_promoted_to_fact():
    registry = build_registry("t", [source("t", excerpt="苹果出货660万台。同比增长40.9")])
    assert len(registry) == 1
    with pytest.raises(ValueError):
        render_bound_text("苹果同比增长40.9%[S1]。", "t", registry)


def test_rewriter_uses_same_fact_registry_and_renders_candidate():
    from agents import rewriter
    from tests.test_rewriter_patch_flow import state, response
    from tests.test_rewriter import FakeResp
    s = state()
    s["report_pipeline_version"] = 2
    registry = build_registry("t", s["evidence"])
    key = next(iter(registry))
    payload = json.loads(response(s, f"市场规模10亿元[{key}]。").choices[0].message.content)
    with patch.object(rewriter, "chat", new=AsyncMock(return_value=FakeResp(json.dumps(payload)))):
        out = asyncio.run(rewriter.rewriter_node(s))
    assert "市场规模10亿元[S1]。" in out["draft_content"]
    assert out["claim_bindings"][0]["fact_ids"] == [key]
    assert out["review_history"][-1]["rewrite"]["fact_registry"][key]["quote"] == "市场规模10亿元。"


def test_missing_binding_unknown_scope_and_deleted_fact():
    registry = build_registry("t", [source("t", excerpt="市场规模10亿元。")])
    key = next(iter(registry))
    assert not build_registry("t", [source("t", source_deleted=True)])
    for body in ("市场规模10亿元。", "市场发生显著结构变化。",
                 f"2026年中国市场规模10亿元[{key}]。",
                 f"中国市场规模10亿元[{key}]。", f"智能手表市场规模10亿元[{key}]。"):
        with pytest.raises(ValueError):
            render_bound_text(body, "t", registry)


def test_two_facts_from_same_source_and_explicit_source_selection():
    registry = build_registry("t", [source("t", excerpt="市场规模10亿元。同比增长20%。"),
                                     source("t", source_id="S2", url="https://example.invalid/b",
                                            excerpt="市场规模10亿元。")])
    keys = [key for key, fact in registry.items() if fact["source_id"] == "S1"]
    out, _ = render_bound_text(f"市场规模10亿元、同比增长20%[{keys[0]}][{keys[1]}]。", "t", registry)
    assert "[S1]" in out
    second = next(key for key, fact in registry.items() if fact["source_id"] == "S2")
    out, _ = render_bound_text(f"市场规模10亿元[{second}]。", "t", registry)
    assert "[S2]" in out and "[S1]" not in out


def test_forecast_must_keep_its_role_and_actual_can_be_quoted_separately():
    registry = build_registry("t", [source("t", excerpt="2024年实际规模10亿元，预计2026年规模20亿元。")])
    key = next(iter(registry))
    render_bound_text(f"2024年实际规模10亿元[{key}]。", "t", registry)
    render_bound_text(f"预计2026年规模20亿元[{key}]。", "t", registry)
    with pytest.raises(ValueError, match="预测"):
        render_bound_text(f"2026年实际规模20亿元[{key}]。", "t", registry)
    with pytest.raises(ValueError, match="期间"):
        render_bound_text(f"2026年实际规模10亿元[{key}]。", "t", registry)


def test_literal_json_example_is_preserved_beside_bound_fact():
    registry = build_registry("t", [source("t", excerpt="市场规模10亿元。")])
    key = next(iter(registry))
    example = '```json\n{"amount": 10, "example_ref": "[Fexample]"}\n```'
    body, claims = render_bound_text(f"# 报告\n\n市场规模10亿元[{key}]。\n\n{example}", "t", registry)
    assert example in body and len(claims) == 1


def test_binding_error_exposes_sentence_for_targeted_repair():
    registry = build_registry("t", [source("t", excerpt="市场规模10亿元。")])
    key = next(iter(registry))
    with pytest.raises(BindingError) as info:
        render_bound_text(f"市场发生显著结构变化。市场规模10亿元[{key}]。", "t", registry)
    assert info.value.sentence == "市场发生显著结构变化。"
    assert str(info.value) == "定性声明缺少事实编号或明确的建议/推断标识"


def test_salvage_drops_unbound_sentence_and_keeps_bound_claims():
    registry = build_registry("t", [source("t", excerpt="市场规模10亿元。")])
    key = next(iter(registry))
    text, claims, dropped = salvage_bound_text(
        f"市场发生显著结构变化。市场规模10亿元[{key}]。", "t", registry)
    assert "市场发生显著结构变化。" not in text
    assert "市场规模10亿元[S1]。" in text
    assert dropped == ["市场发生显著结构变化。"]
    assert claims[0]["fact_ids"] == [key]


def test_salvage_stops_at_drop_limit_instead_of_returning_empty_shell():
    registry = build_registry("t", [source("t", excerpt="市场规模10亿元。")])
    with pytest.raises(BindingError):
        salvage_bound_text("无支持声明。无支持声明。无支持声明。", "t", registry, max_drops=2)


def test_heading_prefixed_source_sentence_binds_clean_claim():
    evidence = [source(
        "t", excerpt="## 有数据显示，2025年国内庭院花园整体市场规模突破2000亿元，"
                     "近三年年均增速稳定在10％以上，预计2026年将接近2600亿元。")]
    registry = build_registry("t", evidence)
    key = next(iter(registry))
    assert registry[key]["quote"].startswith("有数据显示")
    body = ("相关测算显示，2025年国内庭院花园整体市场规模突破2000亿元，"
            f"近三年年均增速稳定在10％以上，预计2026年将接近2600亿元[{key}]。")
    text, claims = render_bound_text(body, "t", registry)
    assert "[S1]" in text and "[F" not in text
    assert claims[0]["fact_ids"] == [key]


def test_single_edit_fact_id_is_corrected():
    registry = build_registry("t", [source("t", excerpt="市场规模10亿元。")])
    key = next(iter(registry))
    wrong = key + "a"
    text, claims = render_bound_text(f"市场规模10亿元[{wrong}]。", "t", registry)
    assert "[S1]" in text and wrong not in text
    assert claims[0]["fact_ids"] == [key]
    assert claims[0]["fact_id_corrections"] == {wrong: key}


def test_ambiguous_near_miss_is_not_corrected():
    from services.fact_binding import _resolve_fact_key
    assert _resolve_fact_key("Fabx", {"Fabc": {}, "Fabd": {}}) == "Fabx"


def test_salvage_keeps_heading_when_dropping_unbound_claim():
    registry = build_registry("t", [source("t", excerpt="市场规模10亿元。")])
    key = next(iter(registry))
    body = f"## 市场规模与增长趋势\n市场前景广阔。\n\n市场规模10亿元[{key}]。"
    text, claims, dropped = salvage_bound_text(body, "t", registry)
    assert "## 市场规模与增长趋势" in text
    assert "市场前景广阔。" not in text
    assert "市场规模10亿元[S1]。" in text
    assert dropped == ["## 市场规模与增长趋势\n市场前景广阔。"]


def test_heading_only_fact_quote_is_normalized():
    registry = build_registry("t", [source("t", excerpt="## 市场规模10亿元。")])
    assert next(iter(registry.values()))["quote"] == "市场规模10亿元。"


def test_english_annual_source_matches_chinese_annual_claim():
    # 声明写“年度”，英文事实原文写 "annual installations"，期间口径应视为一致。
    registry = build_registry("t", [source(excerpt=(
        "2025 was a record-setting year: annual installations surpassed the 600,000-unit "
        "milestone for the first time."))])
    key = next(iter(registry))
    render_bound_text(f"2025年工业机器人年度新增安装量超过60万台[{key}]。", "t", registry)


def test_fact_reference_with_inner_spaces_is_accepted():
    registry = build_registry("t", [source("t", excerpt="市场规模10亿元。")])
    key = next(iter(registry))
    text, claims = render_bound_text(f"市场规模10亿元[ {key} ]。", "t", registry)
    assert "[S1]" in text and claims[0]["fact_ids"] == [key]


def test_salvage_covers_full_report_scale_beyond_previous_limit():
    registry = build_registry("t", [source("t", excerpt="市场规模10亿元。")])
    key = next(iter(registry))
    body = "\n\n".join(["市场发生显著结构变化。"] * 15 + [f"市场规模10亿元[{key}]。"])
    text, claims, dropped = salvage_bound_text(body, "t", registry)
    assert len(dropped) == 15
    assert "市场规模10亿元[S1]。" in text
    assert claims[0]["fact_ids"] == [key]


@pytest.mark.parametrize('text', ['[建议] 核心零部件供应商需加速适配验证。',
                                 '【推断】市场将延续增长。',
                                 '（资料说明）部分数据暂缺。'])
def test_bracketed_advisory_markers_need_no_fact_id(text):
    assert render_bound_text(text, 't', {})[0] == text


@pytest.mark.parametrize('text', ['记者调研发现，行业竞争逻辑尚未改变。',
                                 '调研发现，内容生产成本仍在下降。'])
def test_journalistic_observation_prefix_needs_no_fact_id(text):
    assert render_bound_text(text, 't', {})[0] == text


def test_qualifier_insertion_with_same_source_is_preserved():
    previous = '头部企业综合毛利率降至21.6%[S1]。'
    current = '部分头部企业综合毛利率降至21.6%[S1]。'
    assert render_bound_text(current, 't', {}, previous=previous)[0] == current


def test_number_change_with_same_source_still_needs_fact_id():
    previous = '头部企业综合毛利率降至21.6%[S1]。'
    with pytest.raises(BindingError):
        render_bound_text('头部企业综合毛利率降至25.6%[S1]。', 't', {}, previous=previous)


def test_negation_change_in_fragment_still_needs_fact_id():
    previous = '行业未出现增长，市场保持平稳[S1]。'
    with pytest.raises(BindingError):
        render_bound_text('出现增长[S1]。', 't', {}, previous=previous)


def test_wrong_letter_fact_reference_is_corrected_to_registry_key():
    registry = build_registry("t", [source("t", excerpt="市场规模10亿元。")])
    key = next(iter(registry))
    text, claims = render_bound_text(f"市场规模10亿元[C{key[1:]}]。", "t", registry)
    assert "[S1]" in text and "C" + key[1:] not in text
    assert claims[0]["fact_ids"] == [key]


def test_truncated_fact_reference_is_removed_instead_of_leaking():
    registry = build_registry("t", [source("t", excerpt="市场规模10亿元。")])
    key = next(iter(registry))
    text, _ = render_bound_text(f"建议关注渠道建设[C{key[1:20]}]。", "t", registry)
    assert "[C" not in text and "[F" not in text
    with pytest.raises(BindingError, match="定量声明缺少事实编号"):
        render_bound_text(f"市场规模10亿元[C{key[1:20]}]。", "t", registry)


def test_salvage_can_locate_sentence_carrying_malformed_reference():
    registry = build_registry("t", [source("t", excerpt="市场规模10亿元。")])
    key = next(iter(registry))
    bad = f"市场规模99亿元[C{key[1:20]}]。"
    body = f"{bad}\n\n市场规模10亿元[{key}]。"
    text, claims, dropped = salvage_bound_text(body, "t", registry)
    assert bad in dropped
    assert "市场规模10亿元[S1]。" in text and "[C" not in text


def test_salvage_keeps_title_merged_with_bare_numbered_heading():
    registry = build_registry("t", [source("t", excerpt="市场规模10亿元。")])
    key = next(iter(registry))
    body = f"### 3.2\n物流前置与供应链基建升级\n\n市场规模10亿元[{key}]。"
    text, claims, dropped = salvage_bound_text(body, "t", registry)
    assert "### 3.2 物流前置与供应链基建升级" in text
    assert "市场规模10亿元[S1]。" in text


def test_salvage_merges_separate_title_line_back_into_bare_heading():
    registry = build_registry("t", [source("t", excerpt="市场规模10亿元。")])
    key = next(iter(registry))
    body = f"### 3.2\n\n物流前置与供应链基建升级\n\n市场规模10亿元[{key}]。"
    text, claims, dropped = salvage_bound_text(body, "t", registry)
    assert "### 3.2 物流前置与供应链基建升级" in text
    assert "### 3.2\n" not in text


@pytest.mark.parametrize('text', ['本调研基于公开监测数据与产业跟踪报告，系统评估市场规模。',
                                 '本报告围绕行业趋势展开分析。',
                                 '本文采用公开资料梳理竞争格局。'])
def test_methodology_sentences_need_no_fact_id(text):
    assert render_bound_text(text, 't', {})[0] == text


def test_plain_claim_starting_with_report_is_still_rejected():
    with pytest.raises(BindingError):
        render_bound_text('本报告认为行业已进入成熟期。', 't', {})
