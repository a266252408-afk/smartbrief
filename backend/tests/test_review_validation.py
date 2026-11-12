"""R04-01 不可信审核意见不能驱动正文修复或默认放行。"""

import asyncio
import json
from unittest.mock import AsyncMock, patch
import pytest
from services.review_validation import finding_errors, repair_report_locations, repair_source_finding_locations
from agents import reviewer
from tests.test_evidence import source
from tests.test_rewriter import FakeResp


BODY = "# 报告\n\n## 执行摘要\n摘要说明。\n\n## 正文\n市场规模10亿元[S1]。\n\n## 结论与建议\n建议跟踪。"


def finding(**change):
    base = dict(
        finding_id="M1",
        problem="需要核对",
        expected="依据来源修正",
        severity="hard",
        evidence=dict(kind="source", source_id="S1", quote="市场规模10亿元。"),
        location=dict(paragraph=3, sentence=1),
    )
    return base | change


@pytest.mark.parametrize(
    ("row", "code"),
    [
        (finding(location=dict(paragraph=99, sentence=1)), "REPORT_LOCATION_INVALID"),
        (finding(location=dict(paragraph=3, sentence=99)), "REPORT_LOCATION_INVALID"),
        (
            finding(
                evidence=dict(kind="source", source_id="S999", quote="市场规模10亿元。")
            ),
            "SOURCE_UNAVAILABLE",
        ),
        (
            finding(
                evidence=dict(kind="source", source_id="S1", quote="来源未有的原句。")
            ),
            "SOURCE_QUOTE_INVALID",
        ),
        (
            finding(evidence=dict(kind="report", quote="报告未有的原句。")),
            "REPORT_QUOTE_INVALID",
        ),
    ],
)
def test_bad_anchor_or_source_is_precise_error(row, code):
    errors = finding_errors(
        dict(findings=[row]), BODY, [source(excerpt="市场规模10亿元。")]
    )
    assert (
        errors
        and errors[0]["code"] == code
        and errors[0]["path"].startswith("/findings/0/")
    )


def test_quote_from_source_title_is_not_source_excerpt():
    row = finding(
        evidence=dict(kind="source", source_id="S1", quote="仅标题中的第二名")
    )
    assert finding_errors(
        dict(findings=[row]),
        BODY,
        [source(title="仅标题中的第二名", excerpt="市场规模10亿元。")],
    )


def test_valid_source_and_report_quotes_are_accepted():
    rows = [
        finding(),
        finding(
            finding_id="R1", evidence=dict(kind="report", quote="市场规模10亿元[S1]。")
        ),
    ]
    assert not finding_errors(
        dict(findings=rows), BODY, [source(excerpt="市场规模10亿元。")]
    )


def test_report_quote_can_span_sentences_when_it_overlaps_anchor():
    text = "市场规模10亿元。同比增长20%。"
    row = finding(
        evidence=dict(kind="report", quote=text), location=dict(paragraph=1, sentence=1)
    )
    assert not finding_errors(dict(findings=[row]), text, [])


def test_report_quote_must_overlap_selected_sentence():
    text = "市场规模10亿元。同比增长20%。"
    row = finding(
        evidence=dict(kind="report", quote="同比增长20%。"),
        location=dict(paragraph=1, sentence=1),
    )
    assert (
        finding_errors(dict(findings=[row]), text, [])[0]["code"]
        == "REPORT_QUOTE_INVALID"
    )


def execute(raws, body=BODY):
    model = AsyncMock(
        side_effect=[
            FakeResp(
                raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
            )
            for raw in raws
        ]
    )
    with patch.object(reviewer, "chat", new=model):
        out = asyncio.run(
            reviewer.reviewer_node(
                dict(
                    task_id="t",
                    topic="市场",
                    report_pipeline_version=2,
                    draft_content=body,
                    evidence=[source(excerpt="市场规模10亿元。")],
                )
            )
        )
    return out, model


def test_forged_finding_is_corrected_once_before_body_rewrite():
    bad = dict(
        passed=False,
        comments=["数字有误"],
        findings=[
            finding(evidence=dict(kind="source", source_id="S1", quote="伪造出处"))
        ],
    )
    out, model = execute([bad, dict(passed=True, comments=[])])
    assert model.await_count == 2 and out["passed"]
    attempts = out["review_history"][-1]["review_response_attempts"]
    assert len(attempts) == 2 and "伪造出处" in attempts[0]["raw_response"]
    assert not out["findings"]


def test_still_invalid_finding_is_unavailable_not_report_error_or_pass():
    bad = dict(
        passed=False,
        comments=["数字有误"],
        findings=[
            finding(evidence=dict(kind="source", source_id="S1", quote="伪造出处"))
        ],
    )
    out, model = execute([bad, bad])
    assert model.await_count == 2 and out["quality_status"] == "unavailable"
    assert not out["findings"] and not out["review_history"][-1]["review_complete"]


def test_invalid_json_shares_same_two_attempt_budget():
    out, model = execute(["not-json", "not-json"])
    assert model.await_count == 2 and out["quality_status"] == "unavailable"
    assert len(out["review_history"][-1]["review_response_attempts"]) == 2


def test_confirmed_report_error_survives_review_fault():
    out, _ = execute(["not-json", "not-json"], BODY + "\n\n无效来源[S999]。")
    assert out["quality_status"] == "rejected" and out["findings"]
    assert not out["review_history"][-1]["review_complete"]


def test_suspected_number_is_not_promoted_to_confirmed_on_review_fault():
    out, _ = execute(["not-json", "not-json"], BODY.replace("10亿元", "99亿元"))
    assert out["quality_status"] == "unavailable"


def test_provenance_and_policy_share_one_correction_budget():
    bad = dict(
        passed=False,
        comments=["需要核对"],
        findings=[
            finding(evidence=dict(kind="source", source_id="S1", quote="伪造出处"))
        ],
    )
    conflicting = dict(passed=True, comments=[], findings=[finding()])
    out, model = execute([bad, conflicting])
    assert model.await_count == 2 and out["quality_status"] == "unavailable"
    assert len(out["review_history"][-1]["review_response_attempts"]) == 2


def test_unique_report_quote_is_reanchored_to_actual_paragraph_and_sentence():
    row = finding(
        evidence=dict(kind="report", quote="市场规模10亿元[S1]。"),
        location=dict(paragraph=1, sentence=9),
    )
    corrections = repair_report_locations(dict(findings=[row]), BODY)
    assert row["location"] == dict(paragraph=3, sentence=1)
    assert corrections[0]["finding_id"] == "M1"
    assert corrections[0]["before"] == dict(paragraph=1, sentence=9)
    forged = finding(evidence=dict(kind="report", quote="报告未有的原句。"))
    assert not repair_report_locations(dict(findings=[forged]), BODY)
    assert (
        finding_errors(dict(findings=[forged]), BODY, [])[0]["code"]
        == "REPORT_QUOTE_INVALID"
    )


def test_reviewer_separates_program_metadata_and_repeats_coordinate_rules_on_correction():
    calls = []

    async def model(messages, **kwargs):
        calls.append([dict(message) for message in messages])
        if len(calls) == 1:
            return FakeResp(json.dumps(dict(
                passed=False,
                comments=["长度不足"],
                findings=[finding(evidence=dict(kind="report", quote="程序统计内容"))],
            ), ensure_ascii=False))
        return FakeResp('{"passed":true,"comments":[]}')

    with patch.object(reviewer, "chat", new=model):
        out = asyncio.run(reviewer.reviewer_node(dict(
            task_id="t", topic="市场", report_pipeline_version=2,
            draft_content=BODY, evidence=[source(excerpt="市场规模10亿元。")],
        )))
    first = calls[0][1]["content"]
    assert "【程序统计（不是报告正文，不得作为报告quote）】" in first
    assert first.index("【程序统计") < first.index("【报告正文")
    assert "段落坐标（仅用于location；报告正文从段落1开始" in first
    correction = calls[1][-1]["content"]
    assert "不是报告正文" in correction and "从1开始计数" in correction
    assert out["passed"] and len(out["review_history"][-1]["review_response_attempts"]) == 2


def test_response_audit_preserves_empty_choices_nontext_and_truncation():
    from services.review_validation import response_audit
    import hashlib

    assert response_audit(object())["raw_length"] == 0
    response = FakeResp(42)
    assert response_audit(response)["raw_response"] == "42"
    response = FakeResp("x" * 33000)
    audit = response_audit(response)
    assert audit["raw_length"] == 33000 and audit["raw_truncated"]
    assert audit["raw_sha256"] == hashlib.sha256(("x" * 33000).encode()).hexdigest()


def test_deleted_or_other_task_source_is_not_accepted():
    evidence = [source(excerpt="市场规模10亿元。", source_deleted=True)]
    assert (
        finding_errors(dict(findings=[finding()]), BODY, evidence)[0]["code"]
        == "SOURCE_UNAVAILABLE"
    )
    evidence = [source("other", excerpt="市场规模10亿元。")]
    assert (
        finding_errors(dict(findings=[finding()]), BODY, evidence, task_id="t")[0][
            "code"
        ]
        == "SOURCE_UNAVAILABLE"
    )


def test_dispute_keeps_initial_response_trace_and_independent_raw():
    from services.review_disputes import resolve_disputes

    initial = dict(
        passed=False,
        comments=["待复核"],
        findings=[finding()],
        review_response_attempts=[dict(raw_response="初始响应")],
    )
    result, audit = asyncio.run(
        resolve_disputes(
            initial,
            BODY,
            [source(excerpt="市场规模10亿元。")],
            AsyncMock(return_value=FakeResp('{"passed":true,"comments":[]}')),
            reviewer._parse_response,
        )
    )
    assert result["review_response_attempts"] == initial["review_response_attempts"]
    assert audit["response_attempt"]["raw_response"] == '{"passed":true,"comments":[]}'


def test_independent_review_cannot_use_forged_source_quote():
    from services.review_disputes import resolve_disputes

    initial = dict(passed=False, comments=["待复核"], findings=[finding()])
    bad = dict(
        passed=False,
        comments=["仍有问题"],
        findings=[
            finding(evidence=dict(kind="source", source_id="S1", quote="伪造出处"))
        ],
    )
    result, audit = asyncio.run(
        resolve_disputes(
            initial,
            BODY,
            [source(excerpt="市场规模10亿元。")],
            AsyncMock(return_value=FakeResp(json.dumps(bad))),
            reviewer._parse_response,
        )
    )
    assert audit["status"] == "unresolved" and result["review_diagnostics"]
    assert audit["response_attempt"]["decision"] == "rejected"


def test_response_trace_survives_real_sqlite_history_roundtrip():
    from db import database, task_repository as repo

    bad = dict(
        passed=False,
        comments=["需核对"],
        findings=[
            finding(evidence=dict(kind="source", source_id="S1", quote="伪造出处"))
        ],
    )
    out, _ = execute([bad, dict(passed=True, comments=[])])
    database.init_db()
    repo.create_task("t", "市场")
    repo.update_task("t", review_history=out["review_history"])
    saved = repo.get_task("t")["review_history"][-1]["review_response_attempts"]
    assert saved == out["review_history"][-1]["review_response_attempts"]
    assert (
        len(saved) == 2
        and saved[0]["raw_sha256"]
        and "伪造出处" in saved[0]["raw_response"]
    )


def test_source_quote_whitespace_and_linebreak_are_normalized():
    row = finding(evidence=dict(kind="source", source_id="S1", quote="市场规模\n\n10亿元。"))
    assert finding_errors(
        dict(findings=[row]), BODY, [source(excerpt="市场规模 10亿元。")]
    ) == []


def test_report_quote_with_wrong_citation_marker_adopts_actual_draft_sentence():
    body = ("# 报告\n\n## 执行摘要\n总量收缩与结构重塑并行[S2]。"
            "其中内销约1005万辆，同比下降约20%。[S21]\n\n## 结论与建议\n建议跟踪。")
    row = finding(
        evidence=dict(kind="report", quote="其中内销约1005万辆，同比下降约20%。[S2]"),
        location=dict(paragraph=1, sentence=2),
    )
    corrections = repair_report_locations(dict(findings=[row]), body)
    assert row["evidence"]["quote"] == "其中内销约1005万辆，同比下降约20%。[S21]"
    assert row["location"] == dict(paragraph=2, sentence=2)
    assert corrections[0]["quote_corrected"] is True
    assert not finding_errors(dict(findings=[row]), body, [])


def test_paraphrased_core_quote_is_omitted_on_second_attempt_instead_of_unavailable():
    payload = dict(
        passed=True,
        comments=[],
        findings=[],
        core_coverage=[dict(question="市场", covered=True,
                            quote="报告提到了市场规模。", reason="覆盖")],
    )
    out, model = execute([payload, payload])
    assert model.await_count == 2 and out["quality_status"] == "passed"
    checks = out["review_history"][-1]["content_checks"]
    assert checks["core_status"] == "unknown" and checks["core_coverage"] == []


def test_structure_finding_contradicting_program_check_is_invalid():
    from services.review_validation import structure_finding_errors
    body = "# 报告\n\n## 结论与建议\n\n建议跟踪。[S1]"
    row = finding(evidence=dict(kind="report", quote="## 结论与建议"),
                  problem="结论与建议章节为空，仅包含标题", location=dict(paragraph=2, sentence=1))
    errors = structure_finding_errors(dict(findings=[row]), body)
    assert errors and errors[0]["code"] == "STRUCTURE_FINDING_CONTRADICTED"
    empty = "# 报告\n\n## 结论与建议"
    assert not structure_finding_errors(dict(findings=[row]), empty)


def test_contradicted_structure_finding_goes_back_for_retry():
    bad = dict(
        passed=False,
        comments=["结构缺失"],
        findings=[finding(
            evidence=dict(kind="report", quote="## 结论与建议"),
            problem="结论与建议章节为空，仅包含标题",
            location=dict(paragraph=4, sentence=1),
        )],
    )
    out, model = execute([bad, dict(passed=True, comments=[])])
    assert model.await_count == 2 and out["quality_status"] == "passed"


def test_source_finding_is_reanchored_to_sentence_citing_that_source():
    body = ("# 报告\n\n## 正文\n出货量5061万台[S2]。渠道集中于数字化触点[S4]。\n\n"
            "## 结论与建议\n建议跟踪。")
    row = finding(
        evidence=dict(kind="source", source_id="S4", quote="渠道数据"),
        location=dict(paragraph=2, sentence=1))
    corrections = repair_source_finding_locations(dict(findings=[row]), body)
    assert row["location"] == dict(paragraph=2, sentence=2)
    assert corrections and corrections[0]["finding_id"] == "M1"


def test_source_finding_with_ambiguous_citation_stays_put():
    body = "# 报告\n\n## 正文\n甲渠道[S4]。乙渠道[S4]。"
    row = finding(evidence=dict(kind="source", source_id="S4", quote="渠道"),
                  location=dict(paragraph=2, sentence=1))
    assert not repair_source_finding_locations(dict(findings=[row]), body)
    assert row["location"] == dict(paragraph=2, sentence=1)
