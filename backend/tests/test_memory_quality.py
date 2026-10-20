"""质量记忆准入：真实SQLite与Chroma，仅替换嵌入/模型边界。"""
import pytest
from db import task_repository as repo
from db.database import init_db
from memory import long_term_memory as memory
from rag import store
from rag.loaders import Document


@pytest.fixture(autouse=True)
def isolated(fake_embeddings, monkeypatch):
    init_db()
    monkeypatch.setattr(store, "_embeddings", fake_embeddings)
    monkeypatch.setattr(memory, "_MEMORY_ENABLED", True)


def approved(task_id="t", *, status="completed", quality="passed", forced=False):
    report = "# 研究结构\n\n明确的测试资料。[S1]"
    repo.create_task(task_id, "测试主题")
    repo.update_task(task_id, status=status, quality_status=quality, forced_output=forced, final_report=report,
        evidence=[dict(source_id="S1", task_id=task_id, kind="web", title="测试来源", excerpt="明确的测试资料",
        url="https://example.invalid/fixture", retrieved_at="2026-10-20T00:00:00Z")])
    return report


@pytest.mark.parametrize("status,quality,forced", [("completed","rejected",False),("completed","unavailable",False),
    ("failed","not_run",False),("cancelled","passed",False),("completed","passed",True),("running","passed",False)])
def test_ineligible_never_written(status, quality, forced):
    report = approved(status=status, quality=quality, forced=forced)
    assert memory.remember("测试主题", report, "t") is False
    assert store.get_store(collection=store.MEMORY_COLLECTION).count() == 0


def test_old_unverified_never_recalled():
    index = store.get_store(collection=store.MEMORY_COLLECTION)
    index.add_documents([Document("# 旧报告", {"type":"report", "task_id":"old", "topic":"历史"})])
    assert memory.recall("历史") == []
    assert index.count() == 1


def test_valid_version_is_idempotent_and_persistent():
    report = approved()
    assert memory.remember("测试主题", report, "t") is True
    assert memory.remember("测试主题", report, "t") is False
    index = store.get_store(collection=store.MEMORY_COLLECTION)
    assert index.count() == 1
    store._stores.clear()
    hits = memory.recall("测试主题")
    assert len(hits) == 1 and hits[0]["verified"] is True
    assert hits[0]["prompt_version"] and hits[0]["quality_status"] == "passed"


def test_missing_or_changed_sources_cannot_enter():
    report = approved()
    repo.update_task("t", evidence=[])
    assert memory.remember("测试主题", report, "t") is False
    assert memory.remember("伪造主题", report, "missing") is False


def test_disabled_keeps_data_and_source_changes_invalidate():
    report = approved()
    assert memory.remember("测试主题", report, "t")
    memory.set_enabled(False)
    assert memory.recall("测试主题") == []
    assert memory.remember("测试主题", report, "t") is False
    assert store.get_store(collection=store.MEMORY_COLLECTION).count() == 1
    memory.set_enabled(True)
    assert memory.recall("测试主题")
    repo.update_task("t", quality_status="rejected")
    assert memory.recall("测试主题") == []
    from db import memory_repository
    assert memory_repository.entries()[0].status == "invalid"


def test_embedding_failure_and_interrupted_activation_retry(monkeypatch, caplog):
    from db import memory_repository
    from unittest.mock import Mock
    report = approved()
    index = store.get_store(collection=store.MEMORY_COLLECTION)
    with monkeypatch.context() as scoped:
        scoped.setattr(index.embeddings, "embed_documents", Mock(side_effect=RuntimeError("secret-key")))
        assert memory.remember("测试主题", report, "t") is False
    assert memory_repository.entries()[0].status == "unavailable"
    with monkeypatch.context() as scoped:
        scoped.setattr(memory_repository, "transition", Mock(side_effect=RuntimeError("interrupted")))
        assert memory.remember("测试主题", report, "t") is False
    assert index.count() == 1 and memory.recall("测试主题") == []
    with monkeypatch.context() as scoped:
        embedding = Mock(side_effect=AssertionError("重复嵌入"))
        scoped.setattr(index.embeddings, "embed_documents", embedding)
        assert memory.remember("测试主题", report, "t") is True
        embedding.assert_not_called()
    assert "secret-key" not in caplog.text


def test_hash_identity_and_missing_task_are_rechecked():
    from db import memory_repository
    report = approved()
    assert memory.remember("伪造主题", report, "t") is False
    assert memory.remember("测试主题", report + "伪造正文", "t") is False
    assert memory.remember("测试主题", report, "t")
    repo.update_task("t", final_report="新报告。[S1]")
    assert memory.recall("测试主题") == []
    assert memory_repository.entries()[0].invalid_reason == "report_changed"
    assert memory.remember("测试主题", "新报告。[S1]", "t")
    assert len(memory_repository.entries()) == 2
    repo.delete_task("t")
    assert memory.recall("测试主题") == []


def test_empty_corrupt_and_deleted_document_sources(monkeypatch):
    from services.memory_policy import eligibility
    from db.database import get_connection
    report = approved()
    repo.update_task("t", final_report="")
    assert eligibility("t")[1] == "report_empty"
    repo.update_task("t", final_report=report)
    with get_connection() as conn:
        conn.execute("UPDATE tasks SET evidence='[{}]' WHERE task_id='t'")
    assert eligibility("t")[1] == "sources_invalid"
    upload = store.ingest_file(b"fixture fact", "fact.txt")
    from services.retrieval_service import retrieve_documents, RetrievalOptions
    evidence = retrieve_documents("fixture fact", [upload["document_id"]], task_id="t", options=RetrievalOptions(threshold=-1.0))["evidence"]
    repo.update_task("t", evidence=evidence)
    assert memory.remember("测试主题", report, "t")
    from unittest.mock import Mock
    with monkeypatch.context() as scoped:
        scoped.setattr(store.get_store(), "get_documents", Mock(side_effect=RuntimeError("offline")))
        assert eligibility("t")[1] == "source_check_unavailable"
        assert memory.recall("测试主题") == []
    store.delete_document_by_id(upload["document_id"])
    assert memory.recall("测试主题") == []


def test_real_workflow_only_admits_verified_report(offline_evidence, monkeypatch):
    import asyncio
    from graph.workflow import run_task
    from db import memory_repository
    monkeypatch.setattr(memory, "_MEMORY_ENABLED", True)
    repo.create_task("graph-memory", "主题")
    offline_evidence["planner"].outputs = ['[{"id":1,"agent":"researcher","task":"资料","depends_on":[]},{"id":2,"agent":"writer","task":"写作","depends_on":[1]}]']
    offline_evidence["react_writer"].outputs = ['# 报告结构\n\n固定测试声明。[S1]']
    offline_evidence["reviewer"].outputs = ['{"passed":true,"comments":[]}']
    final = asyncio.run(run_task("graph-memory", "主题"))
    assert final["quality_status"] == "passed"
    entry = memory_repository.entries()[0]
    assert entry.task_id == "graph-memory" and entry.verified and entry.model_version is None
    assert memory.recall("主题")[0]["memory_id"] == entry.memory_id


def test_index_conflict_and_result_tampering_are_not_trusted(monkeypatch):
    from db import memory_repository
    from unittest.mock import Mock
    import hashlib
    from services.memory_policy import digest_report
    report = approved()
    key = hashlib.sha256(f"t\0{digest_report(report)}".encode()).hexdigest()
    index = store.get_store(collection=store.MEMORY_COLLECTION)
    index.add_documents([Document("伪造索引正文", {"type":"report", "memory_id":key, "verified":True, "task_id":"t", "report_hash":digest_report(report)})])
    assert memory.remember("测试主题", report, "t") is False
    assert memory_repository.get(key).invalid_reason == "index_conflict"
    assert memory_repository.transition("missing", "active") is False
    assert memory_repository.transition(key, "active") is False
    second = approved("t2")
    assert memory.remember("测试主题", second, "t2")
    entry = next(e for e in memory_repository.entries() if e.task_id == "t2")
    monkeypatch.setattr(index, "similarity_search", Mock(return_value=[(Document("替换正文", {"memory_id":entry.memory_id,"task_id":"t2","report_hash":entry.report_hash}), 0.9)]))
    assert memory.recall("测试主题") == []
    monkeypatch.setattr(memory_repository, "get", Mock(return_value=None))
    assert memory._active(entry) is False


def test_corrupt_record_rejected_and_query_failure_is_safe(monkeypatch, caplog):
    from db import memory_repository
    from services.memory_policy import MemoryEntry
    from pydantic import ValidationError
    from unittest.mock import Mock
    report = approved()
    assert memory.remember("测试主题", report, "t")
    entry = memory_repository.entries()[0]
    for changes in [dict(status="deleted"), dict(quality_status="unknown"), dict(index_hash=None)]:
        with pytest.raises(ValidationError):
            MemoryEntry.model_validate(entry.model_dump() | changes)
    index = store.get_store(collection=store.MEMORY_COLLECTION)
    monkeypatch.setattr(index, "similarity_search", Mock(side_effect=RuntimeError("secret-query")))
    assert memory.recall("测试主题") == []
    assert "secret-query" not in caplog.text


def test_source_mutation_during_indexing_cannot_activate(monkeypatch):
    from db import memory_repository
    report = approved()
    assert memory.is_enabled() is True
    index = store.get_store(collection=store.MEMORY_COLLECTION)
    original = index.add_documents
    def changed(docs):
        result = original(docs)
        repo.update_task("t", quality_status="rejected")
        return result
    monkeypatch.setattr(index, "add_documents", changed)
    assert memory.remember("测试主题", report, "t") is False
    assert memory_repository.entries()[0].status == "invalid"


def test_active_registration_with_missing_vector_is_not_recalled():
    report = approved()
    assert memory.remember("测试主题", report, "t")
    index = store.get_store(collection=store.MEMORY_COLLECTION)
    index.delete_by_ids([item["id"] for item in index.get_documents()])
    assert memory.recall("测试主题") == []
