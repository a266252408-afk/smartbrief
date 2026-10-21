"""v2用量读取适配，认证统一由中间件执行。"""
import asyncio

from fastapi import APIRouter, Request

from api.memory_routes import failure, success
from services.usage_summary import task_usage

router = APIRouter(prefix="/api/v2")


@router.get("/tasks/{task_id}/usage")
async def usage(request: Request, task_id: str):
    try:
        result = await asyncio.to_thread(task_usage, task_id)
    except Exception:
        return failure(request, 503, "USAGE_UNAVAILABLE", "用量记录暂不可用")
    if result is None:
        return failure(request, 404, "TASK_NOT_FOUND", "任务不存在")
    return success(request, result)
