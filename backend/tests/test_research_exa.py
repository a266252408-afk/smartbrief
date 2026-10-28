"""research 智能体搜索链路：EXA 优先，未配置/禁用/失败时回退 MCP Bing。"""
import asyncio
from unittest.mock import AsyncMock

from agents import react_researcher
from skills.base_skill import SkillOutput

EXA_RESULT = {
    "source": "exa",
    "query": "测试查询",
    "results": [
        {"title": "EXA 来源", "snippet": "摘要", "url": "https://example.invalid/exa"}
    ],
    "search_endpoint": "https://api.exa.ai/search",
}


class Skill:
    def __init__(self, enabled=True):
        self.enabled = enabled


def patch_skill(monkeypatch, skill, output=None):
    monkeypatch.setattr("skills.skills_manager.manager.get_skill", lambda name: skill)
    monkeypatch.setattr(
        "skills.skills_manager.manager.run_skill",
        AsyncMock(return_value=output) if output is not None else AsyncMock(),
    )


def patch_mcp(monkeypatch, result=None):
    fake = AsyncMock(return_value=result or {"source": "bing", "query": "测试查询", "results": []})
    monkeypatch.setattr(react_researcher.mcp_manager, "call_tool", fake)
    return fake


def test_research_prefers_enabled_exa_skill(monkeypatch):
    monkeypatch.setenv("EXA_API_KEY", "test-key")
    patch_skill(monkeypatch, Skill(), SkillOutput(status="success", data=EXA_RESULT))
    mcp = patch_mcp(monkeypatch)

    result = asyncio.run(react_researcher._default_search("测试查询"))

    assert result == EXA_RESULT
    mcp.assert_not_awaited()


def test_research_observation_records_exa_source(monkeypatch):
    monkeypatch.setenv("EXA_API_KEY", "test-key")
    patch_skill(monkeypatch, Skill(), SkillOutput(status="success", data=EXA_RESULT))
    patch_mcp(monkeypatch)
    logs = []

    result = asyncio.run(react_researcher._run_search("测试查询", 1, logs))

    assert result == EXA_RESULT
    observations = [log["content"] for log in logs if log["type"] == "Observation"]
    assert observations and '"source": "exa"' in observations[-1]


def test_research_falls_back_without_api_key(monkeypatch):
    monkeypatch.setenv("EXA_API_KEY", "")
    patch_skill(monkeypatch, Skill(), SkillOutput(status="success", data=EXA_RESULT))
    mcp = patch_mcp(monkeypatch)

    result = asyncio.run(react_researcher._default_search("测试查询"))

    assert result["source"] == "bing"
    mcp.assert_awaited_once_with("web_search", {"query": "测试查询"})


def test_research_falls_back_when_exa_skill_disabled(monkeypatch):
    monkeypatch.setenv("EXA_API_KEY", "test-key")
    patch_skill(monkeypatch, Skill(enabled=False), SkillOutput(status="success", data=EXA_RESULT))
    mcp = patch_mcp(monkeypatch)

    result = asyncio.run(react_researcher._default_search("测试查询"))

    assert result["source"] == "bing"
    mcp.assert_awaited_once_with("web_search", {"query": "测试查询"})


def test_research_falls_back_when_exa_fails(monkeypatch):
    monkeypatch.setenv("EXA_API_KEY", "test-key")
    patch_skill(
        monkeypatch,
        Skill(),
        SkillOutput(status="failed", data=None, metadata={"error": "EXA 接口返回 HTTP 401"}),
    )
    mcp = patch_mcp(monkeypatch)

    result = asyncio.run(react_researcher._default_search("测试查询"))

    assert result["source"] == "bing"
    mcp.assert_awaited_once_with("web_search", {"query": "测试查询"})
