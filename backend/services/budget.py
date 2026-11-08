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
    max_task_tokens: int = Field(default=500000, ge=1)
    completion_reserve: int = Field(default=16384, ge=1)
    timeout_seconds: float = Field(default=180, gt=0, le=600)
    max_retries: int = Field(default=1, ge=0, le=5)

    @classmethod
    def from_env(cls):
        try:
            return cls(max_task_tokens=os.getenv("MAX_TASK_TOKENS", "500000"),
                       completion_reserve=os.getenv("MODEL_COMPLETION_RESERVE", "16384"),
                       timeout_seconds=os.getenv("MODEL_TIMEOUT_SECONDS", "180"),
                       max_retries=os.getenv("MODEL_MAX_RETRIES", "1"))
        except ValueError:
            raise UsagePersistenceError("模型预算/超时配置无效") from None

    @classmethod
    def for_role(cls, role: str):
        """主备各自的输出上限与超时；未单独配置时回退通用值。

        主备模型的思考/输出特性不同，共用一个完成预留会让备用模型
        在思考 token 用尽后返回空正文，故按角色独立配置。
        """
        prefix = {"primary": "PRIMARY", "backup": "BACKUP"}.get(role)
        if prefix is None:
            raise ValueError("未知模型角色")
        base = cls.from_env()
        try:
            return cls(**(base.model_dump() | {
                "completion_reserve": int(os.getenv(f"{prefix}_MODEL_COMPLETION_RESERVE",
                                                    base.completion_reserve)),
                "timeout_seconds": float(os.getenv(f"{prefix}_MODEL_TIMEOUT_SECONDS",
                                                   base.timeout_seconds)),
            }))
        except (TypeError, ValueError):
            raise UsagePersistenceError("模型预算/超时配置无效") from None


def completion_reserve_for(model: str, policy: ModelPolicy) -> int:
    """按模型限制输出预留；GLM 思考型模型给 32k 会长时间不返回，压到 16k。"""
    cap = 16384
    try:
        cap = int(os.getenv("GLM_MODEL_MAX_OUTPUT_TOKENS", str(cap)))
    except ValueError:
        cap = 16384
    if "glm" in (model or "").lower():
        return min(policy.completion_reserve, cap)
    return policy.completion_reserve


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
