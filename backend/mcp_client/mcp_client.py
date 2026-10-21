"""MCP 客户端：基于官方 MCP SDK 的多 Server 统一管理。

客户端与服务端的职责：
- MCP 协议分为 Server（工具/资源提供方）与 Client（消费方）两端。
- **Server 端**用 FastMCP（如 mcp_servers/web_search_server.py）：只需注册函数 + 装饰器，
  FastMCP 自动生成 JSON Schema、处理 stdio / HTTP 传输与会话握手。
- **Client 端**用官方 `mcp` SDK 的 `ClientSession`（官方 SDK 内置的 FastMCP 只做 Server，不含 Client；
  注意 FastMCP v2 独立包 `fastmcp` 另提供 Client 封装，本项目基于官方 SDK 选型）：
  它封装了 initialize / notifications/initialized / tools/list / tools/call 等全套流程，
  我们只需拿到 read/write 流就能建立会话——不用再手写 JSON-RPC。

连接类型：
- stdio：内置 Server 以子进程启动，通过 stdio_client 建立会话。
- streamable_http：远程 Server（如 ModelScope 12306），通过 streamable_http_client 连接；
  该传输协议需要先 initialize → 获取 mcp-session-id → 发送 initialized 通知，
  SDK 已自动完成，我们不再关心握手细节。

管理器（MCPManager）：
- 统一管理所有 Server 的开关状态（enabled）与工具列表
- 配置持久化到 backend/mcp_config.json
- 启动时加载配置并拉起内置 stdio Server
"""
import asyncio
import json
import logging
import sys
from contextlib import AsyncExitStack
from pathlib import Path
from uuid import uuid4
from data_paths import runtime_path

from mcp import ClientSession, types
from mcp.client.sse import sse_client
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client

logger = logging.getLogger(__name__)

# 远程 SSE 端点不可达 / 超时（如 ModelScope 远程 Server）时，MCP SDK 会在
# 后台 sse_reader 任务里用 logger.exception 打印完整堆栈（httpx.ReadTimeout），
# 属于「远程服务不可达」的常规噪音，会刷屏 Notebook / 控制台。连接状态已通过
# last_error 上报业务层，这里把 SDK 的 sse 日志压到 CRITICAL 抑制刷屏。
logging.getLogger("mcp.client.sse").setLevel(logging.CRITICAL)

# 内置 stdio Server（不可删除/编辑，可开关）
BUILTIN_SERVER_SCRIPT = (
    Path(__file__).resolve().parents[2] / "mcp_servers" / "web_search_server.py"
)
BUILTIN_SERVER_ID = "builtin-web-search"
BUILTIN_SERVER_NAME = "web_search_server"

CONFIG_PATH = runtime_path('mcp_config.json')

TIMEOUT_SECONDS = 5

# 单次工具调用超时 + 失败重试次数
MCP_CALL_TIMEOUT_SECONDS = 5.0   # 单次工具调用超时上限
MCP_CALL_RETRIES = 2             # 调用失败/超时后的重试次数
# 新增/编辑 Server 时连接并发现工具的总超时（防止阻塞超过前端请求超时，
# 导致「前端报错、后端却已写入配置」的不一致）
CONNECT_TIMEOUT_SECONDS = 8


# ================================================================ 配置持久化


def _load_config() -> dict:
    """读取 MCP Server 配置。"""
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {"servers": []}


def _save_config(servers: list[dict]) -> None:
    """保存 MCP Server 配置。"""
    CONFIG_PATH.write_text(
        json.dumps({"servers": servers}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


# ================================================================ 连接实现


class BaseConnection:
    """MCP 连接基类。"""

    def __init__(self, config: dict) -> None:
        self.id: str = config["id"]
        self.name: str = config.get("name", config["id"])
        self.kind: str = config.get("kind", "http")
        self.url: str = config.get("url", "")
        self.description: str = config.get("description", "")
        self.enabled: bool = config.get("enabled", True)
        self.builtin: bool = config.get("builtin", False)
        self.last_error: str = ""  # 最近一次连接/调用错误（前端展示）

    def to_dict(self) -> dict:
        """配置信息（用于持久化）。"""
        return {
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "url": self.url,
            "description": self.description,
            "enabled": self.enabled,
            "builtin": self.builtin,
        }

    async def start(self) -> None:
        """建立 MCP 会话。"""
        raise NotImplementedError

    async def close(self) -> None:
        """关闭 MCP 会话。"""
        raise NotImplementedError

    async def list_tools(self) -> list:
        """获取 MCP 格式工具列表：[{"name", "description", "inputSchema"}]。"""
        raise NotImplementedError

    async def call_tool(self, name: str, arguments: dict | None = None) -> dict:
        """调用 MCP 工具。"""
        raise NotImplementedError


class StdioConnection(BaseConnection):
    """stdio 连接：基于官方 SDK 子进程 ClientSession。"""

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self._exit_stack: AsyncExitStack | None = None
        self._session: ClientSession | None = None

    @property
    def is_running(self) -> bool:
        return self._session is not None

    async def start(self) -> None:
        """启动子进程并初始化 MCP 会话。"""
        if self._session is not None:
            return
        self._exit_stack = AsyncExitStack()
        try:
            server_params = StdioServerParameters(
                command=sys.executable,
                args=[str(BUILTIN_SERVER_SCRIPT)],
                env=None,
            )
            read_write = await self._exit_stack.enter_async_context(
                stdio_client(server_params)
            )
            read_stream, write_stream = read_write
            self._session = await self._exit_stack.enter_async_context(
                ClientSession(read_stream, write_stream)
            )
            await self._session.initialize()
        except Exception:
            exit_stack, self._exit_stack = self._exit_stack, None
            self._session = None
            if exit_stack is not None:
                try:
                    await exit_stack.aclose()
                except BaseException:
                    # 清理路径绝不能抛异常（含 asyncio.CancelledError），否则会掩盖原始错误。
                    pass
            raise

    async def close(self) -> None:
        """关闭子进程与 MCP 会话（先清空状态，再关闭并吞掉 SDK 关闭异常）。"""
        exit_stack, self._exit_stack = self._exit_stack, None
        self._session = None
        if exit_stack is not None:
            try:
                await exit_stack.aclose()
            except BaseException:
                # MCP SDK 的 stdio_client / streamable_http_client 是异步生成器，
                # 关闭时其内部 anyio cancel scope 可能抛 RuntimeError 或
                # asyncio.CancelledError（应用 shutdown 取消生命周期任务时尤为常见），
                # 此时连接实际已断开，忽略即可，保证上层状态一致、可重新建立连接。
                pass

    async def list_tools(self) -> list:
        if self._session is None:
            raise ConnectionError("MCP Server 未启动")
        result = await self._session.list_tools()
        return [_tool_to_dict(t) for t in result.tools]

    async def call_tool(self, name: str, arguments: dict | None = None) -> dict:
        if self._session is None:
            raise ConnectionError("MCP Server 未启动")
        result = await self._session.call_tool(name, arguments=arguments or {})
        return _parse_call_result(result)


class HttpConnection(BaseConnection):
    """HTTP 连接：基于官方 SDK streamable_http ClientSession。"""

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self._exit_stack: AsyncExitStack | None = None
        self._session: ClientSession | None = None

    async def start(self) -> None:
        """建立 streamable_http MCP 会话。"""
        if self._session is not None:
            return
        if not self.url:
            raise ConnectionError("未配置 MCP Server URL")
        self._exit_stack = AsyncExitStack()
        try:
            read_write = await self._exit_stack.enter_async_context(
                streamable_http_client(self.url)
            )
            read_stream, write_stream, _ = read_write  # streamable_http 返回三元组（含 session_id 函数）
            self._session = await self._exit_stack.enter_async_context(
                ClientSession(read_stream, write_stream)
            )
            await self._session.initialize()
            self.last_error = ""
        except Exception as exc:
            self.last_error = str(exc)
            exit_stack, self._exit_stack = self._exit_stack, None
            self._session = None
            if exit_stack is not None:
                try:
                    await exit_stack.aclose()
                except BaseException:
                    pass
            raise ConnectionError(f"HTTP 连接失败（{self.url}）：{exc}") from exc

    async def close(self) -> None:
        """关闭 HTTP 会话（先清空状态，再关闭并吞掉 SDK 关闭异常）。"""
        exit_stack, self._exit_stack = self._exit_stack, None
        self._session = None
        if exit_stack is not None:
            try:
                await exit_stack.aclose()
            except BaseException:
                # 同 StdioConnection.close：streamable_http_client 异步生成器
                # 关闭时其内部 anyio cancel scope 可能抛 RuntimeError 或
                # asyncio.CancelledError（应用 shutdown 取消时），忽略即可。
                pass

    async def list_tools(self) -> list:
        if self._session is None:
            raise ConnectionError("MCP Server 未连接")
        result = await self._session.list_tools()
        return [_tool_to_dict(t) for t in result.tools]

    async def call_tool(self, name: str, arguments: dict | None = None) -> dict:
        if self._session is None:
            raise ConnectionError("MCP Server 未连接")
        result = await self._session.call_tool(name, arguments=arguments or {})
        return _parse_call_result(result)


class SseConnection(BaseConnection):
    """SSE 连接：基于官方 SDK sse_client（旧版 HTTP+SSE 传输）。

    与 HttpConnection 的唯一区别：使用 ``sse_client`` 建立会话，
    且其异步上下文管理器 yield 二元组 ``(read_stream, write_stream)``
    （streamable_http_client yield 三元组，含 session_id 函数）。
    """

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self._exit_stack: AsyncExitStack | None = None
        self._session: ClientSession | None = None

    async def start(self) -> None:
        """建立 SSE MCP 会话。"""
        if self._session is not None:
            return
        if not self.url:
            raise ConnectionError("未配置 MCP Server URL")
        self._exit_stack = AsyncExitStack()
        try:
            read_write = await self._exit_stack.enter_async_context(
                sse_client(self.url)
            )
            read_stream, write_stream = read_write  # sse_client 返回二元组
            self._session = await self._exit_stack.enter_async_context(
                ClientSession(read_stream, write_stream)
            )
            await self._session.initialize()
            self.last_error = ""
        except Exception as exc:
            self.last_error = str(exc)
            exit_stack, self._exit_stack = self._exit_stack, None
            self._session = None
            if exit_stack is not None:
                try:
                    await exit_stack.aclose()
                except BaseException:
                    pass
            raise ConnectionError(f"SSE 连接失败（{self.url}）：{exc}") from exc

    async def close(self) -> None:
        """关闭 SSE 会话（先清空状态，再关闭并吞掉 SDK 关闭异常）。"""
        exit_stack, self._exit_stack = self._exit_stack, None
        self._session = None
        if exit_stack is not None:
            try:
                await exit_stack.aclose()
            except BaseException:
                # 同 StdioConnection.close：sse_client 异步生成器关闭时
                # 其内部 anyio cancel scope 可能抛 RuntimeError 或
                # asyncio.CancelledError（应用 shutdown 取消时），忽略即可。
                pass

    async def list_tools(self) -> list:
        if self._session is None:
            raise ConnectionError("MCP Server 未连接")
        result = await self._session.list_tools()
        return [_tool_to_dict(t) for t in result.tools]

    async def call_tool(self, name: str, arguments: dict | None = None) -> dict:
        if self._session is None:
            raise ConnectionError("MCP Server 未连接")
        result = await self._session.call_tool(name, arguments=arguments or {})
        return _parse_call_result(result)


# ================================================================ 连接工厂


def _create_connection(config: dict) -> BaseConnection:
    """按配置创建对应传输类型的连接（stdio / streamable_http / sse）。"""
    kind = config.get("kind", "http")
    if kind == "stdio":
        return StdioConnection(config)
    if kind == "sse":
        return SseConnection(config)
    return HttpConnection(config)


# ================================================================ 工具函数


def _tool_to_dict(tool: types.Tool) -> dict:
    """将官方 SDK Tool 对象转为 dict（保持原有字段结构）。"""
    return {
        "name": tool.name,
        "description": tool.description or "",
        "inputSchema": tool.inputSchema
        or {"type": "object", "properties": {}, "required": []},
    }


def _parse_call_result(result: types.CallToolResult) -> dict:
    """解析 tools/call 结果：优先取第一个 text content 并解析 JSON。"""
    for item in result.content:
        if isinstance(item, types.TextContent):
            try:
                return json.loads(item.text)
            except json.JSONDecodeError:
                return {"text": item.text}
    return {"content": [c.model_dump() for c in result.content]}


def _to_openai_schema(tool: dict) -> dict | None:
    """将 MCP 工具格式转换为 OpenAI Function Calling Schema。"""
    name = tool.get("name")
    if not name:
        return None
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": tool.get("description", ""),
            "parameters": tool.get("inputSchema")
            or {"type": "object", "properties": {}, "required": []},
        },
    }


# ================================================================ 管理器


class MCPManager:
    """统一管理多个 MCP Server（内置 stdio + 自定义 http）。"""

    def __init__(self) -> None:
        self._servers: dict[str, BaseConnection] = {}
        self._tools: dict[str, list] = {}  # server_id -> MCP 工具列表（缓存）

    # ------------------------------------------------------------ 生命周期

    async def start(self) -> None:
        """加载配置并启动所有启用的 Server（内置 stdio 自动拉起子进程）。"""
        config = _load_config()
        servers = config.get("servers", [])
        # 确保内置 Server 始终存在（不可删除）
        if not any(s.get("id") == BUILTIN_SERVER_ID for s in servers):
            servers.insert(
                0,
                {
                    "id": BUILTIN_SERVER_ID,
                    "name": BUILTIN_SERVER_NAME,
                    "kind": "stdio",
                    "url": "",
                    "description": "内置联网搜索（Bing，网络失败返回错误）",
                    "enabled": True,
                    "builtin": True,
                },
            )
            _save_config(servers)
        for cfg in servers:
            self._servers[cfg["id"]] = _create_connection(cfg)
        await self._start_enabled()

    async def close(self) -> None:
        for conn in self._servers.values():
            await conn.close()

    async def _start_enabled(self) -> None:
        """启动所有启用且在线的 Server，并刷新工具缓存。"""
        for conn in self._servers.values():
            if not conn.enabled:
                continue
            await self._connect(conn)

    async def _connect(self, conn: BaseConnection) -> None:
        """建立连接并刷新工具列表（失败不抛出，记录错误供前端展示）。

        连接与工具发现受 CONNECT_TIMEOUT_SECONDS 总超时保护，避免远程
        Server 不可达时长时间阻塞——否则前端会先因请求超时报错，后端
        却随后把配置写盘，造成「前端报错、后端已写入」的不一致。
        """
        try:
            await asyncio.wait_for(self._open_session(conn), timeout=CONNECT_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            self._tools[conn.id] = []
            conn.last_error = f"连接超时（>{CONNECT_TIMEOUT_SECONDS}s）"
            await conn.close()  # 清理取消后可能残留的会话/子进程
        except Exception as exc:
            self._tools[conn.id] = []
            conn.last_error = str(exc)

    async def _open_session(self, conn: BaseConnection) -> None:
        """启动会话并刷新工具缓存（供 _connect 超时控制内部调用）。"""
        await conn.start()
        tools = await conn.list_tools()
        self._tools[conn.id] = tools

    @property
    def is_running(self) -> bool:
        """内置 stdio Server 是否在运行（健康检查用）。"""
        conn = self._servers.get(BUILTIN_SERVER_ID)
        return isinstance(conn, StdioConnection) and conn.is_running

    # ------------------------------------------------------------ 查询

    def list_servers(self) -> list[dict]:
        """Server 列表（含工具、开关状态），供前端展示。"""
        result = []
        for conn in self._servers.values():
            tools = []
            if conn.enabled:
                tools = [
                    {
                        "name": t.get("name"),
                        "description": t.get("description", ""),
                        "schema": t,
                    }
                    for t in self._tools.get(conn.id, [])
                ]
            item = conn.to_dict()
            item["tools"] = tools
            item["last_error"] = conn.last_error
            result.append(item)
        return result

    async def list_enabled_schemas(self) -> list[dict]:
        """已启用 Server 的全部工具 Schema（OpenAI 格式），供 LLM 注入。"""
        schemas: list[dict] = []
        seen: set[str] = set()
        for conn in self._servers.values():
            if not conn.enabled:
                continue
            for tool in self._tools.get(conn.id, []):
                schema = _to_openai_schema(tool)
                if schema and schema["function"]["name"] not in seen:
                    seen.add(schema["function"]["name"])
                    schemas.append(schema)
        return schemas

    async def call_tool(self, name: str, arguments: dict | None = None) -> dict:
        """按工具名在启用的 Server 中路由调用（超时 5s + 重试 2 次）。

        找不到工具时，附上各 Server 的连接错误摘要（last_error），
        便于诊断「内置 Server 为何没有注册工具」（如子进程启动失败、超时）。
        """
        for conn in self._servers.values():
            if not conn.enabled:
                continue
            tool_names = {t.get("name") for t in self._tools.get(conn.id, [])}
            if name in tool_names:
                return await self._call_with_retry(conn, name, arguments)
        errs = [
            f"{s.id}: {s.last_error}"
            for s in self._servers.values()
            if s.enabled and s.last_error
        ]
        suffix = f"（连接诊断：{'；'.join(errs)}）" if errs else ""
        raise ValueError(f"未知 MCP 工具：{name}{suffix}")

    async def _call_with_retry(self, conn, name: str, arguments: dict | None) -> dict:
        """带超时与重试的工具调用。

        容错策略：每次调用限时 5s，失败/超时重试 2 次；
        全部失败才抛出最后一次异常（由上层 ReAct 循环的 Observation 记录后降级处理）。
        """
        last_exc: Exception | None = None
        for attempt in range(MCP_CALL_RETRIES + 1):
            try:
                return await asyncio.wait_for(
                    conn.call_tool(name, arguments), timeout=MCP_CALL_TIMEOUT_SECONDS
                )
            except Exception as exc:  # noqa: BLE001 - 重试兜底，必须捕获所有异常
                last_exc = exc
                if attempt < MCP_CALL_RETRIES:
                    logger.warning(
                        "MCP 工具 %s 调用失败（第 %d/%d 次）：%s，重试中",
                        name, attempt + 1, MCP_CALL_RETRIES, exc,
                    )
        raise last_exc

    # ------------------------------------------------------------ 管理操作

    async def add_server(
        self,
        name: str,
        url: str,
        description: str = "",
        enabled: bool = True,
        transport: str = "http",
    ) -> dict:
        """添加自定义远程 MCP Server（保存后立即尝试发现工具）。

        :param transport: 传输方式，"http"（Streamable HTTP）或 "sse"（HTTP+SSE）。
        :注意: 连接失败时**仍然保存配置**并返回 last_error（便于前端提示
          「已保存但连接失败」），避免「前端报错、后端已写入」的歧义；
          连接阶段受 CONNECT_TIMEOUT_SECONDS 超时保护。
        """
        name = name.strip()
        url = url.strip()
        transport = transport.strip() if transport else "http"
        if transport not in ("http", "sse"):
            raise ValueError("transport 仅支持 http / sse")
        if not name or not url:
            raise ValueError("名称与 URL 不能为空")
        if any(s.name == name for s in self._servers.values()):
            raise ValueError(f"已存在同名 Server：{name}")
        conn = _create_connection(
            {
                # 用 uuid 保证 id 全局唯一（避免删除后再添加时
                # 复用旧序号导致覆盖已有 Server 的数据丢失）
                "id": f"mcp-{uuid4().hex[:8]}",
                "name": name,
                "kind": transport,
                "url": url,
                "description": description.strip(),
                "enabled": enabled,
                "builtin": False,
            }
        )
        self._servers[conn.id] = conn
        await self._connect(conn)
        self._persist()
        return conn.to_dict()

    async def update_server(
        self,
        server_id: str,
        name: str,
        url: str,
        description: str,
        enabled: bool,
        transport: str | None = None,
    ) -> dict:
        """编辑自定义 Server（可切换 http / sse 传输方式，重新发现工具）。"""
        conn = self._servers.get(server_id)
        if conn is None:
            raise ValueError("Server 不存在")
        if conn.builtin:
            raise ValueError("内置 Server 不可编辑")
        name = name.strip()
        url = url.strip()
        new_kind = transport.strip() if transport else conn.kind
        if new_kind not in ("http", "sse"):
            raise ValueError("transport 仅支持 http / sse")
        if any(s.name == name and s.id != server_id for s in self._servers.values()):
            raise ValueError(f"已存在同名 Server：{name}")
        # 关闭旧连接并重建（传输方式变化时连接对象类型也随之切换）
        await conn.close()
        self._tools.pop(conn.id, None)
        new_conn = _create_connection(
            {
                "id": conn.id,
                "name": name,
                "kind": new_kind,
                "url": url,
                "description": description.strip(),
                "enabled": bool(enabled),
                "builtin": conn.builtin,
            }
        )
        self._servers[server_id] = new_conn
        await self._connect(new_conn)  # 重新发现工具
        self._persist()
        return new_conn.to_dict()

    async def delete_server(self, server_id: str) -> None:
        """删除自定义 Server。"""
        conn = self._servers.get(server_id)
        if conn is None:
            raise ValueError("Server 不存在")
        if conn.builtin:
            raise ValueError("内置 Server 不可删除")
        await conn.close()
        self._servers.pop(server_id, None)
        self._tools.pop(server_id, None)
        self._persist()

    async def set_enabled(self, server_id: str, enabled: bool) -> dict:
        """开关 Server（内置与自定义均支持）。"""
        conn = self._servers.get(server_id)
        if conn is None:
            raise ValueError("Server 不存在")
        conn.enabled = bool(enabled)
        if conn.enabled:
            await self._connect(conn)
        else:
            await conn.close()
            self._tools[conn.id] = []
        self._persist()
        return conn.to_dict()

    async def refresh_tools(self, server_id: str) -> list:
        """手动刷新某 Server 的工具列表。"""
        conn = self._servers.get(server_id)
        if conn is None:
            raise ValueError("Server 不存在")
        await self._connect(conn)
        return self._tools.get(conn.id, [])

    def _persist(self) -> None:
        _save_config([s.to_dict() for s in self._servers.values()])


# 全局单例（FastAPI lifespan 统一管理）
mcp_manager = MCPManager()
