"""大模型封装测试（llm/client.py）。"""
import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from llm import client as llm_client


@pytest.fixture(autouse=True)
def reset_client():
    """每个测试前重置懒加载客户端。"""
    llm_client._client = None
    yield
    llm_client._client = None


def test_get_client_missing_key_raises(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        llm_client.get_client()


def test_get_client_creates_and_caches(monkeypatch):
    monkeypatch.setenv("OPENAI_BASE_URL", "http://test/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    c1 = llm_client.get_client()
    c2 = llm_client.get_client()
    assert c1 is c2
    assert str(c1.base_url) == "http://test/v1/"


def test_get_client_default_base_url(monkeypatch):
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    client = llm_client.get_client()
    assert str(client.base_url) == "https://api.openai.com/v1/"


def test_get_model_default(monkeypatch):
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    assert llm_client.get_model() == "gpt-4o-mini"


def test_get_model_custom(monkeypatch):
    monkeypatch.setenv("OPENAI_MODEL", "custom-model")
    assert llm_client.get_model() == "custom-model"


def test_chat_with_tools_and_choice(monkeypatch):
    monkeypatch.setenv("OPENAI_MODEL", "gpt-4o-mini")
    create = AsyncMock(return_value="ok")
    with patch.object(llm_client, "get_client") as get_client:
        get_client.return_value = AsyncMock(
            chat=AsyncMock(completions=AsyncMock(create=create))
        )
        tools = [{"type": "function"}]
        result = asyncio.run(
            llm_client.chat(
                [{"role": "user", "content": "hi"}], tools=tools, tool_choice="auto"
            )
        )
        assert result == "ok"
        kwargs = create.call_args.kwargs
        assert kwargs["model"] == "gpt-4o-mini"
        assert kwargs["tools"] == tools
        assert kwargs["tool_choice"] == "auto"


def test_chat_without_tools(monkeypatch):
    monkeypatch.setenv("OPENAI_MODEL", "gpt-4o-mini")
    create = AsyncMock(return_value="ok")
    with patch.object(llm_client, "get_client") as get_client:
        get_client.return_value = AsyncMock(
            chat=AsyncMock(completions=AsyncMock(create=create))
        )
        asyncio.run(llm_client.chat([{"role": "user", "content": "hi"}]))
        kwargs = create.call_args.kwargs
        assert "tools" not in kwargs
        assert "tool_choice" not in kwargs
