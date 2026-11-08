"""v3返工/导出/版本选择的真实边界。"""

import asyncio
import json
from unittest.mock import AsyncMock, patch
import pytest
from services.document_rewrite import rewrite_context, apply_actions, rewrite_document
from services.report_document import build_document, render_document
from tests.test_report_document import example
from tests.test_rewriter import FakeResp


def fixture():
    p, e, c = example()
    d = build_document(p, "t", e, c)
    return p, e, d, rewrite_context(d, e)


def replacement(text="建议持续跟踪来源。"):
    return dict(kind="recommendation", text=text)


def test_explicit_target_keeps_id_and_increments_revision():
    p, e, d, c = fixture()
    result = apply_actions(
        d,
        dict(actions=[dict(target="B3", operation="replace", value=replacement())]),
        c,
        e,
    )
    candidate = result["document"]
    assert (
        candidate["revision"] == 2
        and candidate["sections"][2]["blocks"][0]["block_id"]
        == d["sections"][2]["blocks"][0]["block_id"]
    )
    assert candidate["sections"][0]["blocks"] == d["sections"][0]["blocks"]
    assert result["accepted"] and not result["rejected"]


def test_bad_action_does_not_remove_another_good_action():
    p, e, d, c = fixture()
    payload = dict(
        actions=[
            dict(target="B3", operation="replace", value=replacement()),
            dict(
                target="B2",
                operation="replace",
                value=dict(
                    kind="fact",
                    text="市场规模999亿元。",
                    claims=[dict(quote="市场规模999亿元。", facts=["F1"])],
                ),
            ),
        ]
    )
    result = apply_actions(d, payload, c, e)
    assert len(result["accepted"]) == len(result["rejected"]) == 1
    assert "999亿元" not in render_document(result["document"])
    assert result["document"]["sections"][1]["blocks"] == d["sections"][1]["blocks"]


@pytest.mark.parametrize("fault", ["revision", "task", "target", "alias"])
def test_request_snapshot_cannot_be_reused_or_tampered(fault):
    p, e, d, c = fixture()
    if fault == "revision":
        c["base_revision"] = 9
    elif fault == "task":
        c["task_id"] = "other"
    elif fault == "target":
        c["targets"]["B3"]["block_id"] = "unknown"
    else:
        c["facts"]["aliases"]["F1"] = "unknown"
    with pytest.raises(ValueError):
        apply_actions(
            d,
            dict(actions=[dict(target="B3", operation="replace", value=replacement())]),
            c,
            e,
        )


def test_remove_required_section_block_is_rejected():
    p, e, d, c = fixture()
    result = apply_actions(
        d, dict(actions=[dict(target="B3", operation="remove", value=None)]), c, e
    )
    assert result["document"] == d and result["rejected"]


def test_unchanged_action_is_not_applied():
    p, e, d, c = fixture()
    result = apply_actions(
        d,
        dict(
            actions=[
                dict(
                    target="B3",
                    operation="replace",
                    value=p["sections"][2]["blocks"][0],
                )
            ]
        ),
        c,
        e,
    )
    assert result["document"] == d and not result["accepted"]
    assert result["rejected"][0]["code"] == "NO_CHANGE"


def test_kind_only_change_is_not_a_factual_repair():
    p, e, d, c = fixture()
    value = p["sections"][1]["blocks"][0] | {"kind": "analysis"}
    out = apply_actions(
        d, dict(actions=[dict(target="B2", operation="replace", value=value)]), c, e
    )
    assert not out["accepted"] and out["document"] == d
    assert out["rejected"][0]["code"] == "NO_CHANGE"


def test_full_kind_only_change_is_not_progress():
    p, e, d, c = fixture()
    p["sections"][1]["blocks"][0]["kind"] = "analysis"
    out = asyncio.run(
        rewrite_document(
            AsyncMock(return_value=FakeResp(json.dumps(p))),
            dict(
                task_id="t",
                report_document=d,
                evidence=e,
                draft_content=render_document(d),
                full_rewrite_required=True,
            ),
        )
    )
    assert out["report_document"] == d and out["forced_output"]


def test_full_candidate_failure_keeps_whole_original_document():
    p, e, d, c = fixture()
    p["sections"][0]["blocks"][0]["claims"][0]["facts"] = ["F9999"]
    model = AsyncMock(return_value=FakeResp(json.dumps(p)))
    out = asyncio.run(
        rewrite_document(
            model,
            dict(
                task_id="t",
                topic="市场",
                report_pipeline_version=3,
                report_document=d,
                evidence=e,
                draft_content=render_document(d),
                full_rewrite_required=True,
                review_history=[{}],
            ),
        )
    )
    assert out["report_document"] == d and out["draft_content"] == render_document(d)


def test_real_rewriter_entry_uses_short_targets_without_digest():
    from agents import rewriter

    p, e, d, c = fixture()
    payload = dict(
        actions=[dict(target="B3", operation="replace", value=replacement())]
    )
    model = AsyncMock(return_value=FakeResp(json.dumps(payload)))
    with patch.object(rewriter, "chat", new=model):
        out = asyncio.run(
            rewriter.rewriter_node(
                dict(
                    task_id="t",
                    topic="市场",
                    report_pipeline_version=3,
                    report_document=d,
                    evidence=e,
                    draft_content=render_document(d),
                    review_history=[{}],
                )
            )
        )
    assert (
        out["report_document"]["revision"] == 2 and out["quality_status"] == "rejected"
    )
    request = "\n".join(m["content"] for m in model.call_args.args[0])
    assert (
        "B3" in request and "digest" not in request and "base_revision" not in request
    )


def test_v3_export_renders_authoritative_document():
    from db import database, task_repository as repo
    from fastapi.testclient import TestClient
    from main import app
    from tests.auth_support import READ_HEADERS

    p, e, d, c = fixture()
    database.init_db()
    repo.create_task("t", "市场", metadata={"report_pipeline_version": 3})
    repo.update_task(
        "t", status="completed", report_document=d, final_report="旧缓存", evidence=e
    )
    response = TestClient(app).get("/api/v1/tasks/t/report", headers=READ_HEADERS)
    assert response.status_code == 200 and response.json()[
        "content_markdown"
    ] == render_document(d)


def test_v3_explicit_create_and_idempotency_version_identity(monkeypatch):
    import httpx
    from db import database, task_repository as repo
    from services.task_service import get_task_service
    from main import app
    from tests.auth_support import ADMIN_HEADERS

    database.init_db()

    async def run():
        service = get_task_service()
        monkeypatch.setattr(service, "schedule", lambda *args, **kwargs: None)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app),
            base_url="http://test",
            headers=ADMIN_HEADERS,
        ) as client:
            one = await client.post(
                "/api/v2/tasks",
                json={"topic": "市场", "report_pipeline_version": 3},
                headers={"Idempotency-Key": "version-choice"},
            )
            assert one.status_code == 202
            task = repo.get_task(one.json()["data"]["task_id"])
            assert task["metadata"]["report_pipeline_version"] == 3
            conflict = await client.post(
                "/api/v2/tasks",
                json={"topic": "市场", "report_pipeline_version": 2},
                headers={"Idempotency-Key": "version-choice"},
            )
            assert conflict.status_code == 409

    asyncio.run(run())


def test_full_candidate_cannot_delete_all_bound_evidence():
    p, e, d, c = fixture()
    for section in p["sections"]:
        section["blocks"] = [replacement("资料说明：仅作组织说明。")]
    out = asyncio.run(
        rewrite_document(
            AsyncMock(return_value=FakeResp(json.dumps(p))),
            dict(
                task_id="t",
                topic="市场",
                report_document=d,
                evidence=e,
                draft_content=render_document(d),
                full_rewrite_required=True,
            ),
        )
    )
    assert out["report_document"] == d


def test_v3_reviewer_does_not_treat_code_literals_as_citations():
    from agents import reviewer

    p, e, _ = example()
    p["sections"][1]["blocks"].append(
        dict(kind="analysis", text="```txt\n[S999] 99亿元\n```")
    )
    d = build_document(p, "t", e)
    with patch.object(
        reviewer,
        "chat",
        new=AsyncMock(return_value=FakeResp('{"passed":true,"comments":[]}')),
    ):
        out = asyncio.run(
            reviewer.reviewer_node(
                dict(
                    task_id="t",
                    topic="市场",
                    report_pipeline_version=3,
                    report_document=d,
                    evidence=e,
                    draft_content=render_document(d),
                )
            )
        )
    assert not any("S999" in error for error in out["citation_errors"])


def test_v3_review_rerenders_authoritative_document():
    from agents import reviewer

    p, e, d, c = fixture()
    with patch.object(
        reviewer,
        "chat",
        new=AsyncMock(return_value=FakeResp('{"passed":true,"comments":[]}')),
    ):
        out = asyncio.run(
            reviewer.reviewer_node(
                dict(
                    task_id="t",
                    topic="市场",
                    report_pipeline_version=3,
                    report_document=d,
                    evidence=e,
                    draft_content="市场999亿元[S1]。",
                )
            )
        )
    assert out["draft_content"] == render_document(d)


def test_v3_export_missing_document_cannot_use_old_cache():
    from db import database, task_repository as repo
    from fastapi.testclient import TestClient
    from main import app
    from tests.auth_support import READ_HEADERS

    database.init_db()
    repo.create_task("t", "市场", metadata={"report_pipeline_version": 3})
    repo.update_task("t", status="completed", final_report="旧缓存")
    assert (
        TestClient(app).get("/api/v1/tasks/t/report", headers=READ_HEADERS).status_code
        == 409
    )


@pytest.mark.parametrize("version", [1, True, 3.0, "3", 4, None])
def test_explicit_api_version_is_strict(version):
    from services.task_service import TaskCreateRequest

    with pytest.raises(ValueError):
        TaskCreateRequest(topic="市场", report_pipeline_version=version)


def test_resume_checkpoint_cannot_switch_pipeline(offline_services):
    from db import database, task_repository as repo
    from graph import workflow
    from memory.checkpointer import open_checkpointer

    database.init_db()
    repo.create_task("version-resume", "市场", metadata={"report_pipeline_version": 3})

    async def run():
        async with open_checkpointer() as (cp, kind):
            assert kind == "sqlite"
            graph = workflow.build_graph(checkpointer=cp, interrupt_before=["planner"])
            initial = workflow._initial_state("version-resume", "市场") | {
                "report_pipeline_version": 2
            }
            await graph.ainvoke(
                initial, {"configurable": {"thread_id": "version-resume"}}
            )
        with pytest.raises(ValueError):
            await workflow.run_task("version-resume", "市场", resume=True)

    asyncio.run(run())
    assert not offline_services["planner"].calls


def test_named_finding_restricts_actions_to_actual_block():
    p, e, d, c = fixture()
    payload = dict(
        actions=[
            dict(target="B2", operation="replace", value=replacement("不应修改正文。")),
            dict(target="B3", operation="replace", value=replacement()),
        ]
    )
    out = asyncio.run(
        rewrite_document(
            AsyncMock(return_value=FakeResp(json.dumps(payload))),
            dict(
                task_id="t",
                report_document=d,
                evidence=e,
                draft_content=render_document(d),
                findings=[
                    dict(
                        location={"paragraph": 7},
                        problem="结论需补充",
                        expected="补充建议",
                    )
                ],
                review_history=[{}],
            ),
        )
    )
    receipt = out["review_history"][-1]["rewrite"]
    assert len(receipt["accepted"]) == len(receipt["rejected"]) == 1
    assert receipt["rejected"][0]["code"] == "SCOPE_RESTRICTED"
    assert out["report_document"]["sections"][1]["blocks"] == d["sections"][1]["blocks"]


def test_deletion_and_later_action_keep_original_targets():
    p, e, _ = example()
    p["sections"][1]["blocks"].append(replacement("原补充说明。"))
    d = build_document(p, "t", e)
    c = rewrite_context(d, e)
    out = apply_actions(
        d,
        dict(
            actions=[
                dict(target="B2", operation="remove"),
                dict(
                    target="B3", operation="replace", value=replacement("新补充说明。")
                ),
            ]
        ),
        c,
        e,
    )
    assert len(out["accepted"]) == 2
    assert (
        out["document"]["sections"][1]["blocks"][0]["block_id"]
        == d["sections"][1]["blocks"][1]["block_id"]
    )
    assert out["document"]["sections"][1]["blocks"][0]["text"] == "新补充说明。"


def test_duplicate_or_unknown_target_is_refused_without_losing_first_action():
    p, e, d, c = fixture()
    good = dict(target="B3", operation="replace", value=replacement())
    out = apply_actions(
        d, dict(actions=[good, good, dict(target="B999", operation="remove")]), c, e
    )
    assert len(out["accepted"]) == 1
    assert [r["code"] for r in out["rejected"]] == [
        "DUPLICATE_TARGET",
        "SCOPE_RESTRICTED",
    ]


@pytest.mark.parametrize(
    "proposal",
    [
        dict(target="B3", operation="replace"),
        dict(target="B3", operation="remove", value=replacement()),
        None,
    ],
)
def test_invalid_action_shape_cannot_become_deletion(proposal):
    p, e, d, c = fixture()
    out = apply_actions(d, dict(actions=[proposal]), c, e)
    assert out["document"] == d and out["rejected"][0]["code"] == "CONTRACT_REJECTED"
    assert out["rejected"][0]["errors"]


@pytest.mark.parametrize("payload", [{}, dict(actions=[]), dict(actions=[None])])
def test_protocol_correction_has_two_call_limit(payload):
    p, e, d, c = fixture()
    model = AsyncMock(return_value=FakeResp(json.dumps(payload)))
    out = asyncio.run(
        rewrite_document(
            model,
            dict(
                task_id="t",
                report_document=d,
                evidence=e,
                draft_content=render_document(d),
            ),
        )
    )
    assert model.await_count == 2 and out["report_document"] == d


def test_invalid_json_cannot_change_document():
    p, e, d, c = fixture()
    model = AsyncMock(return_value=FakeResp("{invalid"))
    out = asyncio.run(
        rewrite_document(
            model,
            dict(
                task_id="t",
                report_document=d,
                evidence=e,
                draft_content=render_document(d),
            ),
        )
    )
    assert model.await_count == 2 and out["report_document"] == d


def test_truncated_response_is_terminal_and_keeps_document():
    p, e, d, c = fixture()
    response = FakeResp("{}")
    response.choices[0].finish_reason = "length"
    model = AsyncMock(return_value=response)
    out = asyncio.run(
        rewrite_document(
            model,
            dict(
                task_id="t",
                report_document=d,
                evidence=e,
                draft_content=render_document(d),
            ),
        )
    )
    assert model.await_count == 1 and out["report_document"] == d


def test_full_valid_candidate_is_atomic_and_unchanged_full_is_not_progress():
    p, e, d, c = fixture()
    state = dict(
        task_id="t",
        report_document=d,
        evidence=e,
        draft_content=render_document(d),
        full_rewrite_required=True,
    )
    unchanged = asyncio.run(
        rewrite_document(AsyncMock(return_value=FakeResp(json.dumps(p))), state)
    )
    assert unchanged["report_document"] == d
    p["title"] = "新市场报告"
    updated = asyncio.run(
        rewrite_document(AsyncMock(return_value=FakeResp(json.dumps(p))), state)
    )
    assert (
        updated["report_document"]["revision"] == 2
        and updated["report_document"]["title"] == "新市场报告"
    )


def test_v3_actual_checkpoint_resume_keeps_document_revision(
    offline_services, monkeypatch
):
    from agents import react_researcher
    from db import database, task_repository as repo
    from graph import workflow
    from memory.checkpointer import open_checkpointer

    p, _, _ = example()
    database.init_db()
    repo.create_task("doc-resume", "市场", metadata={"report_pipeline_version": 3})
    monkeypatch.setattr(
        react_researcher.mcp_manager,
        "call_tool",
        AsyncMock(
            return_value={
                "results": [
                    {
                        "title": "统计资料",
                        "url": "https://example.invalid/stat",
                        "snippet": "市场规模10亿元。",
                    }
                ]
            }
        ),
    )
    offline_services["planner"].outputs = [
        json.dumps(
            [
                dict(id=1, agent="researcher", task="研究市场", depends_on=[]),
                dict(id=2, agent="writer", task="完整报告", depends_on=[1]),
            ]
        )
    ]
    offline_services["react_researcher"].outputs = [
        '{"action":"web_search","query":"市场"}',
        '{"action":"done"}',
    ]
    offline_services["react_writer"].outputs = [json.dumps(p)]
    offline_services["reviewer"].outputs = ['{"passed":true,"comments":[]}']

    async def run():
        async with open_checkpointer() as (cp, kind):
            assert kind == "sqlite"
            graph = workflow.build_graph(checkpointer=cp, interrupt_before=["reviewer"])
            initial = workflow._initial_state("doc-resume", "市场") | {
                "report_pipeline_version": 3
            }
            await graph.ainvoke(initial, {"configurable": {"thread_id": "doc-resume"}})
            snap = await graph.aget_state({"configurable": {"thread_id": "doc-resume"}})
            before = snap.values["report_document"]
        final = await workflow.run_task("doc-resume", "市场", resume=True)
        assert (
            final["report_document"] == before
            and final["report_document"]["revision"] == 1
        )

    asyncio.run(run())
    assert len(offline_services["react_writer"].calls) == 1
    assert repo.get_task("doc-resume")["report_document"]["revision"] == 1


def test_actual_v3_graph_rewrites_then_persists_revised_document(
    offline_services, monkeypatch
):
    from agents import react_researcher
    from db import database, task_repository as repo
    from graph.workflow import run_task

    p, _, _ = example()
    text = "市场规模10亿元，因此需求推动增长。"
    p["sections"][1]["blocks"][0] = dict(
        kind="analysis", text=text, claims=[dict(quote=text, facts=["F1"])]
    )
    database.init_db()
    repo.create_task(
        "doc-rewrite-graph", "市场", metadata={"report_pipeline_version": 3}
    )
    monkeypatch.setattr(
        react_researcher.mcp_manager,
        "call_tool",
        AsyncMock(
            return_value={
                "results": [
                    {
                        "title": "统计资料",
                        "url": "https://example.invalid/stat",
                        "snippet": "市场规模10亿元。",
                    }
                ]
            }
        ),
    )
    offline_services["planner"].outputs = [
        json.dumps(
            [
                dict(id=1, agent="researcher", task="研究市场", depends_on=[]),
                dict(id=2, agent="writer", task="完整报告", depends_on=[1]),
            ]
        )
    ]
    offline_services["react_researcher"].outputs = [
        '{"action":"web_search","query":"市场"}',
        '{"action":"done"}',
    ]
    offline_services["react_writer"].outputs = [json.dumps(p)]
    finding = dict(
        finding_id="CAUSE",
        problem="规模不证明需求推动增长的因果关系",
        expected="删除未经支持的因果断言，保留规模",
        severity="hard",
        evidence=dict(kind="source", source_id="S1", quote="市场规模10亿元。"),
        location=dict(paragraph=5, sentence=1),
    )
    review = json.dumps(
        dict(
            passed=False,
            comments=["因果关系不成立，应保留规模描述。"],
            findings=[finding],
        )
    )
    offline_services["reviewer"].outputs = [
        review,
        review,
        '{"passed":true,"comments":[]}',
    ]
    offline_services["rewriter"].outputs = [
        json.dumps(
            dict(
                actions=[
                    dict(
                        target="B2",
                        operation="replace",
                        value=dict(
                            kind="fact",
                            text="市场规模10亿元。",
                            claims=[dict(quote="市场规模10亿元。", facts=["F1"])],
                        ),
                    )
                ]
            )
        )
    ]
    asyncio.run(run_task("doc-rewrite-graph", "市场"))
    saved = repo.get_task("doc-rewrite-graph")
    assert saved["report_document"]["revision"] == 2
    assert saved["final_report"] == render_document(saved["report_document"])
    assert "因此需求推动增长" not in saved["final_report"]
    assert len(offline_services["rewriter"].calls) == 1
