"""REFERENCE-AUDIT-REPAIR-01 Phase 0：两起真实失败的离线回归夹具。

夹具来源见 ``tests/fixtures/reference_audit/README.md``。本文件只读取固定 JSON，
不调用模型、搜索或网络；Phase 1 起在同一夹具上追加正面断言。
"""
import json
import re
from pathlib import Path

import pytest

from services.citations import REFERENCE, check_citations, rebind_citations, split_sentences
from services.numbers import token_matches_text

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "reference_audit"


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURE_DIR / f"{name}.json").read_text(encoding="utf-8"))


FRIDGE = load_fixture("smart_fridge")
EBIKE = load_fixture("ebike")


def replay(fixture: dict) -> dict:
    return check_citations(
        fixture["source_task"]["task_id"],
        fixture["draft_content"],
        fixture["evidence"],
    )


@pytest.mark.parametrize("fixture", [FRIDGE, EBIKE], ids=["smart_fridge", "ebike"])
def test_fixture_integrity(fixture):
    assert fixture["schema_version"] == 1
    assert fixture["evidence"]
    assert fixture["draft_content"].strip()
    assert fixture["final_report"].strip()
    assert fixture["recorded"]["quality_status"] == "rejected"
    assert fixture["recorded"]["forced_output"] == 1
    assert fixture["known_cases"]
    assert fixture["known_gaps"]
    task_id = fixture["source_task"]["task_id"]
    assert all(item["task_id"] == task_id for item in fixture["evidence"])
    assert all(item["excerpt"].strip() for item in fixture["evidence"])

    result = replay(fixture)
    assert not any("来源记录无效或跨任务" in error for error in result["errors"])


def test_smart_fridge_recorded_binding_errors_are_miscitations_not_fabrications():
    """17 条“数字找不到”全部能在候选来源中找到，不能当成编造。"""
    known = {item["source_id"]: item for item in FRIDGE["evidence"]}
    miscitations = [case for case in FRIDGE["known_cases"] if case["kind"] == "miscitation"]
    assert len(miscitations) == 17
    for case in miscitations:
        assert case["in_cited_excerpt"] == []
        assert case["candidate_sources"]
        assert case["cited"] not in case["candidate_sources"]
        assert all(
            token_matches_text(number, known[source_id]["excerpt"])
            for source_id in case["candidate_sources"]
            for number in case["numbers"]
        )


def test_smart_fridge_resolved_false_positives_are_not_reported_again():
    """旧版分句偏移造成的 16 条误报，当前检查器不得再以原被引来源报出。"""
    result = replay(FRIDGE)
    flags = [issue for issue in result["binding_issues"] if issue["kind"] == "unbound_number"]
    resolved = [
        case for case in FRIDGE["known_cases"]
        if case["kind"] == "miscitation" and case["baseline_status"] == "resolved_false_positive"
    ]
    assert len(resolved) == 16
    for case in resolved:
        assert not any(
            case["cited"] in (issue.get("source_ids") or [])
            and set(issue.get("numbers") or []) & set(case["numbers"])
            for issue in flags
        )


def test_smart_fridge_residual_miscitation_is_rebound_deterministically():
    """核心结论段的 CR5 88% 仍挂在 S14，可确定性重挂到 S31，且只改这一段。"""
    result = replay(FRIDGE)
    issue = next(
        issue for issue in result["binding_issues"]
        if issue["kind"] == "unbound_number" and issue["numbers"] == ["88%"]
    )
    assert issue["source_ids"] == ["S14"]
    assert issue["candidate_sources"] == ["S31"]

    rebound = rebind_citations(FRIDGE["draft_content"], FRIDGE["evidence"])
    assert rebound["rebound"] == 1
    assert rebound["ambiguous"] == []
    assert rebound["unmatched"] == []

    before = re.split(r"\n\s*\n", FRIDGE["draft_content"])
    after = re.split(r"\n\s*\n", rebound["body"])
    assert len(before) == len(after)
    changed = [index for index, (old, new) in enumerate(zip(before, after)) if old != new]
    assert len(changed) == 1
    assert "利润池[S31]。" in after[changed[0]]
    assert "[S14]" in after[changed[0]]  # 同一段前面的 S14 引用保持不变


def test_ebike_traditional_numerals_are_not_a_binding_error():
    """S8 原文用繁体“億”和千分位“1,451億”，不得再误判为数字找不到。"""
    cited = next(item for item in EBIKE["known_cases"]
                 if item["kind"] == "false_positive_traditional_numerals")
    known = {item["source_id"]: item for item in EBIKE["evidence"]}
    assert all(token_matches_text(number, known["S8"]["excerpt"]) for number in cited["numbers"])

    result = replay(EBIKE)
    assert not any(
        issue["kind"] == "unbound_number"
        and set(issue.get("numbers") or []) & set(cited["numbers"])
        for issue in result["binding_issues"]
    )


def test_ebike_unit_scale_error_is_reported_and_not_autofixed():
    """46.39 billion 被写成 46.39亿：检查器只报 10 倍提示，rebind 不改数字。

    真正的单位改写由 ``apply_unit_scale_fixes`` 单独执行；本测试固定检查层与
    重挂层的行为边界。
    """
    case = next(item for item in EBIKE["known_cases"]
                if item["kind"] == "unit_scale_error")
    result = replay(EBIKE)
    issue = next(
        issue for issue in result["binding_issues"]
        if issue["kind"] == "unbound_number" and issue["numbers"] == case["numbers"]
    )
    assert issue["source_ids"] == ["S11"]
    assert issue["candidate_sources"] == []
    assert issue["scale_matches"]["46.39亿"][0] == {"source": "S11", "factor": 10}
    assert issue["scale_matches"]["74.98亿"][0] == {"source": "S11", "factor": 10}

    rebound = rebind_citations(EBIKE["draft_content"], EBIKE["evidence"])
    assert "46.39亿美元" in rebound["body"]
    assert "74.98亿美元" in rebound["body"]


def test_ebike_uncited_number_sentence_is_detected_with_scale_hint():
    """B4：59.49亿美元/161.46亿美元所在句子没有 [S8]，但同段有 S8。

    段落级兜底应检出这两个数字，并给出相对 S8 的 10 倍量级提示；
    同句的 10.5% 在 S8 中能找到，不能再混入 unbound_numbers。
    """
    paragraph = next(
        para for para in re.split(r"\n\s*\n", EBIKE["draft_content"])
        if "59.49亿美元" in para
    )
    sentence = next(
        sentence for sentence in split_sentences(paragraph)
        if "59.49亿美元" in sentence
    )
    assert not REFERENCE.search(sentence)

    result = replay(EBIKE)
    issue = next(
        issue for issue in result["binding_issues"]
        if issue["kind"] == "unbound_number" and "59.49亿" in issue["numbers"]
    )
    assert issue["uncited_sentence"] is True
    assert issue["category"] == "unit_scale"
    assert issue["numbers"] == ["59.49亿", "161.46亿"]
    assert "S8" in issue["source_ids"]
    assert issue["scale_matches"]["59.49亿"][0] == {"source": "S8", "factor": 10}
    assert issue["scale_matches"]["161.46亿"][0] == {"source": "S8", "factor": 10}
