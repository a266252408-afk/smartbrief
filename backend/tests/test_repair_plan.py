"""R05-01分流、完整修改包、重复拦截和共享预算真实边界。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import pytest
from services.repair_plan import plan_repair
from tests.test_evidence import source
from tests.test_review_adjudication import issue
from tests.test_review_validation import BODY


def task(**changes):
    return (
        dict(
            task_id="t",
            topic="市场",
            report_pipeline_version=2,
            draft_content=BODY,
            evidence=[source(excerpt="市场规模10亿元。")],
            quality_status="rejected",
            findings=[
                issue(
                    rule="unbound_number",
                    decision="confirmed",
                    location=dict(paragraph=3, sentence=1),
                )
            ],
        )
        | changes
    )


def test_plan_contains_all_targets_full_sources_and_preserved_paragraphs():
    state = task(
        findings=[
            issue(
                finding_id=f"I{i}",
                decision="confirmed",
                location=dict(paragraph=3, sentence=1),
            )
            for i in range(12)
        ]
    )
    plan = plan_repair(state)
    assert plan["action"] == "rewrite" and plan["scope"] == "paragraph"
    assert (
        len(plan["targets"]) == 12
        and plan["targets"][0]["sources"][0]["excerpt"] == "市场规模10亿元。"
    )
    assert "10亿元" in plan["targets"][0]["report_quote"]
    assert plan["preserve"] and all(p["paragraph"] != 3 for p in plan["preserve"])


def test_fault_dispute_gap_and_full_scope_are_distinct_routes():
    assert plan_repair(task(quality_status="unavailable"))["action"] == "stop"
    assert (
        plan_repair(
            task(findings=[issue(decision="unresolved")], quality_status="unavailable")
        )["targets"][0]["kind"]
        == "dispute"
    )
    gap = task(evidence=[], review_comments=["缺少资料"])
    assert plan_repair(gap)["action"] == "research"
    assert plan_repair(gap | dict(research_revision_count=1))["action"] == "stop"
    assert plan_repair(task(full_rewrite_required=True))["scope"] == "paragraph"
    full = issue(rule="report_assembly", decision="confirmed", scope="full")
    assert plan_repair(task(findings=[full]))["scope"] == "full"


def test_same_body_evidence_targets_stop_but_changed_context_can_continue():
    state = task()
    plan = plan_repair(state)
    state["review_history"] = [
        dict(repair_plan=plan, rewrite=dict(changed=False, attempts=[{}]))
    ]
    assert plan_repair(state)["reason"] == "REPAIR_NO_PROGRESS"
    changed = state | dict(evidence=[source(excerpt="新增证据原文")])
    assert plan_repair(changed)["action"] == "rewrite"
    state["review_history"] *= 2
    assert plan_repair(state)["action"] == "stop"


def test_new_routes_are_used_by_real_graph_router():
    from graph.workflow import route_after_review

    state = task()
    plan = plan_repair(state)
    state["review_history"] = [
        dict(repair_plan=plan, rewrite=dict(changed=False, attempts=[{}]))
    ]
    assert route_after_review(state) == "passed"
    assert (
        route_after_review(task(evidence=[], review_comments=["缺少资料"]))
        == "research"
    )


def test_v2_failed_binding_does_not_expand_patch_into_full_rewrite():
    from agents import rewriter
    from tests.test_rewriter_patch_flow import state, response

    s = state() | dict(report_pipeline_version=2)
    model = AsyncMock(return_value=response(s, "市场规模999亿元[S1]。"))
    with patch.object(rewriter, "chat", new=model):
        out = asyncio.run(rewriter.rewriter_node(s))
    assert out["draft_content"] == s["draft_content"]
    assert model.await_count <= 2
    assert all(
        a["mode"] == "patch" for a in out["review_history"][-1]["rewrite"]["attempts"]
    )


@pytest.mark.parametrize("version", [2, 3])
def test_rewriter_budget_exhaustion_preserves_valid_draft(version):
    from agents import rewriter
    from services.budget import BudgetExceeded
    from tests.test_rewriter_patch_flow import state

    s = state() | dict(report_pipeline_version=version, quality_status="rejected")
    if version == 3:
        from tests.test_document_rewrite import fixture

        _, e, d, _ = fixture()
        from services.report_document import render_document

        s.update(report_document=d, evidence=e, draft_content=render_document(d))
    with patch.object(
        rewriter, "chat", new=AsyncMock(side_effect=BudgetExceeded("额度耗尽"))
    ):
        out = asyncio.run(rewriter.rewriter_node(s))
    assert out["draft_content"] == s["draft_content"]
    assert out["forced_output"] and out["quality_status"] == "rejected"
    assert out["error_code"] == "BUDGET_EXCEEDED"


def test_reviewer_budget_exhaustion_keeps_draft_and_quality_unknown():
    from agents import reviewer
    from services.budget import BudgetExceeded

    with patch.object(
        reviewer, "chat", new=AsyncMock(side_effect=BudgetExceeded("额度耗尽"))
    ):
        out = asyncio.run(
            reviewer.reviewer_node(task(quality_status="pending", findings=[]))
        )
    assert out["quality_status"] == "unavailable" and out["forced_output"]
    assert out["error_code"] == "BUDGET_EXCEEDED"


def test_shared_call_limit_is_not_reset_between_agents_or_retries():
    from db import database, usage_repository
    from services.budget import ModelPolicy, BudgetExceeded
    from services.usage_service import measured_call, usage_context

    database.init_db()
    create = AsyncMock(
        return_value=SimpleNamespace(
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1)
        )
    )
    policy = ModelPolicy(
        max_task_tokens=10000,
        max_task_calls=2,
        max_task_seconds=10,
        completion_reserve=10,
    )

    async def scenario():
        for agent in ["writer", "reviewer", "rewriter"]:
            token = usage_context.set(("t", agent))
            try:
                await measured_call(
                    create, dict(model="fixture", messages=[]), policy=policy
                )
            finally:
                usage_context.reset(token)

    with pytest.raises(BudgetExceeded):
        asyncio.run(scenario())
    assert create.await_count == 2
    assert [r["status"] for r in usage_repository.list_usage("t")] == [
        "success",
        "success",
        "budget_exceeded",
    ]


def test_remaining_task_time_caps_call_and_running_reservation_blocks_overlap():
    from db import database, usage_repository
    from services.budget import ModelPolicy, BudgetExceeded
    from services.usage_service import measured_call, usage_context, ModelUsage

    database.init_db()
    policy = ModelPolicy(
        max_task_tokens=10000, max_task_seconds=1, completion_reserve=1
    )
    row = ModelUsage(
        call_id="old",
        task_id="t",
        agent="reviewer",
        model="fixture",
        status="success",
        duration_ms=750,
        input_tokens=1,
        output_tokens=1,
        usage_known=True,
        created_at="2026-11-09T00:00:00+00:00",
    )
    usage_repository.save(row)
    kwargs_seen = []

    async def provider(**kwargs):
        kwargs_seen.append(kwargs)
        assert kwargs["timeout"] <= 0.25
        pending = usage_repository.list_usage("t")[-1]
        assert pending["reserved_seconds"] <= 0.25
        with pytest.raises(BudgetExceeded):
            await measured_call(
                AsyncMock(), dict(model="fixture", messages=[]), policy=policy
            )
        return SimpleNamespace(usage=None)

    async def scenario():
        token = usage_context.set(("t", "rewriter"))
        try:
            return await measured_call(
                provider, dict(model="fixture", messages=[]), policy=policy
            )
        finally:
            usage_context.reset(token)

    asyncio.run(scenario())
    assert len(kwargs_seen) == 1


def test_research_node_cannot_bypass_one_research_limit():
    from agents import research_revision, react_researcher

    model = AsyncMock(side_effect=AssertionError("不允许调用模型"))
    with patch.object(react_researcher, "chat", new=model):
        out = asyncio.run(
            research_revision.research_revision_node(task(research_revision_count=1))
        )
    assert out["forced_output"] and out["error_code"] == "RESEARCH_LIMIT"
    model.assert_not_awaited()


def test_review_fault_is_routed_to_diagnostic_instead_of_body_target():
    from agents import reviewer
    from tests.test_rewriter import FakeResp

    with patch.object(
        reviewer, "chat", new=AsyncMock(return_value=FakeResp("invalid-json"))
    ):
        out = asyncio.run(reviewer.reviewer_node(task(findings=[])))
    plan = out["repair_plan"]
    assert plan["action"] == "stop" and plan["reason"] == "REVIEW_UNAVAILABLE"
    assert any(t["kind"] == "review" for t in plan["targets"])


@pytest.mark.parametrize("version", [2, 3])
def test_actual_sqlite_resume_keeps_best_draft_when_next_writer_hits_budget(
    offline_services,
    version,
):
    from db import database, task_repository as repo
    from memory.checkpointer import open_checkpointer
    from graph.workflow import build_graph, run_task, _initial_state
    from services.budget import BudgetExceeded

    database.init_db()
    repo.create_task(
        "budget-resume", "市场", metadata=dict(report_pipeline_version=version)
    )
    saved = _initial_state("budget-resume", "市场") | dict(
        report_pipeline_version=version,
        draft_content=BODY,
        draft_sections={"1": BODY},
        evidence=[source("budget-resume", excerpt="市场规模10亿元。")],
        completed_steps=[1],
        plan=[
            dict(id=1, agent="writer", task="初稿", depends_on=[]),
            dict(id=2, agent="writer", task="完善初稿", depends_on=[1]),
        ],
    )
    best_body = BODY
    if version == 3:
        from tests.test_report_document import example
        from services.report_document import (
            build_document,
            fact_context,
            render_document,
        )

        payload, _, _ = example()
        document = build_document(
            payload,
            "budget-resume",
            saved["evidence"],
            fact_context("budget-resume", saved["evidence"]),
        )
        best_body = render_document(document)
        saved.update(
            report_document=document,
            draft_content=best_body,
            draft_sections={"1": best_body},
        )
    offline_services["react_writer"].outputs = [BudgetExceeded("全任务额度耗尽")]

    async def scenario():
        config = dict(configurable=dict(thread_id="budget-resume"))
        async with open_checkpointer() as (cp, kind):
            assert kind == "sqlite"
            await build_graph(checkpointer=cp).aupdate_state(
                config, saved, as_node="scheduler"
            )
        result = await run_task("budget-resume", "市场", resume=True)
        assert (
            result["execution_status"] == "completed"
            and result["quality_status"] == "unavailable"
        )
        async with open_checkpointer() as (cp, kind):
            snapshot = await build_graph(checkpointer=cp).aget_state(config)
            assert not snapshot.next

    asyncio.run(scenario())
    result = repo.get_task("budget-resume")
    assert (
        result["final_report"] == best_body
        and result["error_code"] == "BUDGET_EXCEEDED"
    )
    assert result["review_history"][-1]["stop_reason"] == "BUDGET_EXCEEDED"


def test_no_targets_and_core_evidence_gap_are_not_body_rewrite():
    assert plan_repair(task(findings=[]))["reason"] == "NO_ACTIONABLE_TARGETS"
    gap = issue(rule="core_evidence_gap", decision="confirmed")
    assert plan_repair(task(findings=[gap]))["action"] == "research"
    assert (
        plan_repair(task(findings=[gap], research_revision_count=1))["reason"]
        == "RESEARCH_LIMIT"
    )
    assert plan_repair(task(quality_status="passed"))["reason"] == "QUALITY_PASSED"


def test_contracts_and_model_package_keep_internal_fields_out():
    import json
    from pathlib import Path
    from services.repair_plan import RepairPlan, model_repair_package
    from services.usage_service import ModelUsage

    definitions = json.loads(
        (Path(__file__).resolve().parents[2] / "docs/schemas/contracts.json").read_text(
            encoding="utf-8"
        )
    )["$defs"]
    shape = RepairPlan.model_json_schema(ref_template="#/$defs/{model}")
    nested = shape.pop("$defs")
    assert definitions["RepairPlan"] == shape
    assert all(definitions[k] == v for k, v in nested.items())
    assert (
        definitions["ModelUsage"]["properties"]["reserved_seconds"]
        == ModelUsage.model_json_schema()["properties"]["reserved_seconds"]
    )
    package = model_repair_package(plan_repair(task()))
    assert "context_key" not in json.dumps(package) and "digest" not in json.dumps(
        package
    )
    assert model_repair_package(None) is None


def test_cancelled_call_still_charged_after_context_reset():
    from db import database, usage_repository
    from services.budget import ModelPolicy, BudgetExceeded
    from services.usage_service import measured_call, usage_context

    database.init_db()
    policy = ModelPolicy(max_task_calls=1, max_task_tokens=10000, completion_reserve=1)

    async def scenario():
        entered = asyncio.Event()

        async def provider(**kwargs):
            entered.set()
            await asyncio.Event().wait()

        token = usage_context.set(("t", "reviewer"))
        try:
            call = asyncio.create_task(
                measured_call(
                    provider, dict(model="fixture", messages=[]), policy=policy
                )
            )
            await entered.wait()
            call.cancel()
            with pytest.raises(asyncio.CancelledError):
                await call
        finally:
            usage_context.reset(token)
        token = usage_context.set(("t", "rewriter"))
        try:
            with pytest.raises(BudgetExceeded):
                await measured_call(
                    AsyncMock(), dict(model="fixture", messages=[]), policy=policy
                )
        finally:
            usage_context.reset(token)

    asyncio.run(scenario())
    rows = usage_repository.list_usage("t")
    assert [r["status"] for r in rows] == ["cancelled", "budget_exceeded"]


@pytest.mark.parametrize(
    "name,value", [("MAX_TASK_MODEL_CALLS", "0"), ("MAX_TASK_MODEL_SECONDS", "nan")]
)
def test_invalid_shared_caps_reject_before_any_provider(name, value, monkeypatch):
    from services.budget import ModelPolicy
    from services.usage_service import UsagePersistenceError

    monkeypatch.setenv(name, value)
    with pytest.raises(UsagePersistenceError):
        ModelPolicy.from_env()


def test_old_running_ledger_without_reserved_seconds_is_not_free_time():
    from db import database, usage_repository
    from services.budget import BudgetExceeded, ModelPolicy
    from services.usage_service import ModelUsage, usage_context, measured_call

    database.init_db()
    old = ModelUsage(
        call_id="old",
        task_id="t",
        agent="reviewer",
        model="fixture",
        status="running",
        duration_ms=0,
        created_at="2026-11-09T00:00:00+00:00",
        reserved_tokens=1,
    )
    usage_repository.save(old)
    model = AsyncMock()

    async def scenario():
        token = usage_context.set(("t", "rewriter"))
        try:
            await measured_call(
                model,
                dict(model="fixture", messages=[]),
                policy=ModelPolicy(
                    max_task_tokens=10000, max_task_seconds=1, completion_reserve=1
                ),
            )
        finally:
            usage_context.reset(token)

    with pytest.raises(BudgetExceeded):
        asyncio.run(scenario())
    model.assert_not_awaited()


def test_usage_corruption_rejects_and_shared_transaction_save_roundtrips():
    from db import database, usage_repository
    from services.usage_service import ModelUsage

    database.init_db()
    row = ModelUsage(
        call_id="a",
        task_id="t",
        agent="writer",
        model="fixture",
        status="success",
        duration_ms=1,
        created_at="2026-11-09T00:00:00+00:00",
        reserved_seconds=3,
    )
    with database.get_connection() as conn:
        usage_repository.save(row, _connection=conn)
    assert usage_repository.list_usage("t")[0]["reserved_seconds"] == 3
    with database.get_connection() as conn:
        conn.execute("UPDATE model_usage SET task_id='other' WHERE call_id='a'")
    with pytest.raises(ValueError, match="归属损坏"):
        usage_repository.list_usage("other")


def test_source_claim_cannot_expand_v3_allowed_blocks_by_self_reported_full_scope():
    import json
    from agents import rewriter
    from tests.test_document_rewrite import fixture, replacement
    from tests.test_rewriter import FakeResp
    from services.report_document import render_document

    _, e, d, _ = fixture()
    state = task(
        report_pipeline_version=3,
        report_document=d,
        draft_content=render_document(d),
        evidence=e,
        findings=[
            issue(
                rule="unbound_number",
                decision="confirmed",
                scope="full",
                location=dict(paragraph=3, sentence=1),
            )
        ],
    )
    response = FakeResp(
        json.dumps(
            dict(actions=[dict(target="B3", operation="replace", value=replacement())])
        )
    )
    with patch.object(rewriter, "chat", new=AsyncMock(return_value=response)):
        out = asyncio.run(rewriter.rewriter_node(state))
    assert out["report_document"] == d
    assert out["review_history"][-1]["rewrite"]["rejected"]


def test_budget_stop_never_loses_existing_confirmed_blocker():
    from services.repair_plan import stop_repair

    out = stop_repair(task(quality_status="pending"), "BUDGET_EXCEEDED")
    assert out["quality_status"] == "rejected"


def test_reordered_target_set_and_evidence_do_not_bypass_no_progress():
    a = issue(
        finding_id="a", decision="confirmed", location=dict(paragraph=3, sentence=1)
    )
    b = issue(
        finding_id="b",
        rule="unsupported_wording",
        decision="confirmed",
        location=dict(paragraph=2, sentence=1),
    )
    state = task(
        findings=[a, b], evidence=[source(), source(source_id="S2", excerpt="补充来源")]
    )
    previous = plan_repair(state)
    changed = state | dict(
        findings=[b, a],
        evidence=list(reversed(state["evidence"])),
        review_history=[
            dict(repair_plan=previous, rewrite=dict(attempts=[{}], changed=False))
        ],
    )
    assert plan_repair(changed)["reason"] == "REPAIR_NO_PROGRESS"


def test_interrupted_usage_still_reserves_time_after_recovery():
    from db import database, usage_repository
    from services.budget import BudgetExceeded, ModelPolicy
    from services.usage_service import ModelUsage, usage_context, measured_call

    database.init_db()
    old = ModelUsage(
        call_id="old",
        task_id="t",
        agent="writer",
        model="fixture",
        status="failed",
        error_code="USAGE_INTERRUPTED",
        duration_ms=0,
        created_at="2026-11-09T00:00:00+00:00",
        reserved_tokens=1,
        reserved_seconds=5,
    )
    usage_repository.save(old)
    model = AsyncMock(return_value=SimpleNamespace(usage=None))

    async def scenario():
        token = usage_context.set(("t", "reviewer"))
        try:
            await measured_call(
                model,
                dict(model="fixture", messages=[]),
                policy=ModelPolicy(
                    max_task_tokens=10000, max_task_seconds=5, completion_reserve=1
                ),
            )
        finally:
            usage_context.reset(token)

    with pytest.raises(BudgetExceeded):
        asyncio.run(scenario())
    model.assert_not_awaited()


def test_research_budget_stop_ends_graph_without_entering_rewriter(offline_services):
    from graph.workflow import build_graph, _initial_state
    from memory.checkpointer import open_checkpointer
    from services.budget import BudgetExceeded

    offline_services["react_researcher"].outputs = [BudgetExceeded("额度耗尽")]
    saved = _initial_state("research-budget", "市场") | dict(
        report_pipeline_version=2,
        draft_content=BODY,
        quality_status="rejected",
        review_comments=["缺少证据"],
        findings=[],
        evidence=[],
    )

    async def scenario():
        config = dict(configurable=dict(thread_id="research-budget"))
        async with open_checkpointer() as (cp, kind):
            graph = build_graph(checkpointer=cp)
            await graph.aupdate_state(config, saved, as_node="reviewer")
            out = await graph.ainvoke(None, config)
            assert out["forced_output"] and out["error_code"] == "BUDGET_EXCEEDED"
            assert out["draft_content"] == BODY

    asyncio.run(scenario())
    assert not offline_services["rewriter"].calls
