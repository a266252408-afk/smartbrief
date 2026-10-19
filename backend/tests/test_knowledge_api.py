"""API 测试：知识库（上传/列表/检索）、长期记忆检索、任务断点恢复。

全部离线：FakeEmbeddings 注入 rag_store，规避真实嵌入网络调用；
conftest autouse 已把 Chroma / Checkpointer / RAG seed 隔离到 tmp_path。
"""
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from db import task_repository
from db.database import init_db
from memory import long_term_memory
from rag import store as rag_store


class FakeEmbeddings:
    """确定性假嵌入（8 维，md5 派生），保证离线可入库/检索。"""

    dim = 8

    def _vec(self, text: str) -> list:
        import hashlib

        digest = hashlib.md5(text.encode("utf-8")).digest()
        return [b / 255.0 for b in digest[: self.dim]]

    def embed_documents(self, texts: list) -> list:
        return [self._vec(t) for t in texts]

    def embed_query(self, text: str) -> list:
        return self._vec(text)


@pytest.fixture(autouse=True)
def _fake_embeddings():
    rag_store._embeddings = FakeEmbeddings()
    yield
    rag_store._stores.clear()
    rag_store._embeddings = None


@pytest.fixture
def client(tmp_path, monkeypatch):
    """隔离配置路径与 SQLite 库文件，启动应用（与 test_tasks_api 同款）。"""
    monkeypatch.setattr("mcp_client.mcp_client.CONFIG_PATH", tmp_path / "mcp_config.json")
    monkeypatch.setattr("tools.builtin_tools.CONFIG_PATH", tmp_path / "tool_config.json")
    monkeypatch.setattr("db.database.DB_PATH", tmp_path / "api_test.db")
    init_db(tmp_path / "api_test.db")
    from main import app

    with TestClient(app) as c:
        yield c


def _upload(client, name="guide.md", content=None, category="upload"):
    content = content or "# 行业指南\n\n市场规模测算方法：自上而下估算 TAM。"
    return client.post(
        "/api/v1/knowledge/documents",
        files={"file": (name, content.encode("utf-8"), "text/markdown")},
        data={"category": category},
    )


# ---------------------------------------------------------------- 知识库文档管理


def test_knowledge_documents_list_empty(client):
    resp = client.get("/api/v1/knowledge/documents")
    assert resp.status_code == 200
    assert resp.json()["documents"] == []


def test_upload_then_list_document(client):
    resp = _upload(client, name="指南.md")
    assert resp.status_code == 200
    body = resp.json()
    assert body["source"] == "指南.md"
    assert body["added"] >= 1

    docs = client.get("/api/v1/knowledge/documents").json()["documents"]
    entry = [d for d in docs if d["source"] == "指南.md"]
    assert entry and entry[0]["category"] == "upload"
    assert entry[0]["chunk_count"] >= 1


def test_upload_empty_file_rejected(client):
    resp = client.post(
        "/api/v1/knowledge/documents",
        files={"file": ("empty.md", b"", "text/markdown")},
        data={"category": "upload"},
    )
    assert resp.status_code == 400


def test_knowledge_search_hits(client):
    content = "便携式储能电源 2025 出货量预测：市场规模 500 亿元"
    _upload(client, name="research.md", content=content)
    resp = client.post("/api/v1/knowledge/search", json={"query": content, "k": 3})
    assert resp.status_code == 200
    hits = resp.json()["hits"]
    assert len(hits) >= 1
    assert hits[0]["category"] == "upload"
    assert hits[0]["score"] > 0.99          # 与库中文本完全一致的查询命中


def test_knowledge_search_validation(client):
    assert client.post("/api/v1/knowledge/search", json={"query": ""}).status_code == 422


# ---------------------------------------------------------------- 长期记忆检索


def test_memory_search_endpoint(client):
    long_term_memory.remember(
        "便携储能市场调研",
        "# 一、市场规模\n\n2025 出货量数据\n\n# 二、竞争格局\n\n厂商分析",
        task_id="task_mem1",
    )
    resp = client.get("/api/v1/memory/search", params={"q": "便携储能", "k": 3})
    assert resp.status_code == 200
    hits = resp.json()["hits"]
    assert len(hits) == 1
    assert hits[0]["task_id"] == "task_mem1"
    assert "# 一、市场规模" in hits[0]["structure"]


# ---------------------------------------------------------------- 任务断点恢复


def test_resume_task_running(client):
    task_repository.create_task("task_resume1", "储能市场")
    with patch(
        "api.routes.workflow.run_task",
        new=AsyncMock(return_value={"task_id": "task_resume1", "status": "completed"}),
    ):
        resp = client.post("/api/v1/tasks/task_resume1/resume")
    assert resp.status_code == 200
    body = resp.json()
    assert body["resumed"] is True
    assert body["status"] == "running"


def test_resume_task_completed_rejected(client):
    task_repository.create_task("task_done1", "主题")
    task_repository.update_task("task_done1", status="completed")
    resp = client.post("/api/v1/tasks/task_done1/resume")
    assert resp.status_code == 400
    assert "无需恢复" in resp.json()["detail"]


def test_resume_task_not_found(client):
    resp = client.post("/api/v1/tasks/no_such/resume")
    assert resp.status_code == 404


def test_mark_interrupted_tasks_as_paused(client):
    """启动扫描：遗留 running → paused（附带中断说明），completed 不受影响。"""
    task_repository.create_task("task_legacy1", "遗留任务A")
    task_repository.create_task("task_legacy2", "遗留任务B")
    task_repository.create_task("task_done2", "已完成任务")
    task_repository.update_task("task_done2", status="completed")
    # 模拟：进程被重启前两个任务还停在 running
    assert task_repository.get_task("task_legacy1")["status"] == "running"
    changed = task_repository.mark_interrupted_tasks_as_paused()
    assert changed == 2
    paused1 = task_repository.get_task("task_legacy1")
    assert paused1["status"] == "paused"
    assert "恢复执行" in paused1["error"]
    assert task_repository.get_task("task_done2")["status"] == "completed"
    # 幂等：再次调用不再改动
    assert task_repository.mark_interrupted_tasks_as_paused() == 0


def test_resume_task_paused(client):
    """paused（意外中断）任务是恢复执行的主入口：断点续跑并重置为 running。"""
    task_repository.create_task("task_paused1", "中断的储能任务")
    task_repository.mark_interrupted_tasks_as_paused()
    assert task_repository.get_task("task_paused1")["status"] == "paused"
    with patch(
        "api.routes.workflow.run_task",
        new=AsyncMock(return_value={"task_id": "task_paused1", "status": "completed"}),
    ):
        resp = client.post("/api/v1/tasks/task_paused1/resume")
    assert resp.status_code == 200
    assert resp.json()["resumed"] is True
    assert resp.json()["status"] == "running"
    # error 已清除，状态回到 running
    refreshed = task_repository.get_task("task_paused1")
    assert refreshed["status"] == "running"
    assert refreshed["error"] is None
