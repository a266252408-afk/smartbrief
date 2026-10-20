"""上传文档选取的真实Chroma/SQLite离线行为，不替换筛选规则。"""
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from db import task_repository
from db.database import init_db
from rag import store
from services.retrieval_service import DocumentSelection, RetrievalOptions, get_retrievals, retrieve_documents


@pytest.fixture
def documents(fake_embeddings, monkeypatch):
    monkeypatch.setattr(store, "_embeddings", fake_embeddings)
    init_db()
    a = store.ingest_file("独有事实：蓝鲸项目交付137台设备".encode(), "资料.txt")
    b = store.ingest_file("另一事实：红杉项目交付29台设备".encode(), "资料.txt")
    return a, b


def test_selected_document_fact_and_real_chunk(documents):
    a, b = documents
    result = retrieve_documents("独有事实：蓝鲸项目交付137台设备", [a["document_id"]], task_id="t", options=RetrievalOptions(threshold=-1.0))
    assert result["status"] == "matched"
    e = result["evidence"][0]
    assert "137" in e["excerpt"] and e["document_id"] == a["document_id"]
    assert e["chunk_id"] == store.get_document_chunks("资料.txt", "upload")[0]["id"] or e["chunk_id"] == store.get_document_chunks("资料.txt", "upload")[1]["id"]
    other = retrieve_documents("独有事实", [b["document_id"]], task_id="other", options=RetrievalOptions(threshold=-1.0))
    assert "29" in other["evidence"][0]["excerpt"]
    assert other["evidence"][0]["task_id"] == "other"


def test_no_selection_missing_and_threshold(documents):
    a, _ = documents
    assert retrieve_documents("主题", [], task_id="t")["status"] == "not_selected"
    assert retrieve_documents("主题", ["0" * 32], task_id="t")["status"] == "documents_missing"
    result = retrieve_documents("不相关内容", [a["document_id"]], task_id="t", options=RetrievalOptions(threshold=1.0))
    assert result["status"] == "below_threshold" and result["evidence"] == []


def test_embedding_failure_is_not_no_hits(documents, monkeypatch):
    a, _ = documents
    embedding = store.get_store().embeddings
    monkeypatch.setattr(embedding, "embed_query", Mock(side_effect=RuntimeError("secret-key")))
    result = retrieve_documents("主题", [a["document_id"]], task_id="t")
    assert result["status"] == "embedding_failed"
    assert "secret-key" not in str(result)


def test_budget_and_top_k(documents):
    a, b = documents
    result = retrieve_documents("主题", [a["document_id"], b["document_id"]], task_id="t", options=RetrievalOptions(top_k=1, threshold=-1.0))
    assert len(result["evidence"]) == 1
    assert sum(len(e["excerpt"]) for e in result["evidence"]) <= result["options"]["context_budget"]


def test_persistent_record_readback(documents):
    a, _ = documents
    task_repository.create_task("t", "主题")
    result = retrieve_documents("主题", [a["document_id"]], task_id="t", options=RetrievalOptions(threshold=-1.0))
    task_repository.update_task("t", document_ids=[a["document_id"]], retrievals=[result])
    assert get_retrievals("t")["retrievals"] == [result]
    assert get_retrievals("absent") is None


@pytest.mark.parametrize("ids", [["../bad"], ["a" * 32] * 2, ["a" * 32] * 21])
def test_invalid_selection(ids):
    with pytest.raises(ValidationError):
        DocumentSelection(document_ids=ids)


def test_empty_hits_budget_index_and_malformed(documents, monkeypatch):
    a, _ = documents
    index = store.get_store()
    monkeypatch.setattr(index, "similarity_search", Mock(return_value=[]))
    assert retrieve_documents("q", [a["document_id"]], task_id="t")["status"] == "no_hits"
    from rag.loaders import Document
    monkeypatch.setattr(index, "get_documents", Mock(return_value=[{"id": "chunk", "metadata": {"document_id": a["document_id"]}, "content": "长" * 300}]))
    monkeypatch.setattr(index, "similarity_search", Mock(return_value=[(Document("长" * 300, {"document_id": a["document_id"]}), 0.9)]))
    assert retrieve_documents("q", [a["document_id"]], task_id="t", options=RetrievalOptions(context_budget=256))["status"] == "budget_excluded"
    monkeypatch.setattr(index, "similarity_search", Mock(side_effect=RuntimeError("private")))
    assert retrieve_documents("q", [a["document_id"]], task_id="t")["status"] == "index_failed"
    monkeypatch.setattr(index, "get_documents", Mock(side_effect=RuntimeError("private")))
    assert retrieve_documents("q", [a["document_id"]], task_id="t")["error_code"] == "DOCUMENT_INDEX_FAILED"


def test_invalid_records_and_cross_task(documents):
    a, _ = documents
    from services.retrieval_service import RetrievalRecord
    from services.evidence import Evidence
    with pytest.raises(ValidationError):
        RetrievalRecord(task_id="t", query="q", document_ids=[], status="matched")
    e = Evidence(source_id="S1", task_id="other", kind="document", title="title", excerpt="text", document_id=a["document_id"], chunk_id="c", retrieved_at="2026-10-20T00:00:00Z")
    with pytest.raises(ValidationError):
        RetrievalRecord(task_id="t", query="q", document_ids=[a["document_id"]], status="matched", evidence=[e])
    result = retrieve_documents("q", [a["document_id"]], task_id="other", options=RetrievalOptions(threshold=-1.0))
    task_repository.create_task("t", "q")
    with pytest.raises(ValueError):
        task_repository.update_task("t", retrievals=[result])


def test_retrieval_api_permissions_and_corruption(documents):
    from main import app
    from fastapi.testclient import TestClient
    from tests.auth_support import ADMIN_HEADERS, READ_HEADERS
    from db.database import get_connection
    task_repository.create_task("t", "q")
    with TestClient(app, headers=ADMIN_HEADERS) as client:
        assert client.get("/api/v2/tasks/t/retrievals").json()["data"]["retrievals"] == []
        assert client.get("/api/v2/tasks/t/retrievals", headers=READ_HEADERS).status_code == 200
        assert client.get("/api/v2/tasks/absent/retrievals").status_code == 404
        assert client.get("/api/v2/tasks/t/retrievals", headers={"Authorization": ""}).status_code == 401
        assert client.post("/api/v1/generate", json={"topic": "q", "document_ids": ["bad"]}).status_code == 422
        assert client.post("/api/v1/generate", json={"topic": "q"}, headers=READ_HEADERS).status_code == 403
        with get_connection() as conn:
            conn.execute("UPDATE tasks SET retrievals='broken' WHERE task_id='t'")
        response = client.get("/api/v2/tasks/t/retrievals")
        assert response.status_code == 500 and response.json()["error"]["code"] == "RETRIEVALS_INVALID"


def test_old_database_migration_idempotent(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE tasks(task_id TEXT PRIMARY KEY, topic TEXT, status TEXT, final_report TEXT, created_at TEXT, updated_at TEXT)")
        conn.execute("INSERT INTO tasks VALUES('old','q','completed','原报告','now','now')")
    init_db(path)
    init_db(path)
    task = task_repository.get_task("old")
    assert task["document_ids"] == [] and task["retrievals"] == []
    assert task["final_report"] == "原报告" and task["quality_status"] == "unknown"
