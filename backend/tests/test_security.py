"""认证默认拒绝、角色权限、配置与CORS行为。"""
import pytest
from fastapi.testclient import TestClient
from tests.auth_support import ADMIN_TOKEN, READ_TOKEN, ADMIN_HEADERS, READ_HEADERS


@pytest.fixture
def security_client(monkeypatch):
    monkeypatch.setenv("SMARTBRIEF_ADMIN_TOKEN", ADMIN_TOKEN)
    monkeypatch.setenv("SMARTBRIEF_READONLY_TOKEN", READ_TOKEN)
    from main import app
    return TestClient(app)


@pytest.mark.parametrize("path", ["/api/v1/tools", "/api/v1/health", "/docs", "/openapi.json", "/api/v2/auth/session"])
def test_anonymous_is_rejected(security_client, path):
    response = security_client.get(path)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize("header", ["Bearer wrong", "Basic ignored", "Bearer", "Bearer x y", "Bearer 非法"])
def test_invalid_credentials(security_client, header):
    # HTTP头必须可编码，非ASCII凭证用UTF-8字节进入ASGI。
    headers = {"Authorization": header.encode() if "非法" in header else header}
    response = security_client.get("/api/v1/tools", headers=headers)
    assert response.status_code == 401


@pytest.mark.parametrize("headers,role", [(ADMIN_HEADERS,"admin"),(READ_HEADERS,"readonly")])
def test_verified_role_envelope(security_client, headers, role):
    response = security_client.get("/api/v2/auth/session", headers=headers)
    assert response.status_code == 200
    assert response.json()["data"] == {"role": role}
    assert response.json()["meta"]["request_id"]
    assert ADMIN_TOKEN not in response.text and READ_TOKEN not in response.text


@pytest.mark.parametrize("method,path", [("post","/api/v1/generate"),("delete","/api/v1/tasks/missing"),("put","/api/v1/tools/builtin/missing"),("post","/api/v1/chat"),("post","/api/v1/skills/import")])
def test_readonly_cannot_write(security_client, method, path):
    response = security_client.request(method.upper(), path, headers=READ_HEADERS, json={})
    assert response.status_code == 403
    assert response.json()["detail"]


def test_public_probe_is_minimal(security_client):
    assert security_client.get("/healthz").json() == {"status":"ok"}
    assert security_client.head("/healthz").status_code == 200
    assert security_client.post("/healthz").status_code == 401


@pytest.mark.parametrize("admin,reader", [("",READ_TOKEN),(" ",READ_TOKEN),(ADMIN_TOKEN,ADMIN_TOKEN),(" " + ADMIN_TOKEN,READ_TOKEN)])
def test_bad_configuration_fails_closed(security_client, monkeypatch, admin, reader):
    monkeypatch.setenv("SMARTBRIEF_ADMIN_TOKEN", admin)
    monkeypatch.setenv("SMARTBRIEF_READONLY_TOKEN", reader)
    response=security_client.get("/api/v2/auth/session", headers=ADMIN_HEADERS)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "AUTH_NOT_CONFIGURED"


def test_optional_readonly_and_safe_methods(security_client,monkeypatch):
    monkeypatch.delenv("SMARTBRIEF_READONLY_TOKEN",raising=False)
    assert security_client.get("/api/v1/tools",headers=ADMIN_HEADERS).status_code==200
    assert security_client.get("/api/v1/tools",headers=READ_HEADERS).status_code==401


def test_denial_does_not_expose_credentials(security_client, caplog):
    response=security_client.post("/api/v1/generate",headers=READ_HEADERS,json={"topic":"测试"})
    assert response.status_code==403
    assert READ_TOKEN not in response.text + caplog.text
    assert ADMIN_TOKEN not in response.text + caplog.text


def test_cors_uses_explicit_allowlist(security_client):
    headers={"Origin":"http://localhost:5174","Access-Control-Request-Method":"GET","Access-Control-Request-Headers":"Authorization"}
    response=security_client.options("/api/v1/tools",headers=headers)
    assert response.headers.get("access-control-allow-origin")=="http://localhost:5174"
    headers["Origin"]="https://untrusted.example"
    response=security_client.options("/api/v1/tools",headers=headers)
    assert "access-control-allow-origin" not in response.headers


@pytest.mark.parametrize("raw,expected", [("",[]),("http://localhost:5174,http://localhost:5174",["http://localhost:5174"]),("https://app.example",["https://app.example"])])
def test_cors_configuration(monkeypatch, raw, expected):
    from services.security import cors_origins
    monkeypatch.setenv("SMARTBRIEF_CORS_ORIGINS",raw)
    assert cors_origins()==expected


@pytest.mark.parametrize("raw", ["*","https://app.example/path","https://user:pass@app.example","https://app.example?q=x","https://app.example#fragment"])
def test_invalid_cors_configuration(monkeypatch,raw):
    from services.security import cors_origins
    monkeypatch.setenv("SMARTBRIEF_CORS_ORIGINS",raw)
    with pytest.raises(ValueError):
        cors_origins()


def test_readonly_query_and_head(security_client):
    assert security_client.head("/api/v1/tools",headers=READ_HEADERS).status_code != 403
    # 请求体校验422证明只读查询已通过认证，不触发实际嵌入。
    assert security_client.post("/api/v1/knowledge/search",headers=READ_HEADERS,json={}).status_code==422


@pytest.mark.parametrize("length", [1, 6, 31, 4097])
def test_configured_token_has_no_application_length_requirement(security_client, monkeypatch, length):
    token = "a" * length
    monkeypatch.setenv("SMARTBRIEF_ADMIN_TOKEN", token)
    response = security_client.get("/api/v2/auth/session", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    assert response.json()["data"]["role"] == "admin"
    assert token not in response.json()["data"].values()
    assert security_client.get("/api/v2/auth/session", headers={"Authorization": "Bearer wrong-value"}).status_code == 401
    assert security_client.get("/api/v2/auth/session").status_code == 401


def test_short_readonly_token_keeps_role_boundaries(security_client, monkeypatch):
    monkeypatch.setenv("SMARTBRIEF_ADMIN_TOKEN", "fixture-admin")
    monkeypatch.setenv("SMARTBRIEF_READONLY_TOKEN", "r")
    headers = {"Authorization": "Bearer r"}
    assert security_client.get("/api/v2/auth/session", headers=headers).json()["data"]["role"] == "readonly"
    assert security_client.post("/api/v1/generate", headers=headers, json={"topic": "测试"}).status_code == 403
