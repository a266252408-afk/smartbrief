"""v2任务控制HTTP适配；领域状态与句柄交给服务。"""
from fastapi import APIRouter, Request

from api.memory_routes import failure
from starlette.responses import JSONResponse
from services.task_service import get_task_service, TaskControlError, TaskCreateRequest

router = APIRouter(prefix="/api/v2/tasks")


@router.post("/{task_id}/cancel")
async def cancel(request: Request, task_id: str):
    try:
        result = await get_task_service().cancel(task_id)
    except TaskControlError as exc:
        return failure(request, exc.status, exc.code, str(exc))
    return {"data":result,"meta":{"request_id":request.state.request_id}}


@router.post("")
async def create(request: Request):
    try:
        body = TaskCreateRequest.model_validate(await request.json())
    except ValueError:
        return failure(request, 422, "INVALID_TASK_REQUEST", "主题或文档选择无效")
    try:
        result = await get_task_service().submit(body.topic, body.document_ids)
    except TaskControlError as exc:
        return failure(request, exc.status, exc.code, str(exc))
    return JSONResponse({"data":result,"meta":{"request_id":request.state.request_id}},status_code=202)
