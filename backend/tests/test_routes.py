"""API 路由测试（api/routes.py）。"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    """隔离配置路径并启动应用（真实拉起内置 MCP Server）。"""
    monkeypatch.setattr("mcp_client.mcp_client.CONFIG_PATH", tmp_path / "mcp_config.json")
    monkeypatch.setattr("tools.builtin_tools.CONFIG_PATH", tmp_path / "tool_config.json")
    from main import app

    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------- 健康检查


def test_health(client):
    resp = client.get("/api/v1/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["mcp_server"] is True


# ---------------------------------------------------------------- 对话


def test_chat_success(client):
    with patch(
        "api.routes.single_agent.run",
        new=AsyncMock(
            return_value={
                "reply": "现在是 12:00:00",
                "tool_calls": [{"name": "current_time", "success": True}],
            }
        ),
    ):
        resp = client.post("/api/v1/chat", json={"message": "现在几点了", "history": []})
    assert resp.status_code == 200
    assert resp.json()["reply"] == "现在是 12:00:00"


def test_chat_failure_returns_500(client):
    with patch(
        "api.routes.single_agent.run", new=AsyncMock(side_effect=RuntimeError("boom"))
    ):
        resp = client.post("/api/v1/chat", json={"message": "hi"})
    assert resp.status_code == 500
    assert "boom" in resp.json()["detail"]


def test_chat_validation_error(client):
    resp = client.post("/api/v1/chat", json={"message": ""})
    assert resp.status_code == 422


# ---------------------------------------------------------------- 工具汇总


def test_list_tools(client):
    resp = client.get("/api/v1/tools")
    assert resp.status_code == 200
    tools = resp.json()["tools"]
    names = {t["name"] for t in tools}
    assert {"current_time", "calculator", "web_search"} <= names
    builtin = [t for t in tools if t["type"] == "builtin"]
    assert all("enabled" in t for t in builtin)
    mcp = [t for t in tools if t["name"] == "web_search"]
    assert mcp[0]["server"] == "web_search_server"


# ---------------------------------------------------------------- 内置工具管理


def test_list_builtin_tools(client):
    resp = client.get("/api/v1/tools/builtin")
    assert resp.status_code == 200
    assert len(resp.json()["tools"]) == 2


def test_toggle_builtin_tool(client):
    resp = client.put("/api/v1/tools/builtin/calculator", json={"enabled": False})
    assert resp.status_code == 200
    assert resp.json()["enabled"] is False
    # 重新开启，避免影响其他测试
    client.put("/api/v1/tools/builtin/calculator", json={"enabled": True})


def test_toggle_builtin_tool_not_found(client):
    resp = client.put("/api/v1/tools/builtin/not_exist", json={"enabled": True})
    assert resp.status_code == 404


# ---------------------------------------------------------------- MCP 管理


def test_list_mcp_servers(client):
    resp = client.get("/api/v1/tools/mcp")
    assert resp.status_code == 200
    servers = resp.json()["servers"]
    assert len(servers) == 1
    assert servers[0]["builtin"] is True
    assert servers[0]["tools"][0]["name"] == "web_search"


def test_add_mcp_server_success(client):
    with patch(
        "api.routes.mcp_manager.add_server",
        new=AsyncMock(return_value={"id": "mcp-2", "name": "s", "enabled": True}),
    ):
        resp = client.post(
            "/api/v1/tools/mcp",
            json={"name": "s", "url": "http://localhost:9/mcp", "description": "d"},
        )
    assert resp.status_code == 200
    assert resp.json()["server"]["id"] == "mcp-2"


def test_add_mcp_server_error(client):
    with patch(
        "api.routes.mcp_manager.add_server",
        new=AsyncMock(side_effect=ValueError("已存在同名 Server：s")),
    ):
        resp = client.post(
            "/api/v1/tools/mcp", json={"name": "s", "url": "http://x/mcp"}
        )
    assert resp.status_code == 400
    assert "同名" in resp.json()["detail"]


def test_add_mcp_server_with_transport(client):
    """transport 字段透传：http / sse 均支持。"""
    for transport in ("http", "sse"):
        mock_add = AsyncMock(return_value={"id": f"mcp-{transport}", "kind": transport, "enabled": True})
        with patch("api.routes.mcp_manager.add_server", new=mock_add):
            resp = client.post(
                "/api/v1/tools/mcp",
                json={
                    "name": "s",
                    "url": f"http://x/{transport}",
                    "description": "d",
                    "enabled": True,
                    "transport": transport,
                },
            )
        assert resp.status_code == 200
        mock_add.assert_awaited_once_with("s", f"http://x/{transport}", "d", True, transport)


def test_add_mcp_server_invalid_transport(client):
    """非法 transport 在请求模型层被 422 拒绝（不会走到后端写入）。"""
    resp = client.post(
        "/api/v1/tools/mcp",
        json={"name": "s", "url": "http://x/mcp", "transport": "tcp"},
    )
    assert resp.status_code == 422


def test_update_mcp_server_with_transport(client):
    """编辑时 transport 字段透传。"""
    mock_update = AsyncMock(return_value={"id": "mcp-2", "kind": "sse", "enabled": True})
    with patch("api.routes.mcp_manager.update_server", new=mock_update):
        resp = client.put(
            "/api/v1/tools/mcp/mcp-2",
            json={
                "name": "s2",
                "url": "http://y/sse",
                "description": "",
                "enabled": True,
                "transport": "sse",
            },
        )
    assert resp.status_code == 200
    mock_update.assert_awaited_once_with("mcp-2", "s2", "http://y/sse", "", True, "sse")


def test_update_mcp_server_success(client):
    with patch(
        "api.routes.mcp_manager.update_server",
        new=AsyncMock(return_value={"id": "mcp-2", "name": "s2", "enabled": False}),
    ):
        resp = client.put(
            "/api/v1/tools/mcp/mcp-2",
            json={"name": "s2", "url": "http://y/mcp", "description": "", "enabled": False},
        )
    assert resp.status_code == 200
    assert resp.json()["server"]["name"] == "s2"


def test_update_mcp_server_error(client):
    with patch(
        "api.routes.mcp_manager.update_server",
        new=AsyncMock(side_effect=ValueError("内置 Server 不可编辑")),
    ):
        resp = client.put(
            "/api/v1/tools/mcp/builtin-web-search",
            json={"name": "x", "url": "http://x/mcp", "description": "", "enabled": True},
        )
    assert resp.status_code == 400


def test_delete_mcp_server_success(client):
    with patch("api.routes.mcp_manager.delete_server", new=AsyncMock()):
        resp = client.delete("/api/v1/tools/mcp/mcp-2")
    assert resp.status_code == 200
    assert resp.json()["deleted"] == "mcp-2"


def test_delete_mcp_server_error(client):
    with patch(
        "api.routes.mcp_manager.delete_server",
        new=AsyncMock(side_effect=ValueError("内置 Server 不可删除")),
    ):
        resp = client.delete("/api/v1/tools/mcp/builtin-web-search")
    assert resp.status_code == 400


def test_toggle_mcp_server_success(client):
    with patch(
        "api.routes.mcp_manager.set_enabled",
        new=AsyncMock(return_value={"id": "mcp-2", "enabled": True}),
    ):
        resp = client.put("/api/v1/tools/mcp/mcp-2/enabled", json={"enabled": True})
    assert resp.status_code == 200


def test_toggle_mcp_server_not_found(client):
    with patch(
        "api.routes.mcp_manager.set_enabled",
        new=AsyncMock(side_effect=ValueError("Server 不存在")),
    ):
        resp = client.put("/api/v1/tools/mcp/nope/enabled", json={"enabled": True})
    assert resp.status_code == 404


def test_refresh_mcp_server_success(client):
    with patch(
        "api.routes.mcp_manager.refresh_tools",
        new=AsyncMock(return_value=[{"name": "web_search"}]),
    ):
        resp = client.post("/api/v1/tools/mcp/builtin-web-search/refresh")
    assert resp.status_code == 200
    assert resp.json()["tools"][0]["name"] == "web_search"


def test_refresh_mcp_server_not_found(client):
    with patch(
        "api.routes.mcp_manager.refresh_tools",
        new=AsyncMock(side_effect=ValueError("Server 不存在")),
    ):
        resp = client.post("/api/v1/tools/mcp/nope/refresh")
    assert resp.status_code == 404


# ---------------------------------------------------------------- 统计


def test_tool_stats(client):
    with patch("api.routes.single_agent.get_stats", return_value={"t": {"count": 1}}):
        resp = client.get("/api/v1/tools/stats")
    assert resp.status_code == 200
    assert resp.json()["stats"] == {"t": {"count": 1}}
