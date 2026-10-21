"""v2演示样例读取与导入；管理员显式触发，只写入标记数据，不启动生成。"""
from fastapi import APIRouter, Request

from api.memory_routes import failure, success
from services.demo_data import (
    DemoImportError,
    DemoImportRequest,
    environment_status,
    import_samples,
    list_samples,
)

router = APIRouter(prefix="/api/v2/demo")


@router.get("/samples")
async def samples(request: Request):
    try:
        items = list_samples()
    except DemoImportError as exc:
        return failure(request, exc.status, exc.code, str(exc))
    except Exception:
        return failure(request, 503, "DEMO_DATA_UNAVAILABLE", "演示样例暂不可用")
    return success(request, {"items": items})


@router.get("/environment")
async def environment(request: Request):
    try:
        data = environment_status()
    except Exception:
        return failure(request, 503, "ENVIRONMENT_UNAVAILABLE", "运行环境状态暂不可用")
    return success(request, data)


@router.post("/import")
async def import_demo(request: Request):
    try:
        payload = DemoImportRequest.model_validate_json(await request.body() or b"{}")
    except ValueError:
        return failure(request, 422, "INVALID_DEMO_REQUEST", "演示样例请求无效")
    try:
        data = import_samples(payload.sample_ids)
    except DemoImportError as exc:
        return failure(request, exc.status, exc.code, str(exc))
    except Exception:
        return failure(request, 503, "DEMO_IMPORT_FAILED", "演示样例导入失败，未写入数据")
    return success(request, data)
