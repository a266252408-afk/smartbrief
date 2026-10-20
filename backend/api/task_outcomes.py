"""v2任务读取适配；沿用v1真实服务，统一成功/错误包。"""
from fastapi import APIRouter, HTTPException, Request
from starlette.responses import JSONResponse
from api.routes import task_status, task_report
from api.security import ApiError
from services.task_outcome import TaskOutcome

router = APIRouter(prefix="/api/v2/tasks")


async def _read(request: Request, task_id: str, report: bool):
    try:
        data = await (task_report(task_id) if report else task_status(task_id))
    except HTTPException as exc:
        code = "REPORT_NOT_AVAILABLE" if report and exc.detail == "任务尚未完成，暂无可用报告" else "TASK_NOT_FOUND"
        return JSONResponse({"error": ApiError(code=code, message=exc.detail, retryable=False, request_id=request.state.request_id).model_dump()}, status_code=exc.status_code)
    outcome = TaskOutcome.model_validate({key: data[key] for key in TaskOutcome.model_fields})
    data.update(outcome.model_dump())
    return {"data": data, "meta": {"request_id": request.state.request_id}}


@router.get("/{task_id}")
async def detail(request: Request, task_id: str):
    return await _read(request, task_id, False)


@router.get("/{task_id}/report")
async def report(request: Request, task_id: str):
    return await _read(request, task_id, True)
