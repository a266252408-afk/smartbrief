"""MCP Server 测试（FastMCP 版 mcp_servers/web_search_server.py）。"""
import asyncio
import json
import sys
from pathlib import Path

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from mcp_servers import web_search_server as wss


# ---------------------------------------------------------------- 搜索实现


class FakeUrlopenResp:
    def __init__(self, body: bytes):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self._body


# `u=a1...` 为 Bing 跳转链接（base64 编码的真实 URL），解析时需还原
PAGE = b"""
<html><body>
<ol id="b_results">
<li class="b_algo">
  <h2><a href="https://www.bing.com/ck/a?x=1&amp;u=a1aHR0cHM6Ly9leGFtcGxlLmNvbS9h">Title A</a></h2>
  <p>Snippet <b>A</b></p>
</li>
<li class="b_algo">
  <h2><a href="https://example.com/b">Title B</a></h2>
  <p>Snippet B</p>
</li>
</ol>
</body></html>
"""


def test_parse_bing_extracts_results():
    results = wss._parse_bing(PAGE.decode("utf-8"))
    assert len(results) == 2
    assert results[0]["title"] == "Title A"
    assert results[0]["snippet"] == "Snippet A"
    assert results[0]["url"] == "https://example.com/a"  # Bing 跳转链接被解码还原
    assert results[1]["title"] == "Title B"
    assert results[1]["url"] == "https://example.com/b"


def test_parse_bing_no_results():
    assert wss._parse_bing("<html></html>") == []


def test_parse_bing_caps_at_five():
    """解析结果超过 5 条时截断（覆盖 break 分支）。"""
    blocks = []
    for i in range(6):
        blocks.append(
            f'<li class="b_algo"><h2><a href="https://example.com/{i}">T{i}</a></h2>'
            f"<p>S{i}</p></li>"
        )
    page = "<html>" + "".join(blocks) + "</html>"
    assert len(wss._parse_bing(page)) == 5


def test_web_search_success(monkeypatch):
    monkeypatch.setattr(wss.urllib.request, "urlopen", lambda *a, **k: FakeUrlopenResp(PAGE))
    result = wss._web_search("Title", 5)
    assert result["source"] == "bing"
    assert result["query"] == "Title"
    assert len(result["results"]) == 2


def test_web_search_network_failure_returns_error(monkeypatch):
    def boom(*args, **kwargs):
        raise OSError("network down")

    monkeypatch.setattr(wss.urllib.request, "urlopen", boom)
    result = wss._web_search("test", 3)
    assert result["source"] == "bing"
    assert result["results"] == []
    assert "network down" in result["error"]


def test_web_search_empty_results_are_not_fabricated(monkeypatch):
    monkeypatch.setattr(wss.urllib.request, "urlopen", lambda *a, **k: FakeUrlopenResp(b"<html></html>"))
    result = wss._web_search("nothing", 3)
    assert result["source"] == "bing"
    assert result["results"] == []
    assert "error" not in result


# ---------------------------------------------------------------- FastMCP 工具


def test_web_search_tool_returns_json(monkeypatch):
    monkeypatch.setattr(wss, "_web_search", lambda q, n: {"query": q, "n": n})
    text = wss.web_search("q", 3)
    assert json.loads(text) == {"query": "q", "n": 3}


def test_web_search_tool_capped_and_invalid_num_results(monkeypatch):
    captured = {}

    def fake_search(q, n):
        captured["n"] = n
        return {"query": q, "n": n}

    monkeypatch.setattr(wss, "_web_search", fake_search)
    wss.web_search("q", 99)
    assert captured["n"] == 10
    wss.web_search("q", "x")
    assert captured["n"] == 5


# ---------------------------------------------------------------- __main__ 入口


def test_main_module_entry(monkeypatch):
    """以 __main__ 方式运行模块时调用 mcp.run(transport=stdio)（覆盖入口分支）。"""
    import runpy

    from mcp.server.fastmcp import FastMCP

    called = []

    def fake_run(*args, **kwargs):
        called.append((args, kwargs))

    # patch FastMCP 类方法：任何实例（含 runpy 新加载的模块实例）均走 fake，避免真实阻塞 stdin
    monkeypatch.setattr(FastMCP, "run", fake_run)
    runpy.run_path(wss.__file__, run_name="__main__")
    assert called, "应调用 mcp.run() 启动 stdio 传输"
    assert any(c[1].get("transport") == "stdio" for c in called)


# ---------------------------------------------------------------- FastMCP 真实子进程闭环


def test_stdio_connection_roundtrip():
    """真实拉起 FastMCP web_search_server 子进程，验证工具发现与调用。"""

    async def scenario():
        server_params = StdioServerParameters(
            command=sys.executable,
            args=[str(Path(__file__).parent / "fixtures" / "mcp_server.py")],
            env=None,
        )
        async with stdio_client(server_params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                tools = await session.list_tools()
                assert tools.tools[0].name == "web_search"
                result = await session.call_tool(
                    "web_search", arguments={"query": "test", "num_results": 2}
                )
                data = json.loads(result.content[0].text)
                assert data["query"] == "test"
                assert data["source"] == "fixture"
                assert isinstance(data["results"], list)
                assert len(data["results"]) == 2
                failure = await session.call_tool("web_search", arguments={"query": "故障"})
                failed = json.loads(failure.content[0].text)
                assert failed["results"] == []
                assert failed["error"] == "固定搜索故障"

    asyncio.run(scenario())
