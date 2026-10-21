"""v2记忆管理与再生成适配，错误不透传外部异常。"""
import asyncio
from fastapi import APIRouter, Request
from starlette.responses import JSONResponse
from api.security import ApiError
from services import memory_management, task_regeneration

router = APIRouter(prefix="/api/v2")


def failure(request, status, code, message):
    return JSONResponse({"error":ApiError(code=code,message=message,retryable=status==503,request_id=request.state.request_id).model_dump()},status_code=status)


def success(request, data):
    return {"data":data,"meta":{"request_id":request.state.request_id}}


@router.get("/memory")
async def memories(request: Request, page: str = "1", page_size: str = "10", status: str | None = None):
    try:
        parsed_page, parsed_size = int(page), int(page_size)
        if parsed_page < 1 or not 1 <= parsed_size <= 100 or status is not None and status not in memory_management.STATES:
            raise ValueError
    except ValueError:
        return failure(request,422,"INVALID_MEMORY_FILTER","分页或记忆状态无效")
    try:
        data = await asyncio.to_thread(memory_management.list_memories, parsed_page, parsed_size, status)
        return success(request,data)
    except Exception:
        return failure(request,503,"MEMORY_UNAVAILABLE","记忆库暂不可用")


@router.delete("/memory/{memory_id}")
async def delete_memory(request: Request, memory_id: str):
    try:
        result = await asyncio.to_thread(memory_management.delete_memory,memory_id)
    except memory_management.InvalidMemoryId:
        return failure(request,422,"INVALID_MEMORY_ID","记忆ID无效")
    except Exception:
        return failure(request,503,"MEMORY_UNAVAILABLE","记忆库暂不可用，已登记删除的记录不会再召回")
    if result is None:
        return failure(request,404,"MEMORY_NOT_FOUND","记忆不存在")
    return success(request,result)


@router.post("/tasks/{task_id}/regenerate")
async def regenerate(request: Request, task_id: str):
    from services.task_service import get_task_service, TaskControlError
    try:
        service = get_task_service()
        from api.task_control_routes import empty_action
        await empty_action(request)
    except TaskControlError as exc:
        return failure(request, exc.status, exc.code, str(exc))
    try:
        result = await service.regenerate(task_id,key=request.headers.get("Idempotency-Key"))
    except TaskControlError as exc:
        return failure(request,exc.status,exc.code,str(exc))
    except task_regeneration.TaskNotTerminal:
        return failure(request,409,"TASK_NOT_TERMINAL","运行或排队中的任务不能重新生成")
    except Exception:
        return failure(request,503,"TASK_CREATE_FAILED","新任务创建失败")
    if result is None:
        return failure(request,404,"TASK_NOT_FOUND","原任务不存在")
    return success(request,result)
