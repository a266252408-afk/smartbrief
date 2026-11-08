"""Writer 有界自检：确定性自动修复、一次定点修复、变差回滚。"""
import asyncio
from unittest.mock import AsyncMock, patch

from agents import react_writer
from tests.fakes import ScriptedLLM
from tests.test_evidence import source


def run(coro):
    return asyncio.run(coro)


def writer_run(task, outputs, evidence):
    logs = []
    model = ScriptedLLM(outputs)
    with (
        patch.object(react_writer, "_retrieve_terms", new=AsyncMock(return_value=[])),
        patch.object(react_writer, "chat", new=model.chat),
    ):
        content = run(react_writer.react_writer_run(task, 1, logs, {}, evidence=evidence))
    return content, logs, model


def test_self_check_fixes_unit_scale_without_model_retry():
    evidence = [source(source_id="S11", url="https://example.invalid/a",
                       excerpt="The market grows from USD 46.39 billion in 2026 "
                               "to USD 74.98 billion by 2035.")]
    content, logs, model = writer_run(
        "撰写市场规模章节",
        ["市场规模将从46.39亿美元增至74.98亿美元。[S11]"],
        evidence,
    )
    assert "463.9亿美元" in content and "749.8亿美元" in content
    assert len(model.calls) == 1
    assert any("已确定性单位换算 2 处" in log["content"] for log in logs)
    assert not any("写作自检" in log["content"] for log in logs)


def test_self_check_adopts_better_candidate_for_merged_range():
    evidence = [
        source(source_id="S4", url="https://example.invalid/a",
               excerpt="2026年上半年内销2825.2万台，同比下降12.6%。"),
        source(source_id="S7", url="https://example.invalid/b",
               excerpt="2026年上半年国内销量2825万台，同比下跌13%。"),
    ]
    bad = "2026年上半年内销同比下滑12.6%至13%。[S4][S7]"
    good = "2026年上半年内销同比下滑12.6%[S4]；另一口径为下降13%[S7]。"
    content, logs, model = writer_run("撰写市场销量章节", [bad, good], evidence)
    assert content == good
    assert len(model.calls) == 2
    assert any("已采用修复稿" in log["content"] for log in logs)


def test_self_check_rolls_back_worse_candidate():
    evidence = [
        source(source_id="S4", url="https://example.invalid/a",
               excerpt="2026年上半年内销2825.2万台，同比下降12.6%。"),
        source(source_id="S7", url="https://example.invalid/b",
               excerpt="2026年上半年国内销量2825万台，同比下跌13%。"),
    ]
    first = "2026年上半年内销同比下滑12.6%至13%。[S4][S7]"
    second = "2026年上半年内销同比下滑12.6%～13%。[S4][S7]"
    content, logs, model = writer_run("撰写市场销量章节", [first, second], evidence)
    assert content == first
    assert len(model.calls) == 2
    assert any("已回滚原稿" in log["content"] for log in logs)


def test_self_check_not_called_without_mechanical_findings():
    content, logs, model = writer_run(
        "撰写市场规模章节", ["## 市场规模\n章节内容"], []
    )
    assert content == "## 市场规模\n章节内容"
    assert len(model.calls) == 1
    assert not any("写作自检" in log["content"] for log in logs)


def test_self_check_repairs_full_report_structure():
    evidence = [source(excerpt="2026年市场规模增长10%")]
    incomplete = "2026年市场规模增长10%[S1]。"
    complete = ("# 2026年市场报告\n\n## 执行摘要\n\n摘要。\n\n"
                "## 结论与建议\n\n2026年市场规模增长10%[S1]。")
    content, logs, model = writer_run(
        "输出报告标题、执行摘要、结论与建议", [incomplete, complete], evidence
    )
    assert content.startswith("# 2026年市场报告")
    assert "结论与建议" in content
    assert len(model.calls) == 2
    assert any("3→0" in log["content"] for log in logs)


def test_self_check_skips_structure_for_body_only_task():
    evidence = [source(excerpt="2026年市场规模增长10%")]
    content, logs, model = writer_run(
        "撰写完整报告；仅撰写主体章节，不输出报告标题、执行摘要或结论与建议。",
        ["2026年市场规模增长10%[S1]。"],
        evidence,
    )
    assert content == "2026年市场规模增长10%[S1]。"
    assert len(model.calls) == 1
    assert not any("写作自检" in log["content"] for log in logs)


def test_self_check_repair_failure_keeps_original():
    evidence = [
        source(source_id="S4", url="https://example.invalid/a",
               excerpt="2026年上半年内销2825.2万台，同比下降12.6%。"),
        source(source_id="S7", url="https://example.invalid/b",
               excerpt="2026年上半年国内销量2825万台，同比下跌13%。"),
    ]
    bad = "2026年上半年内销同比下滑12.6%至13%。[S4][S7]"
    content, logs, model = writer_run(
        "撰写市场销量章节", [bad, RuntimeError("boom")], evidence
    )
    assert content == bad
    assert len(model.calls) == 2
    assert any("修复失败，保留原稿" in log["content"] for log in logs)
