"""结构化内容、声明定位、绑定、渲染及schema5的真实边界。"""

import copy
import json
import sqlite3
import pytest
from services.report_document import (
    build_document,
    render_document,
    validate_document,
    fact_context,
    DocumentError,
)
from tests.test_evidence import source


def example():
    evidence = [source(excerpt="市场规模10亿元。")]
    context = fact_context("t", evidence)
    fact = dict(
        kind="fact",
        segments=[dict(text="市场规模10亿元。", facts=["F1"])],
    )
    payload = dict(
        title="市场报告",
        sections=[
            dict(
                role=role,
                title="概要" if role == "summary" else role,
                blocks=[
                    copy.deepcopy(fact)
                    if role != "conclusion"
                    else dict(kind="recommendation", segments=[dict(text="建议继续观察。")])
                ],
            )
            for role in ("summary", "body", "conclusion")
        ],
    )
    return payload, evidence, context


def test_deterministic_render_and_server_ids():
    payload, evidence, ctx = example()
    document = build_document(payload, "t", evidence, ctx)
    first = render_document(document)
    assert first == render_document(json.loads(json.dumps(document)))
    assert first.startswith("# 市场报告\n\n## 执行摘要")
    assert first.count("# 市场报告") == 1 and "## 结论与建议" in first
    assert "市场规模10亿元。[S1]" in first and "## 来源附录" in first
    assert document["sections"][0]["blocks"][0]["claims"][0]["claim_id"]
    assert "claim_id" not in json.dumps(payload)
    assert "segments" in payload["sections"][0]["blocks"][0]


@pytest.mark.parametrize(
    "fault",
    [
        "empty",
        "missing_facts",
        "unknown",
        "cross_task",
        "stale",
        "skeleton",
    ],
)
def test_document_rejects_ambiguous_or_unsupported_content(fault):
    p, e, c = example()
    block = p["sections"][1]["blocks"][0]
    if fault == "empty":
        block["segments"] = []
    elif fault == "missing_facts":
        block["segments"][0]["facts"] = []
    elif fault == "unknown":
        block["segments"][0]["facts"] = ["F99999"]
    elif fault == "cross_task":
        c["task_id"] = "other"
    elif fault == "stale":
        c["registry_revision"] = "stale"
    else:
        p["sections"][0]["blocks"] = []
    with pytest.raises(ValueError):
        build_document(p, "t", e, c)


def test_block_kind_does_not_exempt_unbound_number():
    p, e, c = example()
    p["sections"][1]["blocks"] = [
        dict(kind="transition", segments=[dict(text="预计增长99%。")])
    ]
    with pytest.raises(DocumentError):
        build_document(p, "t", e, c)


def test_multisource_conflict_retained_and_missing_side_rejected():
    e = [
        source(excerpt="甲口径市场规模10亿元。"),
        source(excerpt="乙口径市场规模20亿元。", url="https://example.com/other"),
    ]
    e[1]["source_id"] = "S2"
    c = fact_context("t", e)
    p, _, _ = example()
    text = "甲口径市场规模10亿元，乙口径市场规模20亿元；来源存在差异，原因未知。"
    p["sections"][1]["blocks"] = [
        dict(
            kind="analysis",
            segments=[dict(text=text, facts=list(c["aliases"]))],
        )
    ]
    d = build_document(p, "t", e, c)
    assert text + "[S1][S2]" in render_document(d)
    p["sections"][1]["blocks"][0]["segments"][0]["facts"] = list(c["aliases"])[:1]
    with pytest.raises(ValueError):
        build_document(p, "t", e, c)


def test_code_table_list_and_literal_alias_are_not_globally_replaced():
    p, e, c = example()
    text = (
        "```txt\nF1 [S999] 99亿元\n```\n\n- 说明 F1\n\n|名称|值|\n|---|---|\n|F1|说明|"
    )
    p["sections"][1]["blocks"].append(
        dict(kind="analysis", segments=[dict(text=text)])
    )
    assert text in render_document(build_document(p, "t", e, c))


def test_internal_tampering_and_cross_task_rejected():
    p, e, c = example()
    d = build_document(p, "t", e, c)
    with pytest.raises(ValueError):
        validate_document(d, "other")
    d["sections"][0]["blocks"][0]["claims"][0]["end"] = 999
    with pytest.raises(ValueError):
        render_document(d)


def test_v4_migration_idempotent_preserves_legacy_and_doc_roundtrip(tmp_path):
    from db import database, task_repository as repo

    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE tasks(task_id TEXT PRIMARY KEY,topic TEXT,status TEXT,final_report TEXT,updated_at TEXT)"
        )
        conn.execute(
            "INSERT INTO tasks(task_id,topic,status,final_report) VALUES('legacy','旧任务','completed','旧报告')"
        )
        conn.execute("PRAGMA user_version=4")
    database.init_db(path)
    database.init_db(path)
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 5
        assert conn.execute(
            "SELECT final_report,report_document FROM tasks"
        ).fetchone() == ("旧报告", None)
        assert (
            len(
                conn.execute(
                    "SELECT version FROM schema_migrations WHERE version=5"
                ).fetchall()
            )
            == 1
        )
    p, e, c = example()
    d = build_document(p, "t", e, c)
    repo.create_task("t", "新任务", metadata={"report_pipeline_version": 3})
    repo.update_task("t", report_document=d)
    assert repo.get_task("t")["report_document"] == d
    with pytest.raises(ValueError):
        repo.update_task("legacy", report_document=d)


def test_document_backup_restore_to_isolated_copy(tmp_path):
    from db import database, task_repository as repo
    from services.data_backup import create_backup, restore_backup

    root = tmp_path / "source"
    database.init_db(root / "data.db")
    p, e, c = example()
    d = build_document(p, "t", e, c)
    repo.create_task("t", "报告", metadata={"report_pipeline_version": 3})
    repo.update_task("t", report_document=d)
    create_backup(root, tmp_path / "backup", "sb-R02-verified", stopped=True)
    restore_backup(tmp_path / "backup", tmp_path / "restored", "sb-R02-verified")
    database.init_db(tmp_path / "restored/data.db")
    assert repo.get_task("t")["report_document"] == d


@pytest.mark.parametrize(
    "fault",
    [
        "title",
        "role",
        "source_key",
        "section_title",
        "identity",
        "heading",
        "claim_identity",
        "claim_source",
        "unknown_fact",
        "no_fact",
    ],
)
def test_internal_document_rejects_tampered_structure_or_binding(fault):
    p, e, c = example()
    d = build_document(p, "t", e, c)
    block = d["sections"][1]["blocks"][0]
    claim = block["claims"][0]
    if fault == "title":
        d["title"] = "# 另一标题"
    elif fault == "role":
        d["sections"][0]["role"] = "body"
    elif fault == "source_key":
        d["sources"]["S999"] = d["sources"].pop("S1")
    elif fault == "section_title":
        d["sections"][0]["title"] = "第一行\n第二行"
    elif fault == "identity":
        d["sections"][1]["section_id"] = d["sections"][0]["section_id"]
    elif fault == "heading":
        block["text"] = "# 额外标题\n" + block["text"]
    elif fault == "claim_identity":
        claim["claim_id"] = d["sections"][0]["blocks"][0]["claims"][0]["claim_id"]
    elif fault == "claim_source":
        claim["source_ids"] = []
    elif fault == "unknown_fact":
        claim["fact_ids"] = ["Funknown"]
    else:
        claim["fact_ids"] = []
    with pytest.raises(ValueError):
        render_document(d)


def test_analysis_saves_fact_premises_and_unquantified_comment():
    p, e, c = example()
    block = p["sections"][1]["blocks"][0]
    block["kind"] = "analysis"
    block["segments"][0]["premises"] = block["segments"][0].pop("facts")
    p["sections"][2]["blocks"][0]["segments"] = [dict(text="建议继续观察。")]
    d = build_document(p, "t", e, c)
    claim = d["sections"][1]["blocks"][0]["claims"][0]
    assert claim["premise_ids"] and not claim["fact_ids"]
    assert "市场规模10亿元。[S1]" in render_document(d)


def test_report_document_channel_survives_real_sqlite_checkpoint(tmp_path):
    from langgraph.graph import StateGraph, START, END
    from langgraph.checkpoint.sqlite import SqliteSaver
    from graph.state import AgentState

    p, e, c = example()
    document = build_document(p, "t", e, c)
    builder = StateGraph(AgentState)
    builder.add_node("save", lambda state: {"report_document": document})
    builder.add_edge(START, "save")
    builder.add_edge("save", END)
    config = {"configurable": {"thread_id": "document"}}
    with SqliteSaver.from_conn_string(str(tmp_path / "checkpoint.db")) as cp:
        graph = builder.compile(checkpointer=cp)
        assert graph.invoke({"task_id": "t"}, config)["report_document"] == document
    with SqliteSaver.from_conn_string(str(tmp_path / "checkpoint.db")) as cp:
        graph = builder.compile(checkpointer=cp)
        assert graph.get_state(config).values["report_document"] == document


def test_document_source_appendix_keeps_document_locator():
    p, _, _ = example()
    evidence = [
        source(
            kind="document",
            url=None,
            document_id="doc1",
            chunk_id="chunk1",
            page=3,
            excerpt="市场规模10亿元。",
        )
    ]
    d = build_document(p, "t", evidence)
    appendix = render_document(d).split("## 来源附录")[1]
    assert "doc1" in appendix and "3" in appendix and "None" not in appendix
    assert d["sources"]["S1"]["source_deleted"] is False
