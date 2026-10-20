"""pytest 共享配置：确保 backend 与 smartbrief 根目录可被测试导入。

同时提供RAG / 记忆模块的自动隔离：
- 清空嵌入 API Key，避免单测误触发真实网络嵌入；
- RAG 持久化目录 / 模板目录 / 术语文件重定向到 tmp_path；
- Checkpointer 库文件重定向到 tmp_path，避免测试写入真实 checkpoints.db。
"""
import sys
import importlib
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
PROJECT_DIR = Path(__file__).resolve().parents[2]  # smartbrief/（含 mcp_servers/）
for _dir in (BACKEND_DIR, PROJECT_DIR):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))


# 在测试模块导入前关闭 dotenv、遥测和外部网络。
importlib.import_module("tests.offline_guard").install()


@pytest.fixture(autouse=True)
def _rag_memory_test_isolation(tmp_path, monkeypatch):
    """每个测试默认隔离 RAG/记忆外部依赖（离线、不污染真实数据目录）。"""
    import rag.store as rag_store
    import memory.checkpointer as cp_mod
    from db import database
    from skills import skills_manager

    from tests.auth_support import ADMIN_TOKEN, READ_TOKEN
    monkeypatch.setenv("SMARTBRIEF_ADMIN_TOKEN", ADMIN_TOKEN)
    monkeypatch.setenv("SMARTBRIEF_READONLY_TOKEN", READ_TOKEN)

    monkeypatch.delenv("SMARTBRIEF_ALLOW_SKILL_PYTHON", raising=False)
    monkeypatch.delenv("SMARTBRIEF_ALLOW_SKILL_SCRIPTS", raising=False)

    # 所有测试默认隔离业务库、配置和技能目录；局部 fixture 可进一步覆盖。
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "data.db")
    monkeypatch.setattr("mcp_client.mcp_client.CONFIG_PATH", tmp_path / "mcp_config.json")
    monkeypatch.setattr("tools.builtin_tools.CONFIG_PATH", tmp_path / "tool_config.json")
    monkeypatch.setattr(skills_manager, "SKILL_CONFIG_PATH", tmp_path / "skill_config.json")
    monkeypatch.setattr(skills_manager, "SKILLS_POOL_DIR", tmp_path / "skills_pool")
    monkeypatch.setattr("mcp_client.mcp_client.BUILTIN_SERVER_SCRIPT",
                        BACKEND_DIR / "tests" / "fixtures" / "mcp_server.py")

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


@pytest.fixture
def fake_embeddings():
    from tests.fakes import FakeEmbeddings
    return FakeEmbeddings()


@pytest.fixture
def scripted_llm():
    from tests.fakes import ScriptedLLM
    return ScriptedLLM


@pytest.fixture
def offline_services(monkeypatch, fake_embeddings):
    """只替换外部模型/嵌入；真实图、SQLite 和 MCP 协议均保留。"""
    from tests.fakes import ScriptedLLM
    from agents import planner, react_researcher, react_writer, reviewer, rewriter
    from rag import store
    from memory import long_term_memory
    scripts = {}
    for module in (planner, react_researcher, react_writer, reviewer, rewriter):
        script = ScriptedLLM([])
        monkeypatch.setattr(module, "chat", script.chat)
        scripts[module.__name__.rsplit(".", 1)[-1]] = script
    monkeypatch.setattr(store, "_embeddings", fake_embeddings)
    monkeypatch.setattr(long_term_memory, "_MEMORY_ENABLED", False)
    return scripts


@pytest.fixture
def offline_evidence(offline_services, monkeypatch):
    """为需要质量通过的旧场景提供明确可追溯的外部搜索夹具。"""
    from unittest.mock import AsyncMock
    from agents import react_researcher
    from tests.fakes import fixture_search
    monkeypatch.setattr(react_researcher.mcp_manager, "call_tool", AsyncMock(return_value=fixture_search("资料")))
    offline_services["react_researcher"].outputs = ['{"action":"web_search","query":"资料"}', '{"action":"done"}']
    return offline_services
