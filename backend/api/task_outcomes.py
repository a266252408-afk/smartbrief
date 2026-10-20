"""v2任务读取适配；沿用v1真实服务，统一成功/错误包。"""
from fastapi import APIRouter, HTTPException, Request
from starlette.responses import JSONResponse
from api.routes import task_status, task_report
from api.security import ApiError
from services.task_outcome import TaskOutcome
from services.task_sources import get_sources

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


@router.get("/{task_id}/sources")
async def sources(request: Request, task_id: str):
    try:
        data = get_sources(task_id)
    except ValueError:
        return JSONResponse({"error": ApiError(code="SOURCES_INVALID", message="来源记录损坏，暂不可用", retryable=False, request_id=request.state.request_id).model_dump()}, status_code=500)
    if data is None:
        return JSONResponse({"error": ApiError(code="TASK_NOT_FOUND", message="任务不存在", retryable=False, request_id=request.state.request_id).model_dump()}, status_code=404)
    return {"data": data, "meta": {"request_id": request.state.request_id}}


@router.get("/{task_id}/retrievals")
async def retrievals(request: Request, task_id: str):
    from services.retrieval_service import get_retrievals
    try:
        data = get_retrievals(task_id)
    except ValueError:
        return JSONResponse({"error": ApiError(code="RETRIEVALS_INVALID", message="检索记录损坏，暂不可用", retryable=False, request_id=request.state.request_id).model_dump()}, status_code=500)
    if data is None:
        return JSONResponse({"error": ApiError(code="TASK_NOT_FOUND", message="任务不存在", retryable=False, request_id=request.state.request_id).model_dump()}, status_code=404)
    return {"data": data, "meta": {"request_id": request.state.request_id}}
