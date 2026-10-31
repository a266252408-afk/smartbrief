"""评测中心管理入口；不把隐藏标准注入被测任务。"""
from fastapi import APIRouter, Request

from api.evaluation_routes import result
from api.memory_routes import failure
from services import evaluation_catalog_service as catalog
from services import evaluation_run_service as runs
from evaluation.mechanism import MechanismRequest
from services.evaluation_service import EvaluationError

router = APIRouter(prefix='/api/v2/evaluation-center')


@router.get('/capabilities')
async def capabilities(request: Request):
    return result(request,runs.capabilities())


@router.post('/runs')
async def start_run(request: Request):
    try:
        body=MechanismRequest.model_validate(await request.json())
    except ValueError:
        return failure(request,422,'INVALID_MECHANISM_REQUEST','案例重复、未接通或请求格式无效')
    try:
        return result(request,runs.start(body),202)
    except (EvaluationError,catalog.CatalogError) as exc:
        return failure(request,exc.status,exc.code,str(exc))


@router.get('/runs')
async def run_list(request: Request):
    try:
        return result(request,runs.list_runs())
    except EvaluationError as exc:
        return failure(request,exc.status,exc.code,str(exc))


@router.get('/runs/{run_id}')
async def run_detail(request: Request,run_id: str):
    try:
        return result(request,runs.get(run_id))
    except EvaluationError as exc:
        return failure(request,exc.status,exc.code,str(exc))


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
