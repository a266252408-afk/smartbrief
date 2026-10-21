"""确定性调用预算；未知Token用预留量保守扣账，不能声称严格金额封顶。"""
import json
import os

from pydantic import BaseModel, ConfigDict, Field

from services.usage_service import UsagePersistenceError


class BudgetExceeded(UsagePersistenceError):
    code = "BUDGET_EXCEEDED"


class TaskStopped(UsagePersistenceError):
    code = "TASK_STOPPED"


class ModelPolicy(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)
    max_task_tokens: int = Field(default=20000, ge=1)
    completion_reserve: int = Field(default=2048, ge=1)
    timeout_seconds: float = Field(default=60, gt=0, le=600)
    max_retries: int = Field(default=1, ge=0, le=5)

    @classmethod
    def from_env(cls):
        try:
            return cls(max_task_tokens=os.getenv("MAX_TASK_TOKENS", "20000"),
                       completion_reserve=os.getenv("MODEL_COMPLETION_RESERVE", "2048"),
                       timeout_seconds=os.getenv("MODEL_TIMEOUT_SECONDS", "60"),
                       max_retries=os.getenv("MODEL_MAX_RETRIES", "1"))
        except ValueError:
            raise UsagePersistenceError("模型预算/超时配置无效") from None


def reservation(kwargs: dict, policy: ModelPolicy) -> int:
    """UTF-8字节预算含角色/工具结构开销；这是保守估算而非供应商分词器。"""
    payload = {k: v for k, v in kwargs.items() if k in {"messages", "tools", "tool_choice"}}
    return len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) + policy.completion_reserve


def charged(row: dict, limit: int) -> int:
    if row["status"] == "budget_exceeded":
        return 0
    if row["usage_known"]:
        return row["input_tokens"] + row["output_tokens"]
    # 旧账本未知且未预留，不能恢复后当作免费调用。
    return row.get("reserved_tokens") or limit


def reserve(entry, policy):
    """同一SQLite事务检查任务状态、已用预算并持久化本次预留。"""
    from db import usage_repository
    try:
        status = usage_repository.reserve(entry, policy.max_task_tokens)
    except Exception:
        raise UsagePersistenceError("模型预算账本不可用，任务已停止") from None
    if status == "stopped":
        raise TaskStopped("任务已终止，不能继续调用模型")
    if status == "exceeded":
        raise BudgetExceeded("任务Token预算不足，已在调用前停止")
