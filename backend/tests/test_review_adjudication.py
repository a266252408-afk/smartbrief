"""R04-02 证据裁决/soft入口/缓存与不可豁免事实。"""

import asyncio
import copy
import json
from unittest.mock import AsyncMock, patch
import pytest
from services.review_adjudication import adjudicate
from services.review_policy import freeze_policy
from tests.test_evidence import source
from tests.test_rewriter import FakeResp


BODY = "苹果排名第二[S1]。"


def issue(**changes):
    return (
        dict(
            finding_id="CMP1",
            rule="unverified_comparison",
            severity="hard",
            origin="program",
            decision="suspected",
            material=True,
            problem="需核验排序",
            expected="核对来源排名",
            location=dict(paragraph=1, sentence=1),
            source_ids=["S1"],
        )
        | changes
    )


def judge(
    verdict="report_supported", quote="Apple followed in second place.", report=BODY, **changes
):
    value = (
        dict(
            target="I1",
            verdict=verdict,
            report_quote=report,
            sources=[dict(source_id="S1", quote=quote)],
            reason="核对完整来源原文",
        )
        | changes
    )
    return FakeResp(json.dumps(dict(verdicts=[value]), ensure_ascii=False))


def run(findings=None, model=None, evidence=None, history=(), body=BODY):
    return asyncio.run(
        adjudicate(
            [issue()] if findings is None else findings,
            body,
            [source(excerpt="Apple followed in second place.")]
            if evidence is None
            else evidence,
            freeze_policy(dict(task_id="t", topic="市场")),
            model or AsyncMock(return_value=judge()),
            history=history,
            task_id="t",
        )
    )


def test_supported_ranking_is_dismissed_with_program_source_spans():
    result, audit = run()
    assert result[0]["decision"] == "dismissed" and not result[0]["material"]
    record = audit["records"][0]
    assert record["verdict"] == "report_supported" and record["source_spans"][0]["start"] == 0
    assert record["source_spans"][0]["quote"] == "Apple followed in second place."


def test_unsupported_ranking_is_confirmed_only_with_valid_quote():
    result, audit = run(
        model=AsyncMock(return_value=judge("report_contradicted", "No ranking is provided.")),
        evidence=[source(excerpt="No ranking is provided.")],
    )
    assert result[0]["decision"] == "confirmed" and result[0]["material"]


@pytest.mark.parametrize(
    "bad",
    [
        judge(quote="伪造原文"),
        judge(report="别的报告原句"),
        judge(sources=[]),
        judge(
            sources=[dict(source_id="S999", quote="Apple followed in second place.")]
        ),
    ],
)
def test_bad_verdict_evidence_cannot_produce_pass(bad):
    result, audit = run(model=AsyncMock(return_value=bad))
    assert result[0]["decision"] == "unresolved" and audit["status"] == "unresolved"
    assert audit["records"][0]["verdict"] == "insufficient"


@pytest.mark.parametrize(
    "rule", ["invalid_reference", "missing_citation", "period_mismatch"]
)
def test_confirmed_program_fact_cannot_be_overruled(rule):
    model = AsyncMock(return_value=judge())
    result, audit = run(findings=[issue(rule=rule, decision="confirmed")], model=model)
    assert result[0]["decision"] == "confirmed"
    model.assert_not_awaited()


def test_source_soft_issue_is_adjudicated_but_style_soft_is_not():
    soft = issue(
        origin="model",
        rule="reviewer",
        severity="soft",
        decision="unresolved",
        evidence=dict(
            kind="source", source_id="S1", quote="Apple followed in second place."
        ),
    )
    result, audit = run(findings=[soft])
    assert result[0]["decision"] == "dismissed"
    style = issue(
        origin="model",
        rule="reviewer",
        severity="soft",
        decision="confirmed",
        material=False,
        evidence=dict(kind="report", quote=BODY),
    )
    model = AsyncMock()
    result, audit = run(findings=[style], model=model)
    model.assert_not_awaited()


def test_same_context_reuses_verified_ruling_without_model_call():
    first, audit = run()
    model = AsyncMock(side_effect=RuntimeError("不应调用"))
    second, cached = run(model=model, history=[dict(dispute_review=audit)])
    assert second[0]["decision"] == "dismissed" and cached["records"][0]["cached"]
    model.assert_not_awaited()


def test_changed_source_invalidates_cached_ruling():
    first, audit = run()
    model = AsyncMock(return_value=judge("report_contradicted", "No ranking is provided."))
    second, changed = run(
        model=model,
        evidence=[source(excerpt="No ranking is provided.")],
        history=[dict(dispute_review=audit)],
    )
    assert second[0]["decision"] == "confirmed" and model.await_count == 1


def test_same_version_cannot_add_uncached_second_judge_request():
    first, audit = run()
    model = AsyncMock()
    result, later = run(
        findings=[issue(rule="unsupported_wording", finding_id="OTHER")],
        model=model,
        history=[dict(dispute_review=audit)],
    )
    assert result[0]["decision"] == "unresolved"
    model.assert_not_awaited()


def test_missing_provider_result_is_unresolved_and_budget_failure_propagates():
    result, audit = run(model=AsyncMock(side_effect=RuntimeError("失败")))
    assert result[0]["decision"] == "unresolved"
    from services.usage_service import UsagePersistenceError

    with pytest.raises(UsagePersistenceError):
        run(model=AsyncMock(side_effect=UsagePersistenceError("账本失败")))


def test_malformed_verdicts_are_not_empty_success():
    result, audit = run(model=AsyncMock(return_value=FakeResp("{}")))
    assert result[0]["decision"] == "unresolved" and audit["status"] == "unresolved"


def integrated(
    *,
    verdict="report_supported",
    history=(),
    source_text="Apple followed in second place.",
    suffix="",
):
    from agents import reviewer
    from tests.test_review_validation import BODY as template

    body = template.replace("市场规模10亿元[S1]。", BODY) + suffix
    initial = dict(
        passed=True,
        comments=["来源排序需核验"],
        findings=[
            dict(
                finding_id="M1",
                severity="soft",
                problem="来源未提排序",
                expected="核验来源排序",
                evidence=dict(kind="source", source_id="S1", quote=source_text),
                location=dict(paragraph=3, sentence=1),
            )
        ],
        possible_rule_issue="排序规则可能误判",
    )

    async def answer(messages, **kwargs):
        if kwargs.get("output_contract").__name__ == "AdjudicationResult":
            request = json.loads(messages[-1]["content"])
            return FakeResp(
                json.dumps(
                    dict(
                        verdicts=[
                            dict(
                                target=t["target"],
                                verdict=verdict,
                                report_quote=t["report_quote"],
                                sources=[dict(source_id="S1", quote=source_text)],
                                reason="逐项核验来源",
                            )
                            for t in request["targets"]
                        ]
                    ),
                    ensure_ascii=False,
                )
            )
        return FakeResp(json.dumps(initial, ensure_ascii=False))

    model = AsyncMock(side_effect=answer)
    with patch.object(reviewer, "chat", new=model):
        out = asyncio.run(
            reviewer.reviewer_node(
                dict(
                    task_id="t",
                    topic="市场",
                    report_pipeline_version=2,
                    draft_content=body,
                    evidence=[source(excerpt=source_text)],
                    review_history=list(history),
                )
            )
        )
    return out, model


def test_real_reviewer_applies_soft_and_program_rulings_before_quality_merge():
    out, model = integrated()
    assert out["passed"] and out["quality_status"] == "passed"
    audit = out["review_history"][-1]["dispute_review"]
    assert audit["status"] == "reviewed" and model.await_count == 2
    assert all(f["decision"] == "dismissed" for f in out["findings"])
    assert audit["initial_findings"] and audit["response_attempt"]["raw_sha256"]


def test_real_reviewer_insufficient_and_contradicted_do_not_pass():
    for verdict, quality in [
        ("insufficient", "unavailable"),
        ("report_contradicted", "rejected"),
    ]:
        out, model = integrated(verdict=verdict, source_text="No ranking is provided.")
        assert not out["passed"] and out["quality_status"] == quality
        assert model.await_count == 2


def test_sqlite_history_cache_survives_reopen_and_integrity_still_rejects():
    from db import database, task_repository as repo

    first, _ = integrated()
    database.init_db()
    repo.create_task("t", "市场")
    repo.update_task("t", review_history=first["review_history"])
    history = repo.get_task("t")["review_history"]
    second, model = integrated(history=history)
    assert second["passed"] and model.await_count == 1, json.dumps(
        second["review_history"][-1].get("dispute_review"), ensure_ascii=False
    )
    assert all(
        r["cached"] for r in second["review_history"][-1]["dispute_review"]["records"]
    )
    bad, _ = integrated(suffix="\n\n不存在[S999]。")
    assert bad["quality_status"] == "rejected"


@pytest.mark.parametrize(
    "location", [dict(paragraph=99, sentence=1), dict(paragraph=1, sentence=99)]
)
def test_invalid_anchor_never_drives_body_rewrite(location):
    model = AsyncMock()
    out, audit = run(findings=[issue(location=location)], model=model)
    assert out[0]["decision"] == "unresolved" and audit is None
    model.assert_not_awaited()


def test_empty_heading_sentence_has_reliable_span():
    body = "# 标题"
    out, audit = run(body=body, model=AsyncMock(return_value=judge(report=body)))
    assert audit["records"][0]["report_span"] == dict(start=0, end=4, quote=body)


def test_source_fallback_requires_actual_evidence_even_without_inline_citation():
    body = "苹果排名第二。"
    finding = issue(evidence=dict(kind="source", source_id="S1"), source_ids=[])
    out, audit = run(
        body=body, findings=[finding], model=AsyncMock(return_value=judge(report=body))
    )
    assert out[0]["decision"] == "dismissed"
    out, _ = run(
        body=body,
        findings=[finding],
        model=AsyncMock(return_value=judge(report=body, sources=[])),
    )
    assert out[0]["decision"] == "unresolved"


@pytest.mark.parametrize(
    "changes",
    [
        dict(source_deleted=True),
        dict(task_id="other"),
        dict(excerpt="Apple followed in second place. Apple followed in second place."),
    ],
)
def test_unavailable_or_ambiguous_source_does_not_exempt(changes):
    out, _ = run(evidence=[source(excerpt="Apple followed in second place.") | changes])
    assert out[0]["decision"] == "unresolved"


def test_cache_context_and_span_tampering_are_rejected():
    _, audit = run()
    for change in [
        dict(context_key="wrong"),
        dict(report_span=dict(start=99, end=100, quote=BODY)),
        dict(
            source_spans=[
                dict(
                    source_id="S1",
                    start=2,
                    end=99,
                    quote="Apple followed in second place.",
                )
            ]
        ),
    ]:
        bad = copy.deepcopy(audit)
        bad["records"][0].update(change)
        model = AsyncMock()
        out, _ = run(history=[dict(dispute_review=bad)], model=model)
        assert out[0]["decision"] == "unresolved"
        model.assert_not_awaited()


@pytest.mark.parametrize("change", ["revision", "policy", "body", "task"])
def test_changed_context_does_not_reuse_supported_cache(change):
    evidence = [source(excerpt="Apple followed in second place.")]
    policy = freeze_policy(dict(task_id="t", topic="市场"))
    first = asyncio.run(
        adjudicate(
            [issue()],
            BODY,
            evidence,
            policy,
            AsyncMock(return_value=judge()),
            task_id="t",
            revision=1,
        )
    )[1]
    body = BODY
    task = "t"
    revision = 1
    if change == "revision":
        revision = 2
    if change == "policy":
        policy = policy | dict(required_questions=["新问题"])
    if change == "body":
        body = BODY + "新增说明。"
    if change == "task":
        task = "other"
    model = AsyncMock(return_value=judge("insufficient", sources=[]))
    out, _ = asyncio.run(
        adjudicate(
            [issue()],
            body,
            evidence,
            policy,
            model,
            task_id=task,
            revision=revision,
            history=[dict(dispute_review=first)],
        )
    )
    assert out[0]["decision"] == "unresolved" and model.await_count == 1


def test_duplicate_missing_and_truncated_verdict_targets_fail_closed():
    response = judge()
    for raw in [
        dict(verdicts=[]),
        dict(
            verdicts=[
                dict(
                    target="I999",
                    verdict="report_supported",
                    report_quote=BODY,
                    sources=[],
                    reason="越界",
                )
            ]
        ),
    ]:
        out, audit = run(model=AsyncMock(return_value=FakeResp(json.dumps(raw))))
        assert out[0]["decision"] == "unresolved"
    response.choices[0].finish_reason = "length"
    out, audit = run(model=AsyncMock(return_value=response))
    assert out[0]["decision"] == "unresolved"
    model = AsyncMock()
    out, audit = run(
        evidence=[
            source(title="长" * 64001, excerpt="Apple followed in second place.")
        ],
        model=model,
    )
    assert out[0]["decision"] == "unresolved"
    model.assert_not_awaited()


def test_independent_items_keep_valid_ruling_when_other_proof_is_forged():
    items = [issue(), issue(finding_id="OTHER", rule="unsupported_wording")]
    first = json.loads(judge().choices[0].message.content)["verdicts"][0]
    second = first | dict(target="I2", sources=[dict(source_id="S1", quote="伪造")])
    out, audit = run(
        findings=items,
        model=AsyncMock(
            return_value=FakeResp(json.dumps(dict(verdicts=[first, second])))
        ),
    )
    assert [f["decision"] for f in out] == ["dismissed", "unresolved"]
    assert audit["status"] == "unresolved"


@pytest.mark.parametrize("version", [2, 3])
def test_dismissed_ruling_is_not_sent_as_rewrite_target(version):
    from agents import rewriter
    from tests.test_review_validation import BODY as template

    task = dict(
        task_id="t",
        report_pipeline_version=version,
        draft_content=template,
        evidence=[source(excerpt="市场规模10亿元。")],
        findings=[
            issue(
                finding_id="DISMISSED",
                decision="dismissed",
                material=False,
                problem="不再执行的旧目标",
            )
        ],
    )
    if version == 3:
        from tests.test_document_rewrite import fixture

        _, evidence, document, _ = fixture()
        task.update(report_document=document, evidence=evidence)
    model = AsyncMock(return_value=FakeResp("{}"))
    with patch.object(rewriter, "chat", new=model):
        out = asyncio.run(rewriter.rewriter_node(task))
    model.assert_not_awaited()
    assert not out["review_history"][-1]["rewrite"]["changed"]
    for call in model.call_args_list:
        assert "不再执行的旧目标" not in json.dumps(call.args[0], ensure_ascii=False)


def test_public_contracts_match_and_reject_model_owned_offsets():
    from services.review_adjudication import AdjudicationResult, AdjudicationRecord
    from pydantic import ValidationError

    path = (
        __import__("pathlib").Path(__file__).resolve().parents[2]
        / "docs/schemas/contracts.json"
    )
    definitions = json.loads(path.read_text(encoding="utf-8"))["$defs"]
    for model in (AdjudicationResult, AdjudicationRecord):
        schema = model.model_json_schema(ref_template="#/$defs/{model}")
        nested = schema.pop("$defs", {})
        assert definitions[model.__name__] == schema
        assert all(definitions[key] == value for key, value in nested.items())
    wire = json.loads(judge().choices[0].message.content)
    wire["verdicts"][0]["confidence"] = 1.0
    with pytest.raises(ValidationError):
        AdjudicationResult.model_validate(wire)


def test_duplicate_target_is_not_accepted_as_complete_adjudication():
    wire = json.loads(judge().choices[0].message.content)
    wire["verdicts"] *= 2
    out, audit = run(
        findings=[issue(), issue(rule="unsupported_wording")],
        model=AsyncMock(return_value=FakeResp(json.dumps(wire))),
    )
    assert all(f["decision"] == "unresolved" for f in out)
    assert audit["status"] == "unresolved"


def test_repeated_sentence_locations_have_independent_cache_entries():
    body = BODY + "\n\n" + BODY
    items = [issue(), issue(location=dict(paragraph=2, sentence=1))]
    verdict = json.loads(judge().choices[0].message.content)["verdicts"][0]
    model = AsyncMock(
        return_value=FakeResp(
            json.dumps(dict(verdicts=[verdict, verdict | dict(target="I2")]))
        )
    )
    out, audit = run(findings=items, body=body, model=model)
    assert all(f["decision"] == "dismissed" for f in out)
    assert len({r["issue_key"] for r in audit["records"]}) == 2
    model = AsyncMock()
    out, cached = run(
        findings=items, body=body, history=[dict(dispute_review=audit)], model=model
    )
    assert all(f["decision"] == "dismissed" for f in out)
    assert all(r["cached"] for r in cached["records"])
    model.assert_not_awaited()


def test_candidate_sources_are_visible_to_adjudicator():
    model = AsyncMock(return_value=judge())
    result, audit = run(
        findings=[issue(candidate_sources=["S2", "S3"])],
        model=model,
        evidence=[
            source(excerpt="Apple followed in second place."),
            source(source_id="S2", excerpt="苹果位列第一。"),
            source(source_id="S3", excerpt="苹果位列第二。"),
        ],
    )
    request = json.loads(model.call_args.args[0][-1]["content"])
    assert {s["source_id"] for s in request["sources"]} == {"S1", "S2", "S3"}
    assert result[0]["decision"] == "dismissed"


def report_issue(**changes):
    return dict(
        finding_id="STRUCT-REQUIRED-missing_conclusion", rule="required_structure",
        severity="hard", origin="program", decision="suspected", material=True,
        problem="缺少结论与建议", expected="在报告末尾增加独立章节",
        location=dict(paragraph=1, sentence=1),
        evidence={"kind": "report", "quote": "# 2026年市场调研报告"},
        structure_kind="missing_conclusion", has_section_headings=True,
    ) | changes


def report_judge(verdict, sources=()):
    return FakeResp(json.dumps(dict(verdicts=[dict(
        target="I1", verdict=verdict, report_quote="# 2026年市场调研报告",
        sources=list(sources), reason="对照报告结构核验")]), ensure_ascii=False))


REPORT_BODY = "# 2026年市场调研报告\n\n## 执行摘要\n摘要内容。"


@pytest.mark.parametrize(("verdict", "decision"), [("report_contradicted", "confirmed"), ("report_supported", "dismissed")])
def test_report_kind_finding_uses_full_report_and_needs_no_source_quote(verdict, decision):
    model = AsyncMock(return_value=report_judge(verdict))
    result, audit = run(findings=[report_issue()], model=model, evidence=[], body=REPORT_BODY)
    request = json.loads(model.call_args.args[0][-1]["content"])
    assert request["report"] == REPORT_BODY
    assert result[0]["decision"] == decision
    assert audit["records"][0]["source_spans"] == []


def test_ellipsis_spliced_adjudication_quote_keeps_longest_verbatim_fragment():
    evidence = [source(excerpt="Apple followed in second place. It is far behind.")]
    model = AsyncMock(return_value=judge(
        "report_supported", "Apple followed in second place...It is far behind."))
    result, audit = run(model=model, evidence=evidence)
    assert result[0]["decision"] == "dismissed"
    record = audit["records"][0]
    assert record["verdict"] == "report_supported"
    assert record["source_spans"][0]["quote"] == "Apple followed in second place"


def test_fabricated_adjudication_quote_stays_insufficient():
    evidence = [source(excerpt="Apple followed in second place.")]
    model = AsyncMock(return_value=judge(
        "report_supported", "Nothing here...really nothing."))
    result, audit = run(model=model, evidence=evidence)
    assert result[0]["decision"] == "unresolved"
    assert audit["records"][0]["verdict"] == "insufficient"
