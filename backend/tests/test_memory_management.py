"""记忆管理与重生成的真实HTTP/SQLite/Chroma边界。"""
import pytest
from fastapi.testclient import TestClient
from db.database import init_db
from db import task_repository as repo
from memory import long_term_memory as memory
from rag import store
from rag.loaders import Document
from tests.auth_support import ADMIN_HEADERS, READ_HEADERS
from tests.memory_support import approved_report


@pytest.fixture
def client(fake_embeddings, monkeypatch):
    init_db()
    monkeypatch.setattr(store, "_embeddings", fake_embeddings)
    monkeypatch.setattr(memory, "_MEMORY_ENABLED", True)
    from main import app
    with TestClient(app, headers=ADMIN_HEADERS) as c:
        yield c


def seed():
    report = "# 历史结构\n\n明确资料。[S1]"
    approved_report("主题", report, "old")
    assert memory.remember("主题", report, "old")
    return report


def test_management_separates_legacy_and_verified(client):
    seed()
    store.get_store(collection=store.MEMORY_COLLECTION).add_documents([Document("# 旧记忆", {"type":"report","topic":"旧内容","task_id":"missing"})])
    response = client.get("/api/v2/memory")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["total"] == 2
    assert {item["status"] for item in data["items"]} == {"active", "unverified"}
    assert client.get("/api/v2/memory", params={"status":"unverified"}).json()["data"]["total"] == 1


def test_delete_is_authorized_idempotent_and_suppresses_recall(client):
    seed()
    response = client.get("/api/v2/memory")
    assert response.status_code == 200
    key = response.json()["data"]["items"][0]["memory_id"]
    assert client.delete(f"/api/v2/memory/{key}", headers=READ_HEADERS).status_code == 403
    assert client.delete(f"/api/v2/memory/{key}").status_code == 200
    assert memory.recall("主题") == []
    assert client.delete(f"/api/v2/memory/{key}").json()["data"]["deleted"] is False


def test_filters_permissions_and_errors(client, monkeypatch):
    from unittest.mock import Mock
    from services import memory_management
    seed()
    assert client.get("/api/v2/memory", headers=READ_HEADERS).status_code == 200
    assert client.get("/api/v2/memory", headers={"Authorization":""}).status_code == 401
    for params in [{"page":"bad"},{"page":0},{"page_size":101},{"status":"passed"}]:
        response = client.get("/api/v2/memory", params=params)
        assert response.status_code == 422 and "error" in response.json()
    assert client.delete("/api/v2/memory/bad").status_code == 422
    assert client.delete("/api/v2/memory/" + "0"*64).status_code == 404
    assert client.get("/api/v2/memory",params={"page":2,"page_size":1}).json()["data"]["items"] == []
    monkeypatch.setattr(memory_management,"_items", Mock(side_effect=RuntimeError("secret-index")))
    response = client.get("/api/v2/memory")
    assert response.status_code == 503 and "secret-index" not in response.text
    with pytest.raises(ValueError):
        memory_management.list_memories(0)


def test_legacy_deletion_and_index_failure_tombstone(client, monkeypatch):
    from unittest.mock import Mock
    from db import memory_repository
    report = seed()
    index = store.get_store(collection=store.MEMORY_COLLECTION)
    index.add_documents([Document("旧文本",{"type":"report","topic":"旧记录"})])
    key = client.get("/api/v2/memory",params={"status":"unverified"}).json()["data"]["items"][0]["memory_id"]
    assert client.delete(f"/api/v2/memory/{key}").status_code == 200
    assert client.get("/api/v2/memory",params={"status":"unverified"}).json()["data"]["total"] == 0
    active_key = client.get("/api/v2/memory",params={"status":"active"}).json()["data"]["items"][0]["memory_id"]
    with monkeypatch.context() as scoped:
        scoped.setattr(index,"delete_by_ids",Mock(side_effect=RuntimeError("secret-delete")))
        response = client.delete(f"/api/v2/memory/{active_key}")
        assert response.status_code == 503 and "secret-delete" not in response.text
    assert memory_repository.get(active_key).status == "deleted"
    assert memory.recall("主题") == []
    assert memory.remember("主题",report,"old") is False
    assert client.delete(f"/api/v2/memory/{active_key}").status_code == 200
    assert client.delete("/api/v1/tasks/old").status_code == 200
    assert memory_repository.get(active_key).status == "deleted"


def test_task_deletion_invalidates_in_same_transaction(client):
    from db import memory_repository
    seed()
    key = memory_repository.entries()[0].memory_id
    assert client.delete("/api/v1/tasks/old").status_code == 200
    assert memory_repository.get(key).invalid_reason == "task_deleted"
    assert memory.recall("主题") == []
    assert client.get("/api/v2/memory",params={"status":"invalid"}).json()["data"]["total"] == 1


def test_disabled_management_and_current_source_invalidation(client):
    seed()
    memory.set_enabled(False)
    assert client.get("/api/v2/memory").json()["data"]["total"] == 1
    assert client.get("/api/v1/memory/status").json()["count"] == 0
    memory.set_enabled(True)
    repo.update_task("old",quality_status="rejected")
    data = client.get("/api/v2/memory").json()["data"]
    assert data["items"][0]["status"] == "invalid" and data["counts"]["active"] == 0


def test_regeneration_preserves_original_and_creates_real_task(client, offline_evidence, monkeypatch):
    import time
    import re
    monkeypatch.setattr(memory,"_MEMORY_ENABLED",True)
    original = seed()
    doc = store.ingest_file(b"new selected material", "new.txt")
    repo.update_task("old",document_ids=[doc["document_id"]])
    offline_evidence["planner"].outputs = ['[{"id":1,"agent":"writer","task":"新报告","depends_on":[]}]']
    offline_evidence["react_writer"].outputs = [lambda messages:
        "# 新报告\n\n## 执行摘要\n建议核对资料。\n\n## 正文\nnew selected material["
        + re.search(r"^\[(F[a-f0-9]+)\]", messages[-1]["content"], re.M)[1]
        + "]\n\n## 结论与建议\n建议后续跟踪。"]
    offline_evidence["reviewer"].outputs = ['{"passed":true,"comments":[]}']
    assert client.post("/api/v2/tasks/old/regenerate",headers=READ_HEADERS).status_code == 403
    response = client.post("/api/v2/tasks/old/regenerate")
    assert response.status_code == 200
    result = response.json()["data"]
    assert result["task_id"] != "old" and result["regenerated_from"] == "old"
    for _ in range(100):
        new = repo.get_task(result["task_id"])
        if new["status"] in {"completed","failed"}:
            break
        time.sleep(0.05)
    assert new["quality_status"] == "passed" and new["regenerated_from"] == "old"
    assert new["document_ids"] == [doc["document_id"]]
    assert repo.get_task("old")["final_report"] == original
    assert client.get(f"/api/v2/tasks/{new['task_id']}/report").json()["data"]["regenerated_from"] == "old"
    repo.update_task(new["task_id"], regenerated_from="forged")
    assert repo.get_task(new["task_id"])["regenerated_from"] == "old"  # 溯源创建后不可通过更新白名单改写
    assert client.post("/api/v2/tasks/absent/regenerate").status_code == 404
    repo.create_task("running","主题")
    assert client.post("/api/v2/tasks/running/regenerate").status_code == 409


def test_source_check_temporarily_unavailable_is_not_deleted(client, monkeypatch):
    from services.retrieval_service import retrieve_documents, RetrievalOptions
    from unittest.mock import Mock
    from db import memory_repository
    report = "文档声明。[S1]"
    approved_report("主题",report,"document")
    upload = store.ingest_file(b"fixture document", "doc.txt")
    evidence = retrieve_documents("fixture document",[upload["document_id"]],task_id="document",options=RetrievalOptions(threshold=-1.0))["evidence"]
    repo.update_task("document",evidence=evidence)
    assert memory.remember("主题",report,"document")
    with monkeypatch.context() as scoped:
        scoped.setattr(store.get_store(),"get_documents",Mock(side_effect=RuntimeError("offline")))
        item = client.get("/api/v2/memory").json()["data"]["items"][0]
        assert item["status"] == "unavailable" and not item["verified"]
    assert memory_repository.entries()[0].status == "active"


def test_regeneration_database_failure_is_not_success(client, monkeypatch):
    from unittest.mock import Mock
    seed()
    monkeypatch.setattr(repo,"create_task",Mock(side_effect=RuntimeError("secret-db")))
    response = client.post("/api/v2/tasks/old/regenerate")
    assert response.status_code == 503 and "secret-db" not in response.text


def test_corrupt_source_task_does_not_create_phantom_regeneration(client):
    from db.database import get_connection
    seed()
    with get_connection() as conn:
        conn.execute("UPDATE tasks SET document_ids='broken' WHERE task_id='old'")
    response = client.post("/api/v2/tasks/old/regenerate")
    assert response.status_code == 503 and response.json()["error"]["code"] == "TASK_CREATE_FAILED"
    with get_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1


def test_memory_schema_upgrade_is_idempotent_without_trusting_old_data(tmp_path):
    import sqlite3
    path = tmp_path / "old-memory.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE tasks(task_id TEXT PRIMARY KEY, topic TEXT, status TEXT, final_report TEXT, created_at TEXT, updated_at TEXT)")
        conn.execute("INSERT INTO tasks VALUES('legacy','旧主题','completed','旧正文','now','now')")
    init_db(path)
    init_db(path)
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM memory_entries").fetchone()[0] == 0
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    task = repo.get_task("legacy")
    assert task["regenerated_from"] is None and task["quality_status"] == "unknown" and task["final_report"] == "旧正文"


def test_task_deletion_failure_rolls_back_memory_invalidation(client):
    import sqlite3
    from db.database import get_connection
    from db import memory_repository
    seed()
    key = memory_repository.entries()[0].memory_id
    repo.append_log("old",step_id=1,log_type="Thought",content="删除回滚夹具")
    with get_connection() as conn:
        conn.execute("CREATE TRIGGER reject_task_delete BEFORE DELETE ON tasks BEGIN SELECT RAISE(ABORT,'fixture-delete-failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        repo.delete_task("old")
    assert repo.get_task("old") is not None and len(repo.get_logs("old")) == 1
    assert memory_repository.get(key).status == "active"
