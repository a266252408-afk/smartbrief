"""用量汇总与显式单价；不调用模型，不反推历史Token。"""
from decimal import Decimal
import json
import math
import os
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from db import task_repository, usage_repository
from services.budget import ModelPolicy, charged
from services.usage_service import ModelUsage, UsagePersistenceError
from services.model_routing import ModelRoute


class ModelPrice(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
    input_per_million: float = Field(ge=0)
    output_per_million: float = Field(ge=0)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    cache_read_per_million: float | None = Field(default=None, ge=0)
    cache_write_per_million: float | None = Field(default=None, ge=0)
    cache_write_mode: Literal['included', 'additional'] = 'included'


def configured_price(model: str, provider: str | None = None) -> ModelPrice | None:
    raw = os.getenv("MODEL_PRICES_JSON", "")
    if not raw.strip():
        return None
    try:
        mapping = json.loads(raw)
        if not isinstance(mapping, dict):
            raise ValueError("价格须按模型配置")
        value = mapping.get(f'{provider}:{model}', mapping.get(model)) if provider else mapping.get(model)
        return ModelPrice.model_validate(value) if value is not None else None
    except (ValueError, TypeError):
        raise UsagePersistenceError("模型单价配置无效") from None


def apply_price(entry: ModelUsage, price: ModelPrice | None) -> None:
    if price is None or not entry.usage_known or entry.cached_input_tokens is None:
        return
    read, write = entry.cached_input_tokens, entry.cache_write_tokens
    if read and price.cache_read_per_million is None:
        return
    if write and price.cache_write_per_million is None:
        return
    normal = entry.input_tokens - read
    write_cost = Decimal(0)
    if price.cache_write_per_million is not None:
        if write is None or price.cache_write_mode == 'included' and write > normal:
            return
        if price.cache_write_mode == 'included':
            normal -= write
        write_cost = Decimal(str(price.cache_write_per_million)) * write
    cost = float((Decimal(str(price.input_per_million)) * normal
                  + Decimal(str(price.cache_read_per_million or 0)) * read + write_cost
                  + Decimal(str(price.output_per_million)) * entry.output_tokens) / 1000000)
    if math.isfinite(cost):
        entry.estimated_cost, entry.currency = cost, price.currency


class RecoveryPosition(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    resume_mode: Literal['checkpoint', 'restart']
    next_agent: Literal['planner', 'researcher', 'writer', 'reviewer', 'rewriter', 'research_revision'] | None = None
    next_step_id: int | None = Field(default=None, ge=1)
    completed_steps: list[int] = Field(default_factory=list)


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
    cached_input_tokens: int | None = None
    cache_write_tokens: int | None = None
    cache_unknown_calls: int = 0
    routing: ModelRoute | None = None
    recovery: RecoveryPosition | None = None


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
    from db.model_route_repository import get_route
    from memory.checkpointer import recovery_position
    def total(field):
        return sum(row[field] for row in calls) if calls and all(row[field] is not None for row in calls) else None
    return UsageReport(task_id=task_id, items=items, call_count=len(calls),
        cached_input_tokens=total('cached_input_tokens'), cache_write_tokens=total('cache_write_tokens'),
        cache_unknown_calls=sum(row['cached_input_tokens'] is None for row in calls),
        routing=get_route(task_id), recovery=recovery_position(task_id),
        blocked_count=len(items) - len(calls), unknown_calls=unknown,
        input_tokens=sum(r["input_tokens"] for r in calls) if calls and not unknown else None,
        output_tokens=sum(r["output_tokens"] for r in calls) if calls and not unknown else None,
        duration_ms=sum(r["duration_ms"] for r in calls), estimated_costs=costs,
        cost_complete=bool(calls) and all(r["estimated_cost"] is not None for r in calls),
        budget_limit_tokens=policy.max_task_tokens, budget_charged_tokens=spent,
        budget_remaining_tokens=max(0, policy.max_task_tokens - spent), budget_estimated=bool(unknown)).model_dump()
