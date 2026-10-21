"""用量汇总与显式单价；不调用模型，不反推历史Token。"""
from decimal import Decimal
import json
import math
import os

from pydantic import BaseModel, ConfigDict, Field

from db import task_repository, usage_repository
from services.budget import ModelPolicy, charged
from services.usage_service import ModelUsage, UsagePersistenceError


class ModelPrice(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
    input_per_million: float = Field(ge=0)
    output_per_million: float = Field(ge=0)
    currency: str = Field(pattern=r"^[A-Z]{3}$")


def configured_price(model: str) -> ModelPrice | None:
    raw = os.getenv("MODEL_PRICES_JSON", "")
    if not raw.strip():
        return None
    try:
        mapping = json.loads(raw)
        if not isinstance(mapping, dict):
            raise ValueError("价格须按模型配置")
        value = mapping.get(model)
        return ModelPrice.model_validate(value) if value is not None else None
    except (ValueError, TypeError):
        raise UsagePersistenceError("模型单价配置无效") from None


def apply_price(entry: ModelUsage, price: ModelPrice | None) -> None:
    if price is None or not entry.usage_known:
        return
    cost = float((Decimal(str(price.input_per_million)) * entry.input_tokens
                  + Decimal(str(price.output_per_million)) * entry.output_tokens) / 1000000)
    if math.isfinite(cost):
        entry.estimated_cost, entry.currency = cost, price.currency


class UsageReport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_id: str
    items: list[ModelUsage]
    call_count: int
    blocked_count: int
    unknown_calls: int
    input_tokens: int | None
    output_tokens: int | None
    duration_ms: float
    estimated_costs: dict[str, float]
    cost_complete: bool
    budget_limit_tokens: int
    budget_charged_tokens: int
    budget_remaining_tokens: int
    budget_estimated: bool


def task_usage(task_id: str) -> dict | None:
    task = task_repository.get_task(task_id)
    if task is None:
        return None
    items = usage_repository.list_usage(task_id)
    for row in items:
        if row["status"] == "running" and task["execution_status"] != "running":
            row["status"], row["error_code"] = "failed", "USAGE_INTERRUPTED"
    calls = [r for r in items if r["status"] != "budget_exceeded"]
    unknown = sum(not r["usage_known"] for r in calls)
    costs = {}
    for row in calls:
        if row["estimated_cost"] is not None:
            costs[row["currency"]] = costs.get(row["currency"], 0.0) + row["estimated_cost"]
    policy = ModelPolicy.from_env()
    spent = sum(charged(r, policy.max_task_tokens) for r in items)
    return UsageReport(task_id=task_id, items=items, call_count=len(calls),
        blocked_count=len(items) - len(calls), unknown_calls=unknown,
        input_tokens=sum(r["input_tokens"] for r in calls) if calls and not unknown else None,
        output_tokens=sum(r["output_tokens"] for r in calls) if calls and not unknown else None,
        duration_ms=sum(r["duration_ms"] for r in calls), estimated_costs=costs,
        cost_complete=bool(calls) and all(r["estimated_cost"] is not None for r in calls),
        budget_limit_tokens=policy.max_task_tokens, budget_charged_tokens=spent,
        budget_remaining_tokens=max(0, policy.max_task_tokens - spent), budget_estimated=bool(unknown)).model_dump()
