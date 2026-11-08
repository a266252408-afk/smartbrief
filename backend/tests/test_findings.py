"""审核 findings 四要素：问题、证据、位置、期望结果，以及模型证据校验。"""
from services.citations import check_citations
from services.findings import build_findings, finding_block, merge_findings
from tests.test_evidence import source


def findings_for(body, sources):
    result = check_citations("t", body, sources)
    return build_findings(body, sources, result["binding_issues"])


def test_miscitation_finding_has_evidence_location_and_action():
    sources = [
        source(excerpt="只有定性描述"),
        source(source_id="S2", url="https://example.invalid/b",
               excerpt="线上6K+市场保持正向增长"),
    ]
    findings = findings_for("线上6K+保持增长[S1]。", sources)
    finding = findings[0]
    assert finding["finding_id"] == "BIND-0001"
    assert finding["problem"] == "引用挂错：数字不在所引来源"
    assert finding["evidence"]["source_id"] == "S2"
    assert "6K" in finding["evidence"]["quote"]
    assert finding["location"] == {"paragraph": 1, "sentence": 1}
    assert "S2" in finding["expected"]
    assert finding["severity"] == "hard"


def test_unit_scale_finding_has_converted_expected_value():
    sources = [source(source_id="S11", url="https://example.invalid/a",
                      excerpt="The market grows from USD 46.39 billion in 2026 "
                              "to USD 74.98 billion by 2035.")]
    findings = findings_for("市场规模将从46.39亿美元增至74.98亿美元。[S11]", sources)
    finding = next(item for item in findings if item["rule"] == "unbound_number")
    assert finding["problem"] == "单位量级错误"
    assert "46.39" in finding["evidence"]["quote"]
    assert "463.9亿美元" in finding["expected"]
    assert "749.8亿美元" in finding["expected"]


def test_unsupported_number_finding_offers_delete_or_rebind():
    sources = [source(excerpt="其他资料")]
    findings = findings_for("销量为9999万台[S1]。", sources)
    finding = findings[0]
    assert finding["problem"] == "数字无来源支持"
    assert "删除" in finding["expected"]
    assert finding["evidence"]["source_id"] == "S1"


def test_merged_range_finding_lists_both_sources():
    sources = [
        source(source_id="S4", url="https://example.invalid/a",
               excerpt="2026年上半年内销2825.2万台，同比下降12.6%。"),
        source(source_id="S7", url="https://example.invalid/b",
               excerpt="2026年上半年国内销量2825万台，同比下跌13%。"),
    ]
    findings = findings_for("2026年上半年内销同比下滑12.6%至13%。[S4][S7]", sources)
    finding = next(item for item in findings if item["rule"] == "merged_fact_range")
    assert finding["finding_id"].startswith("RANGE-")
    assert {item["source_id"] for item in finding["evidence_sources"]} == {"S4", "S7"}
    assert "分别引用" in finding["expected"]


def test_finding_block_contains_four_elements():
    findings = findings_for(
        "线上6K+保持增长[S1]。",
        [source(excerpt="只有定性描述"),
         source(source_id="S2", url="https://example.invalid/b",
                excerpt="线上6K+市场保持正向增长")],
    )
    block = finding_block(findings)
    assert "BIND-0001" in block
    assert "第1段第1句" in block
    assert "S2" in block and "6K" in block
    assert "期望：" in block


def test_merge_keeps_deterministic_evidence_and_accepts_better_expected():
    sources = [source(source_id="S2", url="https://example.invalid/b",
                      excerpt="线上6K+市场保持正向增长")]
    deterministic = [{
        "finding_id": "BIND-0001", "rule": "unbound_number", "kind": "unbound_number",
        "severity": "hard", "problem": "引用挂错", "expected": "改引 S2",
        "evidence": {"source_id": "S2", "quote": "线上6K+市场保持正向增长"},
        "location": {"paragraph": 1, "sentence": 1},
    }]
    model = [{
        "finding_id": "BIND-0001", "problem": "引用挂错",
        "evidence": {"source_id": "S2", "quote": "线上6K+市场保持正向增长"},
        "location": {"paragraph": 1, "sentence": 1},
        "expected": "将 [S1] 改为 [S2]", "severity": "hard",
    }]
    merged, rejected = merge_findings(deterministic, model, sources)
    assert rejected == []
    assert merged[0]["evidence"] == deterministic[0]["evidence"]
    assert merged[0]["expected"] == "将 [S1] 改为 [S2]"


def test_merge_rejects_model_quote_not_in_source():
    sources = [source(source_id="S2", url="https://example.invalid/b",
                      excerpt="线上6K+市场保持正向增长")]
    model = [{
        "finding_id": "BIND-0009", "problem": "虚构证据",
        "evidence": {"source_id": "S2", "quote": "来源里没有这句话"},
        "location": {"paragraph": 1, "sentence": 1},
        "expected": "删除", "severity": "hard",
    }]
    merged, rejected = merge_findings([], model, sources)
    assert merged == []
    assert rejected and rejected[0]["finding_id"] == "BIND-0009"
