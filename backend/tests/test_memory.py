"""长期记忆 / 短期记忆（Checkpointer）测试（离线）。

- long_term_memory：remember / recall / search_memory（假嵌入，隔离真实网络）
- checkpointer：三级降级初始化、has_checkpoint / list_snapshots（写读真实图快照）

conftest autouse fixture 已将 Chroma 持久化目录与 Checkpointer 库文件隔离到 tmp_path。
"""
import asyncio

import pytest

from memory import checkpointer as cp_mod
from memory import long_term_memory
from rag import store as rag_store


class FakeEmbeddings:
    """确定性假嵌入（8 维，md5 派生），保证离线可检索。"""

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


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- 长期记忆


def test_remember_then_recall_roundtrip():
    ok = long_term_memory.remember(
        "便携储能市场",
        "# 一、市场规模\n\n2025 年全球出货 X 万台\n\n# 二、竞争格局\n\n头部厂商盘点",
        task_id="task_m1",
    )
    assert ok is True
    hits = long_term_memory.recall("便携储能", k=3)
    assert len(hits) == 1
    hit = hits[0]
    assert hit["topic"] == "便携储能市场"
    assert hit["task_id"] == "task_m1"
    assert "# 一、市场规模" in hit["structure"]
    assert hit["created_at"]
    assert 0 <= hit["score"] <= 1


def test_recall_empty_collection_returns_empty():
    assert long_term_memory.recall("全新主题") == []


def test_remember_empty_report_skipped():
    assert long_term_memory.remember("主题", "   ") is False


def test_remember_duplicate_skipped():
    long_term_memory.remember("主题A", "报告正文内容" * 10, task_id="t1")
    assert long_term_memory.remember("主题A", "报告正文内容" * 10, task_id="t1") is False


def test_remember_embedding_failure_tolerated(monkeypatch):
    """嵌入不可用（无 Key）→ 记忆写入失败仅返回 False，不抛异常。"""
    monkeypatch.setattr(rag_store, "_embeddings", None)
    assert long_term_memory.remember("主题", "正文") is False


def test_search_memory_proxy():
    long_term_memory.remember("家用储能", "# 报告\n内容", task_id="t2")
    assert len(long_term_memory.search_memory("家用储能", k=2)) == 1
    # 检索结果含 topic / task_id 等记忆字段
    hit = long_term_memory.search_memory("家用储能", k=2)[0]
    assert hit["topic"] == "家用储能"
    assert hit["task_id"] == "t2"


def test_extract_structure_headings_only():
    text = "# 一、标题\n普通段落\n## 二、小节\n内容"
    structure = long_term_memory._extract_structure(text)
    assert structure == ["# 一、标题", "## 二、小节"]


# ---------------------------------------------------------------- 短期记忆（Checkpointer）


def test_open_checkpointer_sqlite_or_fallback(tmp_path, monkeypatch):
    """async with open_checkpointer() 能打开 sqlite（或降级 memory）连接。"""
    monkeypatch.setattr(cp_mod, "CHECKPOINT_DB", tmp_path / "cp.db")
    cp_mod.reset_checkpointer()

    async def _probe():
        async with cp_mod.open_checkpointer() as (cp, kind):
            assert cp is not None
            assert kind in ("sqlite", "memory")     # 依赖可用 → sqlite，否则降级
            return kind

    assert run(_probe()) in ("sqlite", "memory")


def test_has_and_list_snapshots_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(cp_mod, "CHECKPOINT_DB", tmp_path / "cp.db")
    cp_mod.reset_checkpointer()
    assert cp_mod.has_checkpoint("no_such_task") is False
    assert cp_mod.list_snapshots("no_such_task") == []


def test_checkpoint_write_then_resume_detect(tmp_path, monkeypatch):
    """真实 LangGraph 图 + Checkpointer：执行后能检测到快照并可列出。"""
    monkeypatch.setattr(cp_mod, "CHECKPOINT_DB", tmp_path / "cp.db")
    cp_mod.reset_checkpointer()

    from typing import TypedDict

    from langgraph.graph import END, START, StateGraph

    class TinyState(TypedDict):
        x: int

    g = StateGraph(TinyState)
    g.add_node("inc", lambda s: {"x": s["x"] + 1})
    g.add_edge(START, "inc")
    g.add_edge("inc", END)

    async def _run():
        async with cp_mod.open_checkpointer() as (cp, _kind):
            app = g.compile(checkpointer=cp)
            return await app.ainvoke({"x": 1}, {"configurable": {"thread_id": "tiny_1"}})

    assert run(_run())["x"] == 2
    assert cp_mod.has_checkpoint("tiny_1") is True
    snaps = cp_mod.list_snapshots("tiny_1")
    assert len(snaps) >= 1
    assert any("checkpoint_id" in s for s in snaps)


def test_reset_checkpointer_clears_memory_singleton(tmp_path, monkeypatch):
    """reset_checkpointer 清空进程内 MemorySaver 单例，下次重建新实例。"""
    monkeypatch.setattr(cp_mod, "CHECKPOINT_DB", tmp_path / "cp.db")
    cp_mod.reset_checkpointer()
    m1 = cp_mod._get_memory_saver()
    cp_mod.reset_checkpointer()
    m2 = cp_mod._get_memory_saver()
    assert m1 is not m2          # 重置后重建新实例
