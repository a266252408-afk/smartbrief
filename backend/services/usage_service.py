"""模型调用的安全用量契约与任务上下文。"""
from contextvars import ContextVar
from functools import wraps
from typing import Literal
from uuid import uuid4
import asyncio
from datetime import datetime, timezone
from time import perf_counter

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ModelUsage(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
    call_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    agent: str = Field(min_length=1)
    model: str = Field(min_length=1)
    status: Literal["running", "success", "failed", "cancelled", "budget_exceeded"]
    duration_ms: float = Field(ge=0)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    usage_known: bool = False
    estimated_cost: float | None = Field(default=None, ge=0)
    currency: str | None = None
    attempt: int = Field(default=1, ge=1)
    reserved_tokens: int = Field(default=0, ge=0)
    created_at: str = Field(min_length=1)
    error_code: Literal["USAGE_INTERRUPTED", "MODEL_FAILED", "MODEL_CANCELLED", "MODEL_TIMEOUT", "BUDGET_EXCEEDED"] | None = None

    @model_validator(mode="after")
    def consistent(self):
        if self.usage_known != (self.input_tokens is not None and self.output_tokens is not None):
            raise ValueError("Token未知不能伪造为零或已知")
        if not self.usage_known and (self.input_tokens is not None or self.output_tokens is not None):
            raise ValueError("未知用量必须显式null")
        if (self.estimated_cost is None) != (self.currency is None):
            raise ValueError("费用和币种必须同时可用")
        if not self.usage_known and self.estimated_cost is not None:
            raise ValueError("未知用量不能计算费用")
        return self


usage_context: ContextVar[tuple[str, str] | None] = ContextVar("model_usage", default=None)


class ModelCallError(RuntimeError):
    """只暴露固定原因，禁止供应商异常原文进入日志。"""
    code = "MODEL_FAILED"
    retryable = False


class UsagePersistenceError(ModelCallError):
    code = "USAGE_PERSISTENCE_FAILED"


def persist(entry):
    from db import usage_repository
    try:
        usage_repository.save(entry)
    except Exception:
        raise UsagePersistenceError("模型用量持久化失败，任务已停止") from None


def usage_scope(agent: str, *, task=False, state=False, standalone=False):
    """在异步任务边界设置归属；退出/取消时恢复，禁止全局可变账本。"""
    def decorate(function):
        @wraps(function)
        async def wrapped(*args, **kwargs):
            parent = usage_context.get()
            task_id = parent[0] if parent else None
            if task:
                task_id = args[0] if args else kwargs["task_id"]
            if state:
                task_id = (args[0] if args else kwargs["state"]).get("task_id") or task_id
            if standalone:
                task_id = task_id or "chat_" + uuid4().hex
            token = usage_context.set((task_id, agent) if task_id else None)
            try:
                return await function(*args, **kwargs)
            finally:
                usage_context.reset(token)
        return wrapped
    return decorate


async def measured_call(create, kwargs, *, policy, attempt=1, price=None):
    """每次调用前落占位、结束后记录；写入失败不得冒充完成。"""
    context = usage_context.get()
    if context is None:
        return await create(**kwargs)
    entry = ModelUsage(call_id=uuid4().hex, task_id=context[0], agent=context[1],
                       model=kwargs["model"], status="running", duration_ms=0.0,
                       attempt=attempt, created_at=datetime.now(timezone.utc).isoformat(),
                       error_code=None)
    from services.budget import reserve, reservation
    entry.reserved_tokens = reservation(kwargs, policy)
    reserve(entry, policy)
    start = perf_counter()
    try:
        response = await asyncio.wait_for(create(**kwargs), policy.timeout_seconds)
    except asyncio.CancelledError:
        entry.status, entry.error_code = "cancelled", "MODEL_CANCELLED"
        raise
    except Exception as exc:
        from openai import APIConnectionError, APIStatusError, APITimeoutError
        timeout = isinstance(exc, (TimeoutError, APITimeoutError))
        entry.status = "failed"
        entry.error_code = "MODEL_TIMEOUT" if timeout else "MODEL_FAILED"
        error = ModelCallError("模型调用超时" if timeout else "模型调用失败")
        error.code = entry.error_code
        error.retryable = timeout or isinstance(exc, APIConnectionError) or isinstance(exc, APIStatusError) and (exc.status_code == 429 or exc.status_code >= 500)
        raise error from None
    else:
        entry.status, entry.error_code = "success", None
        usage = getattr(response, "usage", None)
        def value(name):
            return usage.get(name) if isinstance(usage, dict) else getattr(usage, name, None)
        incoming, outgoing = value("prompt_tokens"), value("completion_tokens")
        if all(type(n) is int and n >= 0 for n in (incoming, outgoing)):
            entry.input_tokens, entry.output_tokens, entry.usage_known = incoming, outgoing, True
        from services.usage_summary import apply_price
        apply_price(entry, price)
        return response
    finally:
        entry.duration_ms = (perf_counter() - start) * 1000
        persist(entry)
