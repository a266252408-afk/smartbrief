"""pytest 共享配置：确保 backend 与 smartbrief 根目录可被测试导入。

同时提供RAG / 记忆模块的自动隔离：
- 清空嵌入 API Key，避免单测误触发真实网络嵌入；
- RAG 持久化目录 / 模板目录 / 术语文件重定向到 tmp_path；
- Checkpointer 库文件重定向到 tmp_path，避免测试写入真实 checkpoints.db。
"""
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
PROJECT_DIR = Path(__file__).resolve().parents[2]  # smartbrief/（含 mcp_servers/）
for _dir in (BACKEND_DIR, PROJECT_DIR):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))


@pytest.fixture(autouse=True)
def _rag_memory_test_isolation(tmp_path, monkeypatch):
    """每个测试默认隔离 RAG/记忆外部依赖（离线、不污染真实数据目录）。"""
    import rag.store as rag_store
    import memory.checkpointer as cp_mod

    # 1) 关闭应用启动时的 RAG seed；清空嵌入 Key → 嵌入调用快速失败而非联网
    monkeypatch.setenv("SMARTBRIEF_RAG_SEED", "0")
    monkeypatch.setenv("EMBEDDING_API_KEY", "")
    # 2) RAG 数据目录与持久化目录 → tmp_path
    monkeypatch.setattr(rag_store, "CHROMA_PERSIST_DIR", tmp_path / "chroma")
    monkeypatch.setattr(rag_store, "UPLOADS_DIR", tmp_path / "uploads")
    monkeypatch.setattr(rag_store, "TEMPLATES_DIR", tmp_path / "templates")
    monkeypatch.setattr(rag_store, "TERMINOLOGIES_FILE", tmp_path / "terminologies.txt")
    rag_store._stores.clear()          # 清掉跨测试缓存的 store（避免指向旧目录）
    rag_store._embeddings = None
    # 3) Checkpointer → tmp_path，并重置单例
    monkeypatch.setattr(cp_mod, "CHECKPOINT_DB", tmp_path / "checkpoints.db")
    cp_mod.reset_checkpointer()
    yield
