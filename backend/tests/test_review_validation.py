"""R04-01 不可信审核意见不能驱动正文修复或默认放行。"""

import asyncio
import json
from unittest.mock import AsyncMock, patch
import pytest
from services.review_validation import finding_errors
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
