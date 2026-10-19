"""HTTP认证适配：v1保留detail，v2使用统一错误包。"""
from typing import Literal
from uuid import uuid4

from fastapi import Request
from pydantic import BaseModel, ConfigDict
from starlette.responses import JSONResponse

from services.security import AccessDenied, authorize


class ApiError(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str
    message: str
    retryable: bool
    request_id: str


class AuthSession(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["admin", "readonly"]


async def authenticate(request: Request, call_next):
    if request.url.path == "/healthz" and request.method in {"GET", "HEAD"}:
        return await call_next(request)
    request.state.request_id = uuid4().hex
    try:
        request.state.principal = authorize(request.headers.get("authorization"), request.method, request.url.path)
    except AccessDenied as exc:
        error = ApiError(code=exc.code, message=exc.message, retryable=exc.status == 503,
                         request_id=request.state.request_id).model_dump()
        content = {"error": error}
        if not request.url.path.startswith("/api/v2/"):
            content["detail"] = exc.message
        headers = {"Cache-Control": "no-store"}
        if exc.status == 401:
            headers["WWW-Authenticate"] = "Bearer"
        return JSONResponse(content, status_code=exc.status, headers=headers)
    return await call_next(request)
