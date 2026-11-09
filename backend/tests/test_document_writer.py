"""真实文档构建/Scheduler；仅外部模型用替身。"""

import asyncio
import copy
import json
from unittest.mock import AsyncMock, patch
import pytest
from services.document_writer import generate_document
from services.report_document import build_document, render_document
from tests.test_report_document import example
from tests.test_rewriter import FakeResp


def call(payloads, previous=None):
    p, e, c = example()
    model = AsyncMock(
        side_effect=[FakeResp(json.dumps(p, ensure_ascii=False)) for p in payloads]
    )
    result = asyncio.run(generate_document(model, "t", "市场", e, previous=previous))
    return result, model


def test_writer_minimal_contract_keeps_internal_fields_out_of_model_prompt():
    p, e, c = example()
    result, model = call([p])
    assert "10亿元。[S1]" in render_document(result["document"])
    messages = "\n".join(m["content"] for m in model.call_args.args[0])
    assert "F1" in messages
    assert (
        "claim_id" not in messages
        and "sha256" not in messages
        and "start" not in messages
    )
    assert result["attempts"][0]["context"]["aliases"] == c["aliases"]


def test_local_binding_repair_preserves_other_blocks():
    p, e, c = example()
    bad = copy.deepcopy(p)
    bad["sections"][1]["blocks"][0]["segments"][0]["text"] = "市场规模999亿元。"
    correction = dict(blocks=[dict(target="B2", value=p["sections"][1]["blocks"][0])])
    result, model = call([bad, correction])
    assert model.await_count == 2 and result["payload"] == p
    request = json.loads(model.call_args.args[0][-1]["content"])
    assert request["targets"][0]["target"] == "B2"
    assert len(request["targets"]) == 1


def test_local_repair_keeps_other_block_whitespace():
    p, _, _ = example()
    p["sections"][0]["blocks"][0]["segments"].append(
        dict(text="  补充说明。  \n")
    )
    expected = "".join(
        segment["text"] for segment in p["sections"][0]["blocks"][0]["segments"]
    )
    bad = copy.deepcopy(p)
    bad["sections"][1]["blocks"][0]["segments"][0]["facts"] = ["F9999"]
    result, _ = call(
        [bad, dict(blocks=[dict(target="B2", value=p["sections"][1]["blocks"][0])])]
    )
    assert (
        result["document"]["sections"][0]["blocks"][0]["text"] == expected
    )


def test_out_of_scope_correction_cannot_overwrite_accepted_block():
    p, e, c = example()
    previous = build_document(p, "t", e, c)
    bad = copy.deepcopy(p)
    bad["sections"][1]["blocks"][0]["segments"][0]["facts"] = ["F9999"]
    result, model = call(
        [bad, dict(blocks=[dict(target="B1", value=p["sections"][0]["blocks"][0])])],
        previous,
    )
    assert (
        result["document"] == previous
        and not result["applied"]
        and model.await_count == 2
    )
    assert result["attempts"][-1]["decision"] == "rejected"


def test_invalid_first_document_does_not_fall_back_to_markdown_or_drop_content():
    with pytest.raises(ValueError):
        call([{}, {}])


def test_blank_block_cannot_form_a_valid_document():
    p, _, _ = example()
    p["sections"][1]["blocks"] = [dict(kind="transition", segments=[dict(text="   ")])]
    with pytest.raises(ValueError):
        call(
            [
                p,
                dict(
                    blocks=[dict(target="B2", value=dict(kind="transition", segments=[dict(text="  ")]))]
                ),
            ]
        )


def test_valid_rewrite_increments_revision():
    p, e, c = example()
    previous = build_document(p, "t", e, c)
    result, _ = call([p], previous)
    assert result["document"]["revision"] == 2


def test_v3_real_writer_entry_sets_document_and_claims():
    from agents import react_writer

    p, e, _ = example()
    context = dict(task_id="t", claims=[], pipeline_version=3)
    with patch.object(
        react_writer, "chat", new=AsyncMock(return_value=FakeResp(json.dumps(p)))
    ):
        rendered = asyncio.run(
            react_writer.react_writer_run("市场", 1, [], {}, e, binding_context=context)
        )
    assert context["document"]["version"] == 3 and context["claims"]
    assert rendered == render_document(context["document"])


def test_scheduler_preserves_document_in_state_and_diagnostics():
    from agents import react_writer, scheduler

    p, e, _ = example()
    state = dict(
        task_id="t",
        topic="市场",
        report_pipeline_version=3,
        plan=[dict(id=1, agent="writer", task="完整报告", depends_on=[])],
        evidence=e,
    )
    with patch.object(
        react_writer, "chat", new=AsyncMock(return_value=FakeResp(json.dumps(p)))
    ):
        result = asyncio.run(scheduler.scheduler_step_node(state))
    assert result["report_document"]["version"] == 3
    assert result["draft_content"] == render_document(result["report_document"])
    assert result["writer_attempts"]


@pytest.mark.parametrize(
    "invalid",
    [
        None,
        {"sections": [None]},
        {"sections": [{"blocks": None}]},
        {"title": "空壳", "sections": []},
    ],
)
def test_invalid_root_corrected_once_without_legacy_fallback(invalid):
    p, _, _ = example()
    result, model = call([invalid, p])
    assert model.await_count == 2 and result["document"]["version"] == 3
    assert [a["mode"] for a in result["attempts"]] == ["document", "document"]


def test_invalid_local_correction_cannot_trigger_third_attempt():
    p, _, _ = example()
    p["sections"][1]["blocks"][0]["segments"][0]["facts"] = ["F9999"]
    with pytest.raises(ValueError):
        call([p, dict(blocks=[])])


def test_truncated_response_cannot_be_accepted():
    p, e, _ = example()
    response = FakeResp(json.dumps(p))
    response.choices[0].finish_reason = "length"
    model = AsyncMock(return_value=response)
    with pytest.raises(ValueError):
        asyncio.run(generate_document(model, "t", "市场", e))
    assert model.await_count == 2


def test_provider_failure_propagates_without_adding_calls():
    _, e, _ = example()
    model = AsyncMock(side_effect=RuntimeError("模型不可用"))
    with pytest.raises(RuntimeError):
        asyncio.run(generate_document(model, "t", "市场", e))
    assert model.await_count == 1


def test_shared_policy_and_minimal_contract_reach_model():
    from services.review_policy import freeze_policy, policy_text

    p, e, _ = example()
    policy = freeze_policy(dict(task_id="t", topic="市场"))
    model = AsyncMock(return_value=FakeResp(json.dumps(p)))
    asyncio.run(generate_document(model, "t", "市场", e, policy=policy))
    assert policy_text(policy) in "\n".join(
        m["content"] for m in model.call_args.args[0]
    )
    assert model.call_args.kwargs["output_contract"].__name__ == "ModelDocument"


def test_writer_diagnostics_and_document_persist_through_actual_graph(
    offline_services, monkeypatch
):
    from agents import react_researcher
    from db import database, task_repository as repo
    from graph.workflow import run_task

    p, _, _ = example()
    database.init_db()
    repo.create_task("doc-graph", "市场", metadata={"report_pipeline_version": 3})
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
    asyncio.run(run_task("doc-graph", "市场"))
    saved = repo.get_task("doc-graph")
    assert saved["report_document"]["version"] == 3
    assert saved["final_report"] == render_document(saved["report_document"])
    history = saved["review_history"][-1]
    assert history["report_pipeline_version"] == 3 and history["writer_attempts"]
    assert (
        history["report_document_revision"] == 1 and history["report_document_sha256"]
    )


def test_failed_v3_writer_keeps_diagnostics_without_legacy_success(
    offline_services, monkeypatch
):
    from agents import react_researcher
    from db import database, task_repository as repo
    from graph.workflow import run_task

    database.init_db()
    repo.create_task("doc-failed", "市场", metadata={"report_pipeline_version": 3})
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
    offline_services["react_writer"].outputs = ["{}", "{}"]
    with pytest.raises(RuntimeError):
        asyncio.run(run_task("doc-failed", "市场"))
    saved = repo.get_task("doc-failed")
    assert saved["report_document"] is None and saved["quality_status"] == "not_run"
    assert len(saved["review_history"][-1]["writer_attempts"]) == 2
    assert not offline_services["reviewer"].calls
