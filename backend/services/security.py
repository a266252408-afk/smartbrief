"""单所有者令牌验证与角色授权；缺配置默认拒绝，不记录凭证。"""
from dataclasses import dataclass
import hmac
import os
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Principal:
    role: str


class AccessDenied(Exception):
    def __init__(self, status, code, message):
        self.status, self.code, self.message = status, code, message


def _valid(token):
    return 32 <= len(token) <= 4096 and not any(c.isspace() for c in token)


def authorize(header: str | None, method: str, path: str) -> Principal:
    admin = os.getenv("SMARTBRIEF_ADMIN_TOKEN", "")
    reader = os.getenv("SMARTBRIEF_READONLY_TOKEN", "")
    if not _valid(admin) or reader and (not _valid(reader) or reader == admin):
        raise AccessDenied(503, "AUTH_NOT_CONFIGURED", "服务端认证配置不可用")
    parts = (header or "").split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise AccessDenied(401, "UNAUTHENTICATED", "请提供有效访问令牌")
    candidate = parts[1].encode("utf-8")
    if hmac.compare_digest(candidate, admin.encode("utf-8")):
        return Principal("admin")
    if not reader or not hmac.compare_digest(candidate, reader.encode("utf-8")):
        raise AccessDenied(401, "UNAUTHENTICATED", "请提供有效访问令牌")
    if method not in {"GET", "HEAD"} and (method, path) != ("POST", "/api/v1/knowledge/search"):
        raise AccessDenied(403, "FORBIDDEN", "只读令牌无权执行此操作")
    return Principal("readonly")


def cors_origins():
    raw = os.getenv("SMARTBRIEF_CORS_ORIGINS", "http://localhost:5174,http://127.0.0.1:5174")
    origins = list(dict.fromkeys(item.strip() for item in raw.split(",") if item.strip()))
    for origin in origins:
        parsed = urlsplit(origin)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
            raise ValueError("CORS必须配置完整HTTP(S)来源白名单，禁止通配符")
    return origins
