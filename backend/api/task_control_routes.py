"""v2任务控制HTTP适配；领域状态与句柄交给服务。"""
from fastapi import APIRouter, Request

from api.memory_routes import failure
from starlette.responses import JSONResponse
from services.task_service import get_task_service, TaskControlError, TaskCreateRequest

router = APIRouter(prefix="/api/v2/tasks")


async def empty_action(request):
    try:
        if await request.body() and await request.json() != {}:
            raise ValueError
    except ValueError:
        raise TaskControlError("INVALID_ACTION_REQUEST","此动作只接受空请求体",422) from None


@router.post("/{task_id}/cancel")
async def cancel(request: Request, task_id: str):
    try:
        await empty_action(request)
        result = await get_task_service().cancel(task_id,key=request.headers.get("Idempotency-Key"))
    except TaskControlError as exc:
        return failure(request, exc.status, exc.code, str(exc))
    return {"data":result,"meta":{"request_id":request.state.request_id}}


@router.post("/{task_id}/resume")
async def resume(request: Request, task_id: str):
    try:
        await empty_action(request)
        result = await get_task_service().resume(task_id,key=request.headers.get("Idempotency-Key"))
    except TaskControlError as exc:
        return failure(request, exc.status, exc.code, str(exc))
    return JSONResponse({"data":result,"meta":{"request_id":request.state.request_id}},status_code=202)


@router.post("")
async def create(request: Request):
    try:
        body = TaskCreateRequest.model_validate(await request.json())
    except ValueError:
        return failure(request, 422, "INVALID_TASK_REQUEST", "主题或文档选择无效")
    try:
        result = await get_task_service().submit(body.topic, body.document_ids,key=request.headers.get("Idempotency-Key"))
    except TaskControlError as exc:
        return failure(request, exc.status, exc.code, str(exc))
    return JSONResponse({"data":result,"meta":{"request_id":request.state.request_id}},status_code=202)
