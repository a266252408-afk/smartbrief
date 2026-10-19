"""MCP 客户端测试（基于官方 MCP SDK 的 mcp_client/mcp_client.py）。"""
import asyncio
import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import pytest
from mcp import types


def _text(text: str) -> types.TextContent:
    """构造带 type 的 TextContent。"""
    return types.TextContent(type="text", text=text)


def _tool(name: str, description: str = "", input_schema: dict | None = None) -> types.Tool:
    """构造带 inputSchema 的 Tool。"""
    return types.Tool(name=name, description=description, inputSchema=input_schema or {})

from mcp_client.mcp_client import (
    BUILTIN_SERVER_ID,
    BaseConnection,
    HttpConnection,
    MCPManager,
    SseConnection,
    StdioConnection,
    _create_connection,
    _parse_call_result,
    _to_openai_schema,
    _tool_to_dict,
)


@pytest.fixture
def tmp_config(tmp_path, monkeypatch):
    """配置持久化路径重定向到临时目录。"""
    cfg = tmp_path / "mcp_config.json"
    monkeypatch.setattr("mcp_client.mcp_client.CONFIG_PATH", cfg)
    return cfg


def run_manager(mgr, scenario):
    """在同一个事件循环内完成 start -> scenario -> close。"""

    async def _main():
        await mgr.start()
        try:
            return await scenario(mgr)
        finally:
            await mgr.close()

    return asyncio.run(_main())


# ---------------------------------------------------------------- Fake 会话


class FakeSession:
    """模拟官方 SDK 的 ClientSession。"""

    def __init__(self, tools=None, call_result=None):
        self._tools = tools or []
        self._call_result = call_result
        self.initialized = False

    async def initialize(self):
        self.initialized = True

    async def list_tools(self):
        return types.ListToolsResult(tools=self._tools)

    async def call_tool(self, name, arguments=None):
        if self._call_result is not None:
            return self._call_result
        return types.CallToolResult(
            content=[_text(json.dumps({"name": name, "args": arguments}, ensure_ascii=False))]
        )


def _fake_transport(size=2, raise_on_enter=None):
    """返回模拟的 stdio/streamable_http 异步上下文管理器工厂。

    :param size: 传输返回的元组元素个数（stdio=2；streamable_http=3，含 get_session_id 函数）
    :param raise_on_enter: 进入时抛出指定异常（模拟连接失败）
    """

    @asynccontextmanager
    async def _cm(*args, **kwargs):
        if raise_on_enter is not None:
            raise raise_on_enter
        if size == 3:
            yield (AsyncMock(), AsyncMock(), lambda: None)
        else:
            yield (AsyncMock(), AsyncMock())

    return _cm


def _fake_client_session(session):
    """返回模拟的 ClientSession 异步上下文管理器工厂。"""

    @asynccontextmanager
    async def _cm(read_stream, write_stream):
        yield session

    return _cm


# ---------------------------------------------------------------- 配置读写


def test_load_config_missing(tmp_config):
    from mcp_client.mcp_client import _load_config

    assert _load_config() == {"servers": []}


def test_load_config_corrupted(tmp_config):
    from mcp_client.mcp_client import _load_config

    tmp_config.write_text("{bad", encoding="utf-8")
    assert _load_config() == {"servers": []}


def test_load_config_valid(tmp_config):
    from mcp_client.mcp_client import _load_config

    tmp_config.write_text(json.dumps({"servers": [{"id": "a"}]}), encoding="utf-8")
    assert _load_config() == {"servers": [{"id": "a"}]}


def test_save_config(tmp_config):
    from mcp_client.mcp_client import _save_config

    _save_config([{"id": "a", "name": "A"}])
    data = json.loads(tmp_config.read_text(encoding="utf-8"))
    assert data["servers"] == [{"id": "a", "name": "A"}]


# ---------------------------------------------------------------- 连接基类


def test_base_connection_defaults():
    conn = BaseConnection(
        {"id": "x", "name": "y", "kind": "http", "url": "u", "description": "d",
         "enabled": False, "builtin": True}
    )
    assert conn.to_dict()["name"] == "y"
    assert conn.to_dict()["enabled"] is False


def test_base_connection_not_implemented():
    conn = BaseConnection({"id": "x"})
    with pytest.raises(NotImplementedError):
        asyncio.run(conn.start())
    with pytest.raises(NotImplementedError):
        asyncio.run(conn.close())
    with pytest.raises(NotImplementedError):
        asyncio.run(conn.list_tools())
    with pytest.raises(NotImplementedError):
        asyncio.run(conn.call_tool("t"))


# ---------------------------------------------------------------- stdio 连接


def test_stdio_start_list_and_call():
    """模拟 stdio_client + ClientSession 完成启动/列工具/调用。"""
    session = FakeSession(
        tools=[_tool("web_search", "d", {"type": "object"})],
        call_result=types.CallToolResult(content=[_text('{"query":"q"}')]),
    )
    conn = StdioConnection({"id": "s", "name": "s", "kind": "stdio"})

    async def scenario():
        with patch("mcp_client.mcp_client.stdio_client", new=_fake_transport()):
            with patch("mcp_client.mcp_client.ClientSession", new=_fake_client_session(session)):
                await conn.start()
                assert conn.is_running
                assert session.initialized
                tools = await conn.list_tools()
                assert tools[0]["name"] == "web_search"
                result = await conn.call_tool("web_search", {"query": "q"})
                assert result == {"query": "q"}

    asyncio.run(scenario())


def test_stdio_start_failure_cleans_up():
    """start 失败时清理 exit_stack 与 session。"""
    conn = StdioConnection({"id": "s", "name": "s", "kind": "stdio"})

    async def scenario():
        with patch("mcp_client.mcp_client.stdio_client", new=_fake_transport(raise_on_enter=ConnectionError("boom"))):
            with pytest.raises(ConnectionError, match="boom"):
                await conn.start()
        assert conn._exit_stack is None
        assert conn._session is None
        assert not conn.is_running

    asyncio.run(scenario())


def test_stdio_start_failure_with_bad_aclose_cleans_up():
    """stdio start 中途失败且清理阶段 aclose 也抛 RuntimeError：状态仍清空、异常原样抛出。"""
    failing = FakeSession()
    failing.initialize = AsyncMock(side_effect=ConnectionError("init fail"))

    @asynccontextmanager
    async def _bad_exit_transport(*args, **kwargs):
        try:
            yield (AsyncMock(), AsyncMock())
        finally:
            raise RuntimeError("cancel scope exit bug")

    async def scenario():
        with (
            patch("mcp_client.mcp_client.stdio_client", new=_bad_exit_transport),
            patch("mcp_client.mcp_client.ClientSession", new=_fake_client_session(failing)),
        ):
            conn = StdioConnection({"id": "s", "name": "s", "kind": "stdio"})
            with pytest.raises(ConnectionError, match="init fail"):
                await conn.start()
            assert conn._exit_stack is None
            assert conn._session is None

    asyncio.run(scenario())


def test_stdio_list_tools_before_start():
    conn = StdioConnection({"id": "s", "name": "s", "kind": "stdio"})
    with pytest.raises(ConnectionError, match="未启动"):
        asyncio.run(conn.list_tools())


def test_stdio_call_tool_before_start():
    conn = StdioConnection({"id": "s", "name": "s", "kind": "stdio"})
    with pytest.raises(ConnectionError, match="未启动"):
        asyncio.run(conn.call_tool("web_search"))


def test_stdio_connection_roundtrip():
    """真实拉起内置 FastMCP web_search_server 子进程，验证完整闭环。"""

    async def scenario():
        conn = StdioConnection(
            {"id": BUILTIN_SERVER_ID, "name": "web_search_server", "kind": "stdio"}
        )
        await conn.start()
        try:
            assert conn.is_running
            tools = await conn.list_tools()
            assert tools[0]["name"] == "web_search"
            result = await conn.call_tool(
                "web_search", {"query": "test", "num_results": 2}
            )
            assert result["query"] == "test"
            assert len(result["results"]) == 2
        finally:
            await conn.close()
        assert not conn.is_running

    asyncio.run(scenario())


# ---------------------------------------------------------------- http 连接


def make_http_conn(url="http://test/mcp"):
    return HttpConnection(
        {"id": "h", "name": "h", "kind": "http", "url": url,
         "description": "", "enabled": True, "builtin": False}
    )


def test_http_start_list_and_call():
    session = FakeSession(
        tools=[_tool("t1", "d", {"type": "object"})],
        call_result=types.CallToolResult(content=[_text('{"ok": 1}')]),
    )
    conn = make_http_conn()

    async def scenario():
        with patch("mcp_client.mcp_client.streamable_http_client", new=_fake_transport(size=3)):
            with patch("mcp_client.mcp_client.ClientSession", new=_fake_client_session(session)):
                await conn.start()
                assert conn._session is not None
                assert session.initialized
                assert conn.last_error == ""
                assert await conn.list_tools() == [
                    {"name": "t1", "description": "d", "inputSchema": {"type": "object"}}
                ]
                assert await conn.call_tool("t1", {"a": 1}) == {"ok": 1}

    asyncio.run(scenario())


def test_http_missing_url():
    conn = make_http_conn(url="")
    with pytest.raises(ConnectionError, match="未配置"):
        asyncio.run(conn.start())


def test_http_start_failure_records_last_error():
    """连接失败：记录 last_error 并清理资源。"""
    conn = make_http_conn()

    async def scenario():
        with patch(
            "mcp_client.mcp_client.streamable_http_client",
            new=_fake_transport(size=3, raise_on_enter=ConnectionError("refused")),
        ):
            with pytest.raises(ConnectionError, match="refused"):
                await conn.start()
        assert conn.last_error != ""
        assert conn._session is None

    asyncio.run(scenario())


def test_http_close():
    conn = make_http_conn()

    async def scenario():
        stack = AsyncMock()
        conn._exit_stack = stack
        conn._session = FakeSession()
        await conn.close()
        stack.aclose.assert_awaited_once()
        assert conn._session is None

    asyncio.run(scenario())


def test_stdio_close_swallows_aclose_error():
    """close 时 SDK 异步生成器关闭抛 RuntimeError，也应清理状态且不向上抛。"""
    conn = StdioConnection({"id": "s", "name": "s", "kind": "stdio"})

    async def scenario():
        stack = AsyncMock()
        stack.aclose.side_effect = RuntimeError("Attempted to exit a cancel scope")
        conn._exit_stack = stack
        conn._session = FakeSession()
        await conn.close()  # 不应抛出
        assert conn._exit_stack is None
        assert conn._session is None
        assert not conn.is_running

    asyncio.run(scenario())


def test_http_close_swallows_aclose_error():
    """http 连接 close 时 aclose 抛异常同样被吞掉、状态清空。"""
    conn = make_http_conn()

    async def scenario():
        stack = AsyncMock()
        stack.aclose.side_effect = RuntimeError("cancel scope exit bug")
        conn._exit_stack = stack
        conn._session = FakeSession()
        await conn.close()
        assert conn._exit_stack is None
        assert conn._session is None

    asyncio.run(scenario())


def test_http_start_failure_with_bad_aclose_cleans_up():
    """start 中途失败且清理阶段 aclose 也抛 RuntimeError：状态仍清空、异常转 ConnectionError。"""
    failing = FakeSession()
    failing.initialize = AsyncMock(side_effect=ConnectionError("init fail"))

    @asynccontextmanager
    async def _bad_exit_transport(*args, **kwargs):
        try:
            yield (AsyncMock(), AsyncMock(), lambda: None)
        finally:
            raise RuntimeError("cancel scope exit bug")

    async def scenario():
        with (
            patch("mcp_client.mcp_client.streamable_http_client", new=_bad_exit_transport),
            patch("mcp_client.mcp_client.ClientSession", new=_fake_client_session(failing)),
        ):
            conn = make_http_conn()
            with pytest.raises(ConnectionError, match="init fail"):
                await conn.start()
            assert conn._exit_stack is None
            assert conn._session is None

    asyncio.run(scenario())


def test_http_start_idempotent():
    """已建立会话时再次 start 直接返回（不重复初始化）。"""
    conn = make_http_conn()
    session = FakeSession()
    conn._session = session
    conn._exit_stack = AsyncMock()

    async def scenario():
        await conn.start()  # 已存在 session → 直接 return
        assert session.initialized is False  # 未再次 initialize

    asyncio.run(scenario())


def test_http_list_and_call_before_start():
    conn = make_http_conn()
    with pytest.raises(ConnectionError, match="未连接"):
        asyncio.run(conn.list_tools())
    with pytest.raises(ConnectionError, match="未连接"):
        asyncio.run(conn.call_tool("t1"))


# ---------------------------------------------------------------- sse 连接


def make_sse_conn(url="http://test/sse"):
    return SseConnection(
        {"id": "sse1", "name": "sse1", "kind": "sse", "url": url,
         "description": "", "enabled": True, "builtin": False}
    )


def test_sse_start_list_and_call():
    """模拟 sse_client（二元组）+ ClientSession 完成启动/列工具/调用。"""
    session = FakeSession(
        tools=[_tool("t1", "d", {"type": "object"})],
        call_result=types.CallToolResult(content=[_text('{"ok": 1}')]),
    )
    conn = make_sse_conn()

    async def scenario():
        with patch("mcp_client.mcp_client.sse_client", new=_fake_transport()):  # size=2（sse 二元组）
            with patch("mcp_client.mcp_client.ClientSession", new=_fake_client_session(session)):
                await conn.start()
                assert conn._session is not None
                assert session.initialized
                assert conn.last_error == ""
                assert await conn.list_tools() == [
                    {"name": "t1", "description": "d", "inputSchema": {"type": "object"}}
                ]
                assert await conn.call_tool("t1", {"a": 1}) == {"ok": 1}

    asyncio.run(scenario())


def test_sse_missing_url():
    conn = make_sse_conn(url="")
    with pytest.raises(ConnectionError, match="未配置"):
        asyncio.run(conn.start())


def test_sse_start_failure_records_last_error():
    """连接失败：记录 last_error 并清理资源。"""
    conn = make_sse_conn()

    async def scenario():
        with patch(
            "mcp_client.mcp_client.sse_client",
            new=_fake_transport(raise_on_enter=ConnectionError("refused")),
        ):
            with pytest.raises(ConnectionError, match="refused"):
                await conn.start()
        assert conn.last_error != ""
        assert conn._session is None

    asyncio.run(scenario())


def test_sse_close():
    conn = make_sse_conn()

    async def scenario():
        stack = AsyncMock()
        conn._exit_stack = stack
        conn._session = FakeSession()
        await conn.close()
        stack.aclose.assert_awaited_once()
        assert conn._session is None

    asyncio.run(scenario())


def test_sse_close_swallows_aclose_error():
    """sse 连接 close 时 aclose 抛异常同样被吞掉、状态清空。"""
    conn = make_sse_conn()

    async def scenario():
        stack = AsyncMock()
        stack.aclose.side_effect = RuntimeError("cancel scope exit bug")
        conn._exit_stack = stack
        conn._session = FakeSession()
        await conn.close()
        assert conn._exit_stack is None
        assert conn._session is None

    asyncio.run(scenario())


def test_sse_start_failure_with_bad_aclose_cleans_up():
    """start 中途失败且清理阶段 aclose 也抛 RuntimeError：状态仍清空、异常转 ConnectionError。"""
    failing = FakeSession()
    failing.initialize = AsyncMock(side_effect=ConnectionError("init fail"))

    @asynccontextmanager
    async def _bad_exit_transport(*args, **kwargs):
        try:
            yield (AsyncMock(), AsyncMock())
        finally:
            raise RuntimeError("cancel scope exit bug")

    async def scenario():
        with (
            patch("mcp_client.mcp_client.sse_client", new=_bad_exit_transport),
            patch("mcp_client.mcp_client.ClientSession", new=_fake_client_session(failing)),
        ):
            conn = make_sse_conn()
            with pytest.raises(ConnectionError, match="init fail"):
                await conn.start()
            assert conn._exit_stack is None
            assert conn._session is None

    asyncio.run(scenario())


def test_sse_start_idempotent():
    """已建立会话时再次 start 直接返回（不重复初始化）。"""
    conn = make_sse_conn()
    session = FakeSession()
    conn._session = session
    conn._exit_stack = AsyncMock()

    async def scenario():
        await conn.start()
        assert session.initialized is False

    asyncio.run(scenario())


def test_sse_list_and_call_before_start():
    conn = make_sse_conn()
    with pytest.raises(ConnectionError, match="未连接"):
        asyncio.run(conn.list_tools())
    with pytest.raises(ConnectionError, match="未连接"):
        asyncio.run(conn.call_tool("t1"))


def test_create_connection_factory():
    """连接工厂按 kind 选择连接类型（默认 http，向后兼容旧配置）。"""
    assert isinstance(_create_connection({"id": "a", "kind": "stdio"}), StdioConnection)
    assert isinstance(_create_connection({"id": "b", "kind": "sse"}), SseConnection)
    assert isinstance(_create_connection({"id": "c", "kind": "http"}), HttpConnection)
    assert isinstance(_create_connection({"id": "d"}), HttpConnection)


# ---------------------------------------------------------------- 工具函数


def test_tool_to_dict():
    tool = _tool("t", "d", {"type": "object"})
    assert _tool_to_dict(tool) == {
        "name": "t",
        "description": "d",
        "inputSchema": {"type": "object"},
    }


def test_tool_to_dict_defaults():
    tool = _tool("t")
    assert _tool_to_dict(tool)["inputSchema"] == {
        "type": "object", "properties": {}, "required": []
    }


def test_parse_call_result_text_json():
    result = types.CallToolResult(content=[_text('{"a": 1}')])
    assert _parse_call_result(result) == {"a": 1}


def test_parse_call_result_text_not_json():
    result = types.CallToolResult(content=[_text("hello")])
    assert _parse_call_result(result) == {"text": "hello"}


def test_parse_call_result_no_text():
    result = types.CallToolResult(content=[])
    assert _parse_call_result(result) == {"content": []}


def test_to_openai_schema():
    schema = _to_openai_schema(
        {"name": "web_search", "description": "d",
         "inputSchema": {"type": "object", "properties": {}, "required": []}}
    )
    assert schema["function"]["name"] == "web_search"
    assert schema["type"] == "function"


def test_to_openai_schema_missing_name():
    assert _to_openai_schema({"description": "d"}) is None


# ---------------------------------------------------------------- 管理器


def _patch_http_connection(tools=None, call_result=None):
    """返回 async context manager，用于 patch Manager 中的 HttpConnection.start。"""
    session = FakeSession(tools=tools, call_result=call_result)

    @asynccontextmanager
    async def _cm():
        with patch("mcp_client.mcp_client.streamable_http_client", new=_fake_transport(size=3)):
            with patch("mcp_client.mcp_client.ClientSession", new=_fake_client_session(session)):
                yield

    return _cm()


def _patch_sse_connection(tools=None, call_result=None):
    """返回 async context manager，用于 patch Manager 中的 SseConnection.start。"""
    session = FakeSession(tools=tools, call_result=call_result)

    @asynccontextmanager
    async def _cm():
        with patch("mcp_client.mcp_client.sse_client", new=_fake_transport()):
            with patch("mcp_client.mcp_client.ClientSession", new=_fake_client_session(session)):
                yield

    return _cm()


def test_manager_start_inserts_builtin(tmp_config):
    async def scenario(mgr):
        servers = mgr.list_servers()
        assert servers[0]["id"] == BUILTIN_SERVER_ID
        assert servers[0]["builtin"] is True
        assert mgr.is_running
        assert len(servers[0]["tools"]) == 1
        data = json.loads(tmp_config.read_text(encoding="utf-8"))
        assert data["servers"][0]["id"] == BUILTIN_SERVER_ID

    run_manager(MCPManager(), scenario)


def test_manager_add_server_success(tmp_config):
    async def scenario(mgr):
        async with _patch_http_connection(tools=[_tool("t1", "d", {})]):
            server = await mgr.add_server("my_server", "http://localhost:9999/mcp", "desc", True)
        assert server["builtin"] is False
        assert server["enabled"] is True
        assert len(mgr.list_servers()) == 2
        assert mgr.list_servers()[1]["tools"][0]["name"] == "t1"

    run_manager(MCPManager(), scenario)


def test_manager_add_server_invalid(tmp_config):
    async def scenario(mgr):
        with pytest.raises(ValueError, match="不能为空"):
            await mgr.add_server("  ", "http://x")
        with pytest.raises(ValueError, match="不能为空"):
            await mgr.add_server("a", "  ")
        with pytest.raises(ValueError, match="已存在同名"):
            await mgr.add_server("web_search_server", "http://x")

    run_manager(MCPManager(), scenario)


def test_manager_update_server(tmp_config):
    async def scenario(mgr):
        async with _patch_http_connection(tools=[_tool("t1", "d", {})]):
            added = await mgr.add_server("s1", "http://a/mcp", "old", True)
        sid = added["id"]
        async with _patch_http_connection(tools=[_tool("t2", "d", {})]):
            updated = await mgr.update_server(sid, "s2", "http://b/mcp", "new", False)
        assert updated["name"] == "s2"
        assert updated["enabled"] is False
        with pytest.raises(ValueError, match="不存在"):
            await mgr.update_server("nope", "x", "http://x", "", True)
        with pytest.raises(ValueError, match="不可编辑"):
            await mgr.update_server(BUILTIN_SERVER_ID, "x", "http://x", "", True)
        with pytest.raises(ValueError, match="已存在同名"):
            await mgr.update_server(sid, "web_search_server", "http://b/mcp", "", True)

    run_manager(MCPManager(), scenario)


def test_manager_delete_server(tmp_config):
    async def scenario(mgr):
        async with _patch_http_connection():
            added = await mgr.add_server("d1", "http://a/mcp")
        await mgr.delete_server(added["id"])
        assert len(mgr.list_servers()) == 1
        with pytest.raises(ValueError, match="不存在"):
            await mgr.delete_server("nope")
        with pytest.raises(ValueError, match="不可删除"):
            await mgr.delete_server(BUILTIN_SERVER_ID)

    run_manager(MCPManager(), scenario)


def test_manager_set_enabled(tmp_config):
    async def scenario(mgr):
        s = await mgr.set_enabled(BUILTIN_SERVER_ID, False)
        assert s["enabled"] is False
        assert not mgr.is_running
        assert mgr.list_servers()[0]["tools"] == []
        await mgr.set_enabled(BUILTIN_SERVER_ID, True)
        assert mgr.is_running
        assert len(mgr.list_servers()[0]["tools"]) == 1
        with pytest.raises(ValueError, match="不存在"):
            await mgr.set_enabled("nope", True)

    run_manager(MCPManager(), scenario)


def test_manager_call_tool_and_schemas(tmp_config):
    async def scenario(mgr):
        schemas = await mgr.list_enabled_schemas()
        assert [s["function"]["name"] for s in schemas] == ["web_search"]
        result = await mgr.call_tool("web_search", {"query": "q", "num_results": 1})
        assert result["query"] == "q"
        with pytest.raises(ValueError, match="未知 MCP 工具"):
            await mgr.call_tool("no_such_tool")

    run_manager(MCPManager(), scenario)


# ---------------------------------------------------------------- 容错：超时 + 重试


def test_call_with_retry_succeeds_on_third_attempt(tmp_config):
    """前 2 次失败、第 3 次成功：验证重试次数上限内可自愈。"""
    calls = {"n": 0}

    async def flaky(self, name, arguments=None):      # self = 绑定到 fake conn
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("boom")
        return {"ok": 1}

    async def scenario(mgr):
        result = await mgr._call_with_retry(type("C", (), {"call_tool": flaky})(), "t", {})
        assert result == {"ok": 1}
        assert calls["n"] == 3                        # 1 次原始调用 + 2 次重试

    run_manager(MCPManager(), scenario)


def test_call_with_retry_raises_after_all_attempts(tmp_config):
    """全部失败：重试 2 次后抛出最后一次异常（由上层 Observation 降级）。"""

    async def always_fail(self, name, arguments=None):
        raise RuntimeError("always boom")

    async def scenario(mgr):
        conn = type("C", (), {"call_tool": always_fail})()
        with pytest.raises(RuntimeError, match="always boom"):
            await mgr._call_with_retry(conn, "t", {})

    run_manager(MCPManager(), scenario)


def test_call_with_retry_timeout_then_succeeds(tmp_config, monkeypatch):
    """单次调用超时（超过 MCP_CALL_TIMEOUT_SECONDS）→ 超时被视为失败并重试。"""
    monkeypatch.setattr("mcp_client.mcp_client.MCP_CALL_TIMEOUT_SECONDS", 0.05)
    calls = {"n": 0}

    async def slow_then_ok(self, name, arguments=None):
        calls["n"] += 1
        if calls["n"] == 1:
            await asyncio.sleep(10)                   # 挂起 → 触发超时
        return {"ok": 1}

    async def scenario(mgr):
        result = await mgr._call_with_retry(type("C", (), {"call_tool": slow_then_ok})(), "t", {})
        assert result == {"ok": 1}
        assert calls["n"] == 2                        # 超时重试后成功

    run_manager(MCPManager(), scenario)


def test_manager_refresh_tools(tmp_config):
    async def scenario(mgr):
        tools = await mgr.refresh_tools(BUILTIN_SERVER_ID)
        assert len(tools) == 1
        with pytest.raises(ValueError, match="不存在"):
            await mgr.refresh_tools("nope")

    run_manager(MCPManager(), scenario)


def test_manager_connect_error_records_last_error(tmp_config):
    """HTTP Server 无法连接：工具列表为空且记录 last_error，不抛异常。"""

    async def scenario(mgr):
        with patch(
            "mcp_client.mcp_client.streamable_http_client",
            new=_fake_transport(size=3, raise_on_enter=ConnectionError("refused")),
        ):
            added = await mgr.add_server("bad", "http://127.0.0.1:1/mcp", "", True)
        server = [s for s in mgr.list_servers() if s["id"] == added["id"]][0]
        assert server["tools"] == []
        assert server["last_error"] != ""

    run_manager(MCPManager(), scenario)


def test_manager_skips_disabled_servers(tmp_config):
    """关闭的 Server：start / schema 注入 / 工具路由时跳过。"""

    async def scenario(mgr):
        async with _patch_http_connection():
            await mgr.add_server("off", "http://127.0.0.1:1/mcp", "", False)
        await mgr._start_enabled()  # 跳过 disabled 的 off
        assert len(mgr._tools.get("off", [])) == 0
        schemas = await mgr.list_enabled_schemas()  # 只注入启用的内置工具
        assert [s["function"]["name"] for s in schemas] == ["web_search"]
        with pytest.raises(ValueError, match="未知 MCP 工具"):
            await mgr.call_tool("no_such_tool")  # 遍历时跳过 off

    run_manager(MCPManager(), scenario)


# ---------------------------------------------------------------- sse 管理


def test_manager_add_sse_server(tmp_config):
    """添加 transport=sse 的 Server：走 SseConnection 并发现工具。"""

    async def scenario(mgr):
        async with _patch_sse_connection(tools=[_tool("sse_tool", "d", {})]):
            server = await mgr.add_server("sse_srv", "http://localhost:9999/sse", "", True, "sse")
        assert server["kind"] == "sse"
        srv = [s for s in mgr.list_servers() if s["id"] == server["id"]][0]
        assert srv["tools"][0]["name"] == "sse_tool"
        assert srv["kind"] == "sse"

    run_manager(MCPManager(), scenario)


def test_manager_add_server_invalid_transport(tmp_config):
    """非法 transport 直接拒绝，不写入任何 Server。"""

    async def scenario(mgr):
        before = len(mgr.list_servers())
        with pytest.raises(ValueError, match="仅支持 http / sse"):
            await mgr.add_server("x", "http://x/mcp", "", True, "tcp")
        assert len(mgr.list_servers()) == before

    run_manager(MCPManager(), scenario)


def test_manager_update_server_switch_transport(tmp_config):
    """编辑时可将 Server 从 http 切换为 sse（连接对象类型随之切换）。"""

    async def scenario(mgr):
        async with _patch_http_connection():
            added = await mgr.add_server("s1", "http://a/mcp", "", True)
        sid = added["id"]
        async with _patch_sse_connection(tools=[_tool("sse_tool", "d", {})]):
            updated = await mgr.update_server(sid, "s1", "http://a/sse", "", True, "sse")
        assert updated["kind"] == "sse"
        srv = [s for s in mgr.list_servers() if s["id"] == sid][0]
        assert srv["tools"][0]["name"] == "sse_tool"

    run_manager(MCPManager(), scenario)


def test_manager_add_server_id_unique_after_delete(tmp_config):
    """删除后再添加：id 不冲突、不覆盖已有 Server（旧实现会复用序号导致覆盖）。"""

    async def scenario(mgr):
        async with _patch_http_connection():
            a = await mgr.add_server("a1", "http://a/mcp", "", True)
            b = await mgr.add_server("b1", "http://b/mcp", "", True)
        await mgr.delete_server(a["id"])
        async with _patch_http_connection():
            c = await mgr.add_server("c1", "http://c/mcp", "", True)
        ids = {s["id"] for s in mgr.list_servers()}
        assert len(ids) == 3  # builtin + b + c，无 id 冲突
        assert c["id"] != b["id"]
        # 保留下来的 b 配置未被覆盖
        b_srv = [s for s in mgr.list_servers() if s["id"] == b["id"]][0]
        assert b_srv["name"] == "b1"
        assert b_srv["url"] == "http://b/mcp"

    run_manager(MCPManager(), scenario)


def test_manager_connect_timeout_records_error(tmp_config, monkeypatch):
    """连接阻塞超过 CONNECT_TIMEOUT_SECONDS：不无限等待，记录超时错误并保存配置。"""
    monkeypatch.setattr("mcp_client.mcp_client.CONNECT_TIMEOUT_SECONDS", 0.05)

    @asynccontextmanager
    async def _hang_transport(*args, **kwargs):
        await asyncio.sleep(3600)  # 模拟远程 Server 不可达导致的长期挂起
        yield (AsyncMock(), AsyncMock(), lambda: None)

    async def scenario(mgr):
        with patch("mcp_client.mcp_client.streamable_http_client", new=_hang_transport):
            added = await mgr.add_server("slow", "http://slow/mcp", "", True)
        server = [s for s in mgr.list_servers() if s["id"] == added["id"]][0]
        assert server["tools"] == []
        assert "超时" in server["last_error"]

    run_manager(MCPManager(), scenario)
