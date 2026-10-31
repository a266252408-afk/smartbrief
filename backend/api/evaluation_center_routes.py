"""评测中心管理入口；不把隐藏标准注入被测任务。"""
from fastapi import APIRouter, Request

from api.evaluation_routes import result
from api.memory_routes import failure
from services import evaluation_catalog_service as catalog

router = APIRouter(prefix='/api/v2/evaluation-center')


@router.get('/catalog')
async def listing(request: Request, version: str = catalog.VERSION):
    try:
        return result(request,catalog.summary(version))
    except catalog.CatalogError as exc:
        return failure(request,exc.status,exc.code,str(exc))


@router.get('/cases/{case_id}')
async def detail(request: Request, case_id: str):
    try:
        return result(request,catalog.detail(case_id))
    except catalog.CatalogError as exc:
        return failure(request,exc.status,exc.code,str(exc))
