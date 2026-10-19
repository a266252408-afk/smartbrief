"""单智能体测试（agents/single_agent.py）。"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents import single_agent
from tools import builtin_tools

# 单独运行本测试时，确保内置工具注册表已初始化（_execute_tool 依赖此状态）
builtin_tools.init()


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- 统计


def test_record_and_get_stats():
    single_agent._STATS.clear()
    single_agent._record("t1", ok=True)
    single_agent._record("t1", ok=False)
    assert single_agent.get_stats()["t1"] == {"count": 2, "success": 1, "failed": 1}


# ---------------------------------------------------------------- schema 组装


def test_enabled_schemas_merges_builtin_and_mcp():
    with (
        patch.object(single_agent.builtin_tools, "list_enabled_schemas",
                     return_value=[{"b": 1}]),
        patch.object(single_agent.mcp_manager, "list_enabled_schemas",
                     new=AsyncMock(return_value=[{"m": 1}])),
        # 技能注入通道一并入最终 schema（此处置空，专注内置+MCP 合并）
        patch.object(single_agent.skills_manager.manager, "list_enabled_schemas",
                     return_value=[]),
    ):
        schemas = run(single_agent._enabled_schemas())
    assert schemas == [{"b": 1}, {"m": 1}]


# ---------------------------------------------------------------- 工具执行


def test_execute_builtin_tool():
    result = run(single_agent._execute_tool("calculator", {"expression": "2*3"}))
    assert result == "6"


def test_execute_builtin_tool_without_args():
    result = run(single_agent._execute_tool("current_time", {}))
    assert len(result) == 19


def test_execute_mcp_tool():
    with patch.object(
        single_agent.mcp_manager, "call_tool", new=AsyncMock(return_value={"ok": 1})
    ) as call:
        result = run(single_agent._execute_tool("web_search", {"query": "q"}))
    assert result == {"ok": 1}
    call.assert_awaited_once_with("web_search", {"query": "q"})


def test_execute_unknown_tool_raises():
    with patch.object(
        single_agent.mcp_manager, "call_tool",
        new=AsyncMock(side_effect=ValueError("未知 MCP 工具：x")),
    ):
        with pytest.raises(ValueError):
            run(single_agent._execute_tool("x", {}))


# ---------------------------------------------------------------- 对话循环


class FakeFunction:
    def __init__(self, name, arguments):
        self.name = name
        self.arguments = arguments


class FakeToolCall:
    def __init__(self, call_id, name, arguments):
        self.id = call_id
        self.function = FakeFunction(name, arguments)


class FakeMessage:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


class FakeResp:
    def __init__(self, message):
        self.choices = [MagicMock(message=message)]


def fake_chat(responses):
    return AsyncMock(side_effect=responses)


def test_run_no_tool_call():
    resp = FakeResp(FakeMessage(content="直接回答"))
    with patch.object(single_agent, "chat", new=fake_chat([resp])):
        result = run(single_agent.run("你好"))
        # 验证 system prompt 被注入且在最前
        called_messages = single_agent.chat.call_args[0][0]
        assert called_messages[0]["role"] == "system"
        assert "SmartBrief" in called_messages[0]["content"]
    assert result == {"reply": "直接回答", "tool_calls": []}


def test_run_injects_system_prompt_and_filters_existing_system():
    resp = FakeResp(FakeMessage(content="好的"))
    history = [
        {"role": "system", "content": "旧的 system prompt"},
        {"role": "user", "content": "上一轮"},
    ]
    with patch.object(single_agent, "chat", new=fake_chat([resp])):
        run(single_agent.run("你好", history))
        called_messages = single_agent.chat.call_args[0][0]
        # 历史中的旧 system 被过滤，只保留一条新的 system prompt
        system_msgs = [m for m in called_messages if m["role"] == "system"]
        assert len(system_msgs) == 1
        assert called_messages[0]["role"] == "system"
        assert "市场调研" in called_messages[0]["content"]
        # 历史中的 user 消息保留
        assert any(
            m.get("role") == "user" and m.get("content") == "上一轮"
            for m in called_messages
        )


def test_run_with_tool_call_loop():
    resp1 = FakeResp(
        FakeMessage(
            content=None,
            tool_calls=[
                FakeToolCall("call_1", "calculator", '{"expression": "2*3"}')
            ],
        )
    )
    resp2 = FakeResp(FakeMessage(content="结果是 6"))
    with patch.object(single_agent, "chat", new=fake_chat([resp1, resp2])):
        result = run(single_agent.run("计算 2*3", [{"role": "user", "content": "上一轮"}]))
    assert result["reply"] == "结果是 6"
    assert len(result["tool_calls"]) == 1
    assert result["tool_calls"][0]["name"] == "calculator"
    assert result["tool_calls"][0]["success"] is True
    assert result["tool_calls"][0]["result"] == "6"


def test_run_tool_call_failure():
    resp1 = FakeResp(
        FakeMessage(
            content=None,
            tool_calls=[FakeToolCall("call_1", "web_search", '{"query": "q"}')],
        )
    )
    resp2 = FakeResp(FakeMessage(content="搜索失败"))
    with (
        patch.object(single_agent, "chat", new=fake_chat([resp1, resp2])),
        patch.object(
            single_agent.mcp_manager, "call_tool",
            new=AsyncMock(side_effect=RuntimeError("网络错误")),
        ),
    ):
        result = run(single_agent.run("搜索 q"))
    assert result["tool_calls"][0]["success"] is False
    assert result["tool_calls"][0]["result"] == {"error": "网络错误"}


def test_run_max_loop():
    always_tool = FakeResp(
        FakeMessage(
            content=None,
            tool_calls=[FakeToolCall("call_x", "current_time", "{}")],
        )
    )
    final_resp = FakeResp(FakeMessage(content="最终回答"))
    with patch.object(
        single_agent,
        "chat",
        new=fake_chat([always_tool] * single_agent.MAX_LOOP + [final_resp]),
    ):
        result = run(single_agent.run("一直触发工具"))
    assert result["reply"] == "最终回答"
    assert len(result["tool_calls"]) == single_agent.MAX_LOOP


def test_run_dsml_tool_calls():
    """DSML fallback：模型把 tool_calls 以 DSML 文本泄漏到 content 中时仍能解析执行。"""
    dsml_content = (
        '让我调用计算器：< | DSML | tool_calls>\n'
        '< | DSML | invoke name="calculator">\n'
        '< | DSML | parameter name="expression" string="true">2*3</ | DSML | parameter>\n'
        '</ | DSML | invoke>\n'
        '</ | DSML | tool_calls>'
    )
    resp1 = FakeResp(FakeMessage(content=dsml_content, tool_calls=None))
    resp2 = FakeResp(FakeMessage(content="计算结果是 6"))
    with patch.object(single_agent, "chat", new=fake_chat([resp1, resp2])):
        result = run(single_agent.run("帮我算 2*3"))
    assert result["reply"] == "计算结果是 6"
    assert len(result["tool_calls"]) == 1
    assert result["tool_calls"][0]["name"] == "calculator"
    assert result["tool_calls"][0]["success"] is True
    assert result["tool_calls"][0]["result"] == "6"


def test_run_mixed_standard_and_dsml_tool_calls():
    """DSML fallback：标准 tool_calls 与 DSML 文本同时存在时，两者都执行。"""
    dsml_content = (
        '我再算一下：< | DSML | tool_calls>\n'
        '< | DSML | invoke name="calculator">\n'
        '< | DSML | parameter name="expression" string="true">2+3</ | DSML | parameter>\n'
        '</ | DSML | invoke>\n'
        '</ | DSML | tool_calls>'
    )
    resp1 = FakeResp(
        FakeMessage(
            content=dsml_content,
            tool_calls=[FakeToolCall("call_1", "web_search", '{"query": "q"}')],
        )
    )
    resp2 = FakeResp(FakeMessage(content="完成"))
    with (
        patch.object(single_agent, "chat", new=fake_chat([resp1, resp2])),
        patch.object(
            single_agent.mcp_manager, "call_tool",
            new=AsyncMock(return_value={"ok": 1}),
        ),
    ):
        result = run(single_agent.run("搜索并计算"))
    assert result["reply"] == "完成"
    assert len(result["tool_calls"]) == 2
    assert {tc["name"] for tc in result["tool_calls"]} == {"web_search", "calculator"}


def test_run_dsml_no_keyword_variant():
    """DSML fallback：无 'DSML' 关键字的变体（<｜｜tool_calls>）也能解析。"""
    dsml_content = (
        '让我继续：\n'
        '<｜｜tool_calls>\n'
        '<｜｜invoke name="calculator">\n'
        '<｜｜parameter name="expression" string="true">2*3</｜｜parameter>\n'
        '</｜｜invoke>\n'
        '</｜｜tool_calls>'
    )
    resp1 = FakeResp(FakeMessage(content=dsml_content, tool_calls=None))
    resp2 = FakeResp(FakeMessage(content="结果是 6"))
    with patch.object(single_agent, "chat", new=fake_chat([resp1, resp2])):
        result = run(single_agent.run("算一下"))
    assert result["reply"] == "结果是 6"
    assert len(result["tool_calls"]) == 1
    assert result["tool_calls"][0]["name"] == "calculator"
    assert result["tool_calls"][0]["success"] is True
    assert result["tool_calls"][0]["result"] == "6"


# ---------------------------------------------------------------- 技能注入与路由


def test_enabled_schemas_includes_skills_excluding_duplicates():
    """技能 schema 注入：与已有工具（内置/MCP）重名的技能被排除，避免重复工具名。"""
    skill_schemas = [
        {"function": {"name": "skill_a", "description": "技能A"}, "type": "function"},
        {"function": {"name": "web_search", "description": "与MCP重名"}, "type": "function"},
    ]

    def fake_list_enabled_schemas(excluded_names=None):
        # 模拟 skills_manager 的真实排除逻辑（single_agent 只负责传 excluded_names）
        excluded = excluded_names or set()
        return [s for s in skill_schemas if s["function"]["name"] not in excluded]

    with (
        patch.object(single_agent.builtin_tools, "list_enabled_schemas", return_value=[]),
        patch.object(
            single_agent.mcp_manager, "list_enabled_schemas",
            new=AsyncMock(return_value=[{"function": {"name": "web_search", "description": "mcp"}}]),
        ),
        patch.object(
            single_agent.skills_manager.manager, "list_enabled_schemas",
            new=MagicMock(side_effect=fake_list_enabled_schemas),
        ) as skill_call,
    ):
        schemas = run(single_agent._enabled_schemas())
    names = [s["function"]["name"] for s in schemas]
    assert "skill_a" in names
    assert names.count("web_search") == 1            # 技能里的 web_search 被排除
    skill_call.assert_called_once_with(excluded_names={"web_search"})


def test_execute_skill_tool_routing():
    """技能路由：内置与 MCP 均不命中时走技能执行器（通道一）。"""

    class FakeOutput:
        def model_dump(self):
            return {"status": "success", "data": {"reply": "技能结果"}}

    with (
        patch.object(single_agent.builtin_tools, "get_tool", return_value=None),
        patch.object(single_agent.skills_manager.manager, "get_skill", return_value=object()),
        patch.object(
            single_agent.skills_manager.manager, "run_skill",
            new=AsyncMock(return_value=FakeOutput()),
        ) as run_skill,
    ):
        result = run(single_agent._execute_tool("my_skill", {"query": "hello"}))
    assert result == {"status": "success", "data": {"reply": "技能结果"}}
    run_skill.assert_awaited_once_with("my_skill", "hello")


def test_skill_banner_and_system_prompt():
    """技能名片（通道二）：只列已启用技能，动态拼进 system prompt。"""
    skills = [
        {"name": "s1", "display_name": "技能一", "description": "做分析", "enabled": True},
        {"name": "s2", "display_name": "技能二", "description": "做搜索", "enabled": False},
    ]
    with patch.object(single_agent.skills_manager.manager, "list_skills", return_value=skills):
        banner = single_agent._skill_banner()
        prompt = single_agent._build_system_prompt()
    assert "s1（技能一）：做分析" in banner
    assert "s2" not in banner                        # 已禁用技能不进名片
    assert banner in prompt
    assert "SmartBrief" in prompt                    # 基础身份仍在


def test_skill_banner_empty_when_no_enabled():
    with patch.object(single_agent.skills_manager.manager, "list_skills", return_value=[]):
        assert single_agent._skill_banner() == ""
        assert single_agent._build_system_prompt() == single_agent.SYSTEM_PROMPT
